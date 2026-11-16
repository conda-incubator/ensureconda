package cmd

import (
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestRequestRetriesRejectedProxyConnect(t *testing.T) {
	for _, tt := range []struct {
		status  int
		recover bool
	}{
		{503, false}, {407, false}, {503, true},
	} {
		t.Run(fmt.Sprintf("status=%d/recover=%t", tt.status, tt.recover), func(t *testing.T) {
			clock := useRetryClock(t)
			origin := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.WriteString(w, "complete")
			}))
			defer origin.Close()
			var attempts atomic.Int32
			proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodConnect {
					t.Errorf("proxy method = %s, want CONNECT", r.Method)
					http.Error(w, "unexpected method", http.StatusBadRequest)
					return
				}
				if attempts.Add(1) == 1 || !tt.recover {
					http.Error(w, http.StatusText(tt.status), tt.status)
					return
				}
				upstream, err := net.DialTimeout("tcp", origin.Listener.Addr().String(), time.Second)
				if err != nil {
					t.Error(err)
					http.Error(w, err.Error(), http.StatusBadGateway)
					return
				}
				defer upstream.Close()
				conn, buffered, err := w.(http.Hijacker).Hijack()
				if err != nil {
					t.Error(err)
					return
				}
				defer conn.Close()
				_, _ = buffered.WriteString("HTTP/1.1 200 Connection Established\r\n\r\n")
				_ = buffered.Flush()
				go func() {
					_, _ = io.Copy(upstream, buffered)
					_ = upstream.Close()
				}()
				_, _ = io.Copy(conn, upstream)
			}))
			defer proxy.Close()
			proxyURL, err := url.Parse(proxy.URL)
			if err != nil {
				t.Fatal(err)
			}
			client := newHTTPClient()
			transport := client.Transport.(*http.Transport)
			transport.Proxy = http.ProxyURL(proxyURL)
			transport.TLSClientConfig = origin.Client().Transport.(*http.Transport).TLSClientConfig
			defer client.CloseIdleConnections()
			previous := httpClient
			httpClient = func() *http.Client { return client }
			t.Cleanup(func() { httpClient = previous })

			response, err := requestURLWithRetry(origin.URL)
			if tt.recover {
				if err != nil {
					t.Fatal(err)
				}
				if string(response.body) != "complete" || attempts.Load() != 2 || !slices.Equal(clock.delays, []time.Duration{time.Second}) {
					t.Errorf("body %q, attempts %d, delays %v", response.body, attempts.Load(), clock.delays)
				}
				return
			}
			var connectionError *net.OpError
			if !errors.As(err, &connectionError) || connectionError.Op != "proxyconnect" || !strings.Contains(err.Error(), fmt.Sprint(tt.status)) {
				t.Errorf("error = %v, want a proxy connection error with HTTP %d", err, tt.status)
			}
			if attempts.Load() != 7 || !slices.Equal(clock.delays, []time.Duration{time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second, 16 * time.Second, 32 * time.Second}) {
				t.Errorf("attempts %d, delays %v; want seven attempts with exponential backoff", attempts.Load(), clock.delays)
			}
		})
	}
}
