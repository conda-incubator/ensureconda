package cmd

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"sync"
	"time"

	"github.com/go-resty/resty/v2"
)

// Limit eager allocation from Content-Length; larger bodies can still grow.
const maxPreallocatedBody = 128 << 20

// Timeouts matching the Python implementation. readTimeout limits inactivity,
// not the total download time, so slow downloads that keep making progress
// can finish.
var (
	connectTimeout = 10 * time.Second
	readTimeout    = 60 * time.Second
)

// anacondaAPIBaseURL is a variable so that tests can use a local server.
var anacondaAPIBaseURL = "https://api.anaconda.org"

// httpClient returns the client shared by all downloads, created on first use,
// so that requests reuse connections instead of each building a transport.
var httpClient = sync.OnceValue(newHTTPClient)

// newHTTPClient returns a client that gives up on connections that can't be
// established within connectTimeout, or that deliver no data for readTimeout.
// The custom dialer disables Go's automatic HTTP/2 upgrade; HTTP/1.1 is enough
// for the few requests made per run, and keeps the inactivity timeout simple.
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

// httpStatusError is a response with a status other than 2xx.
type httpStatusError struct {
	url        string
	statusCode int
	status     string
}

func (e *httpStatusError) Error() string {
	return fmt.Sprintf("GET %s: HTTP %s", e.url, e.status)
}

// checkStatus returns an error unless the response has a 2xx status.
func checkStatus(resp *resty.Response, rawURL string) error {
	if !resp.IsSuccess() {
		return &httpStatusError{url: rawURL, statusCode: resp.StatusCode(), status: resp.Status()}
	}
	return nil
}

// requestURL checks the status before reading the body, so incomplete error
// bodies cannot hide a permanent HTTP error.
func requestURL(rawURL string) (*resty.Response, error) {
	resp, err := httpClient().R().SetDoNotParseResponse(true).Get(rawURL)
	if resp != nil && resp.RawBody() != nil {
		defer resp.RawBody().Close()
	}
	if err != nil {
		return resp, err
	}
	if err := checkStatus(resp, rawURL); err != nil {
		return resp, err
	}
	var body bytes.Buffer
	if length := resp.RawResponse.ContentLength; length > 0 && length <= maxPreallocatedBody {
		// ReadFrom needs MinRead bytes of spare capacity for its final read.
		body.Grow(int(length) + bytes.MinRead)
	}
	_, err = body.ReadFrom(resp.RawBody())
	resp.SetBody(body.Bytes())
	if err != nil {
		err = &url.Error{Op: "Get", URL: rawURL, Err: err}
	}
	return resp, err
}
