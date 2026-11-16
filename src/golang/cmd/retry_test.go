package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	log "github.com/sirupsen/logrus"
)

type retryClock struct {
	mu     sync.Mutex
	now    time.Time
	delays []time.Duration
}

func (c *retryClock) advance(delay time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(delay)
}

func useRetryClock(t *testing.T) *retryClock {
	t.Helper()
	c := &retryClock{now: time.Date(2026, 11, 15, 12, 0, 0, 0, time.UTC)}
	previousNow, previousSleep := retryNow, retrySleep
	retryNow = func() time.Time {
		c.mu.Lock()
		defer c.mu.Unlock()
		return c.now
	}
	retrySleep = func(delay time.Duration) {
		c.mu.Lock()
		defer c.mu.Unlock()
		c.delays = append(c.delays, delay)
		c.now = c.now.Add(delay)
	}
	t.Cleanup(func() { retryNow, retrySleep = previousNow, previousSleep })
	return c
}

func TestRetryAfterDelay(t *testing.T) {
	now := time.Date(2026, 11, 15, 12, 0, 0, 0, time.UTC)
	for _, tt := range []struct {
		value string
		want  time.Duration
	}{
		{"", 0}, {"garbage", 0}, {"-1", 0}, {"1.5", 0}, {"+2", 0},
		{"0", 0}, {" 12 ", 12 * time.Second},
		{now.Add(30 * time.Second).Format(http.TimeFormat), 30 * time.Second},
		{now.Add(-time.Hour).Format(http.TimeFormat), 0},
		{"9223372037", time.Duration(1<<63 - 1)},
		{"99999999999999999999999999999", time.Duration(1<<63 - 1)},
		{"99999999999999999999999999999x", 0},
	} {
		t.Run(tt.value, func(t *testing.T) {
			if got := retryAfterDelay(tt.value, now); got != tt.want {
				t.Errorf("retryAfterDelay(%q) = %v, want %v", tt.value, got, tt.want)
			}
		})
	}
}

func TestRequestRetriesHTTPFailures(t *testing.T) {
	for _, status := range []int{429, 500, 502, 503, 504, 400, 401, 403, 404, 408, 501} {
		t.Run(fmt.Sprintf("status=%d", status), func(t *testing.T) {
			clock := useRetryClock(t)
			var attempts atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if attempts.Add(1) == 1 {
					w.Header().Set("Retry-After", "3")
					http.Error(w, "failed", status)
					return
				}
				_, _ = io.WriteString(w, "complete response")
			}))
			defer server.Close()
			response, err := requestURLWithRetry(server.URL)
			retryable := slices.Contains([]int{429, 500, 502, 503, 504}, status)
			if !retryable {
				if err == nil || !strings.Contains(err.Error(), fmt.Sprintf("HTTP %d", status)) || attempts.Load() != 1 || len(clock.delays) != 0 {
					t.Fatalf("permanent failure: error %v, attempts %d, delays %v", err, attempts.Load(), clock.delays)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			body := response.Body()
			if string(body) != "complete response" || attempts.Load() != 2 || !slices.Equal(clock.delays, []time.Duration{3 * time.Second}) {
				t.Errorf("body %q, attempts %d, delays %v", body, attempts.Load(), clock.delays)
			}
		})
	}
}

func TestRequestPreservesStatusWithIncompleteErrorBody(t *testing.T) {
	for _, status := range []int{404, 503} {
		for _, stall := range []bool{false, true} {
			t.Run(fmt.Sprintf("status=%d/stall=%t", status, stall), func(t *testing.T) {
				clock := useRetryClock(t)
				useReadTimeout(t, 100*time.Millisecond)
				var attempts atomic.Int32
				release := make(chan struct{})
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if attempts.Add(1) == 1 {
						w.Header().Set("Retry-After", "3")
						w.Header().Set("Content-Length", "1000")
						w.WriteHeader(status)
						_, _ = io.WriteString(w, "partial")
						w.(http.Flusher).Flush()
						if stall {
							<-release
						}
						return
					}
					_, _ = io.WriteString(w, "complete")
				}))
				defer server.Close()
				defer close(release)

				response, err := requestURLWithRetry(server.URL)
				if status == 404 {
					var statusError *httpStatusError
					if !errors.As(err, &statusError) || statusError.statusCode != status || attempts.Load() != 1 || len(clock.delays) != 0 {
						t.Errorf("error %v, attempts %d, delays %v; want HTTP 404 without retrying", err, attempts.Load(), clock.delays)
					}
					return
				}
				if err != nil {
					t.Fatal(err)
				}
				if string(response.Body()) != "complete" || attempts.Load() != 2 || !slices.Equal(clock.delays, []time.Duration{3 * time.Second}) {
					t.Errorf("body %q, attempts %d, delays %v; want a successful retry after Retry-After", response.Body(), attempts.Load(), clock.delays)
				}
			})
		}
	}
}

func TestRequestRetryBudget(t *testing.T) {
	for _, tt := range []struct {
		name     string
		headers  []string
		duration time.Duration
		delays   []time.Duration
	}{
		{"exponential", nil, 0, []time.Duration{time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second, 16 * time.Second, 32 * time.Second}},
		{"long Retry-After", []string{"86400"}, 0, nil},
		{"HTTP date", []string{"Sun, 15 Nov 2026 12:00:30 GMT"}, 0, []time.Duration{30 * time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second, 16 * time.Second}},
		{"cumulative Retry-After", []string{"33", "34"}, 0, []time.Duration{33 * time.Second, 34 * time.Second}},
		{"exact deadline", []string{"70"}, 0, []time.Duration{70 * time.Second}},
		{"30s requests", nil, 30 * time.Second, []time.Duration{time.Second, 2 * time.Second}},
		{"60s requests", nil, 60 * time.Second, []time.Duration{time.Second}},
		{"70s request", nil, 70 * time.Second, nil},
		{"120s request", nil, 120 * time.Second, nil},
	} {
		t.Run(tt.name, func(t *testing.T) {
			clock := useRetryClock(t)
			var attempts atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				i := int(attempts.Add(1)) - 1
				clock.advance(tt.duration)
				if i < len(tt.headers) {
					w.Header().Set("Retry-After", tt.headers[i])
				}
				http.Error(w, "unavailable", http.StatusServiceUnavailable)
			}))
			defer server.Close()
			_, err := requestURLWithRetry(server.URL)
			if err == nil || !strings.Contains(err.Error(), "HTTP 503") {
				t.Fatalf("error = %v, want HTTP 503", err)
			}
			if !slices.Equal(clock.delays, tt.delays) || int(attempts.Load()) != len(tt.delays)+1 {
				t.Errorf("attempts %d, delays %v; want %d, %v", attempts.Load(), clock.delays, len(tt.delays)+1, tt.delays)
			}
		})
	}
}

func TestRequestRetriesInterruptedBodies(t *testing.T) {
	for _, stall := range []bool{false, true} {
		t.Run(fmt.Sprintf("stall=%t", stall), func(t *testing.T) {
			clock := useRetryClock(t)
			useReadTimeout(t, 100*time.Millisecond)
			var attempts atomic.Int32
			release := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if attempts.Add(1) == 1 {
					// Retry-After must not extend waits for a failed 200 body.
					w.Header().Set("Retry-After", "60")
					w.Header().Set("Content-Length", "1000")
					_, _ = io.WriteString(w, "partial body longer than the successful one")
					w.(http.Flusher).Flush()
					if stall {
						<-release
					}
					return
				}
				_, _ = io.WriteString(w, "complete")
			}))
			defer server.Close()
			defer close(release)
			response, err := requestURLWithRetry(server.URL)
			if err != nil {
				t.Fatal(err)
			}
			body := response.Body()
			if string(body) != "complete" || attempts.Load() != 2 || !slices.Equal(clock.delays, []time.Duration{time.Second}) {
				t.Errorf("body %q, attempts %d, delays %v", body, attempts.Load(), clock.delays)
			}
		})
	}
}

func TestRequestCanFinishAfterRetryDeadline(t *testing.T) {
	for _, retryFirst := range []bool{false, true} {
		t.Run(fmt.Sprintf("retry=%t", retryFirst), func(t *testing.T) {
			clock := useRetryClock(t)
			start := retryNow()
			var attempts atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if attempts.Add(1) == 1 && retryFirst {
					clock.advance(60 * time.Second)
					http.Error(w, "unavailable", http.StatusServiceUnavailable)
					return
				}
				for _, part := range []string{"slow ", "download"} {
					clock.advance(45 * time.Second)
					_, _ = io.WriteString(w, part)
					w.(http.Flusher).Flush()
				}
			}))
			defer server.Close()
			response, err := requestURLWithRetry(server.URL)
			if err != nil {
				t.Fatal(err)
			}
			body := response.Body()
			if string(body) != "slow download" || retryNow().Sub(start) <= retryTimeBudget {
				t.Errorf("body %q, elapsed %v", body, retryNow().Sub(start))
			}
		})
	}
}

func TestRequestLogsEachFailedAttempt(t *testing.T) {
	useRetryClock(t)
	var output bytes.Buffer
	logger := log.StandardLogger()
	previousOutput, previousFormatter := logger.Out, logger.Formatter
	log.SetOutput(&output)
	log.SetFormatter(&log.JSONFormatter{DisableTimestamp: true})
	t.Cleanup(func() { log.SetOutput(previousOutput); log.SetFormatter(previousFormatter) })
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer server.Close()
	_, err := requestURLWithRetry(server.URL)
	if err == nil {
		t.Fatal("request unexpectedly succeeded")
	}
	lines := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(lines) != 7 {
		t.Fatalf("got %d log lines, want one for each of 7 failures: %s", len(lines), output.String())
	}
	for i, line := range lines {
		var entry struct {
			URL     string `json:"url"`
			Error   string `json:"error"`
			Attempt int    `json:"attempt"`
			Message string `json:"msg"`
		}
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatal(err)
		}
		decision := "retrying in"
		if i == len(lines)-1 {
			decision = "not retrying: remaining time budget"
		}
		if entry.URL != server.URL || !strings.Contains(entry.Error, "HTTP 503") || entry.Attempt != i+1 || !strings.Contains(entry.Message, decision) {
			t.Errorf("unexpected failure log: %s", line)
		}
	}
}

func TestRetryableRequestErrors(t *testing.T) {
	for _, tt := range []struct {
		err  error
		want bool
	}{
		{&net.OpError{Op: "dial", Err: errors.New("connection refused")}, true},
		{&net.DNSError{Err: "lookup failed"}, true},
		{os.ErrDeadlineExceeded, true},
		{io.EOF, true}, {io.ErrUnexpectedEOF, true},
		{errors.New("unsupported protocol scheme"), false},
	} {
		t.Run(tt.err.Error(), func(t *testing.T) {
			for _, err := range []error{tt.err, &url.Error{Op: "Get", URL: "https://example.invalid", Err: tt.err}} {
				if got := retryableRequestError(err); got != tt.want {
					t.Errorf("retryableRequestError(%v) = %v, want %v", err, got, tt.want)
				}
			}
		})
	}
}

func TestRequestBodyErrorsIncludeURL(t *testing.T) {
	useRetryClock(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "1000")
		_, _ = io.WriteString(w, "partial")
	}))
	defer server.Close()

	_, err := requestURLWithRetry(server.URL)
	if !errors.Is(err, io.ErrUnexpectedEOF) || !strings.Contains(err.Error(), server.URL) {
		t.Errorf("error = %v, want the URL and a wrapped unexpected EOF", err)
	}
}
