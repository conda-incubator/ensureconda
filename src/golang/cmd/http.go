package cmd

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"time"

	"github.com/go-resty/resty/v2"
)

// Timeouts matching the Python implementation. readTimeout limits inactivity,
// not the total download time, so slow downloads that keep making progress
// can finish.
var (
	connectTimeout = 10 * time.Second
	readTimeout    = 60 * time.Second
)

// anacondaAPIBaseURL is a variable so that tests can use a local server.
var anacondaAPIBaseURL = "https://api.anaconda.org"

// newHTTPClient returns a client that gives up on connections that can't be
// established within connectTimeout, or that deliver no data for readTimeout.
func newHTTPClient() *resty.Client {
	dialer := &net.Dialer{Timeout: connectTimeout, KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			conn, err := dialer.DialContext(ctx, network, addr)
			if err != nil {
				return nil, err
			}
			return &idleTimeoutConn{Conn: conn, timeout: readTimeout}, nil
		},
		TLSHandshakeTimeout: connectTimeout,
		IdleConnTimeout:     90 * time.Second,
	}
	return resty.New().SetTransport(transport)
}

// idleTimeoutConn fails any read that receives no data within its timeout.
type idleTimeoutConn struct {
	net.Conn
	timeout time.Duration
}

func (c *idleTimeoutConn) Read(b []byte) (int, error) {
	if err := c.Conn.SetReadDeadline(time.Now().Add(c.timeout)); err != nil {
		return 0, err
	}
	return c.Conn.Read(b)
}

// checkStatus returns an error unless the response has a 2xx status.
func checkStatus(resp *resty.Response, url string) error {
	if !resp.IsSuccess() {
		return fmt.Errorf("GET %s: HTTP %s", url, resp.Status())
	}
	return nil
}
