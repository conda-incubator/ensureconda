package cmd

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	log "github.com/sirupsen/logrus"
)

// Only scheduling retries is bounded; an active download may finish later.
const retryTimeBudget = 70 * time.Second

// Limit eager allocation from Content-Length; larger bodies can still grow.
const maxPreallocatedBody = 128 << 20

var (
	retryNow   = time.Now
	retrySleep = time.Sleep
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

// httpClient returns the client shared by all downloads, created on first use,
// so that requests reuse connections instead of each building a transport.
var httpClient = sync.OnceValue(newHTTPClient)

// newHTTPClient returns a client that gives up on connections that can't be
// established within connectTimeout, or that deliver no data for readTimeout.
// The custom dialer disables Go's automatic HTTP/2 upgrade; HTTP/1.1 is enough
// for the few requests made per run, and keeps the inactivity timeout simple.
func newHTTPClient() *http.Client {
	dialer := &net.Dialer{Timeout: connectTimeout, KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		OnProxyConnectResponse: func(_ context.Context, _ *url.URL, _ *http.Request, resp *http.Response) error {
			// net/http otherwise returns a plain error for rejected tunnels.
			// Treat these as connection failures, like Python's ProxyError.
			if resp.StatusCode != http.StatusOK {
				return &net.OpError{Op: "proxyconnect", Net: "tcp", Err: fmt.Errorf("HTTP %s", resp.Status)}
			}
			return nil
		},
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
	return &http.Client{Transport: transport}
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
func checkStatus(resp *http.Response, rawURL string) error {
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return &httpStatusError{url: rawURL, statusCode: resp.StatusCode, status: resp.Status}
	}
	return nil
}

// retryAfterDelay parses Retry-After seconds or an HTTP date. Malformed values
// and past dates do not extend the backoff. Saturate large values so they cannot
// overflow into a short wait and cause a premature retry.
func retryAfterDelay(value string, now time.Time) time.Duration {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0
	}
	// ParseUint can report overflow before reaching a trailing non-digit.
	// Validate the entire value before treating an overflow as a long delay.
	if strings.IndexFunc(value, func(r rune) bool { return r < '0' || r > '9' }) == -1 {
		seconds, err := strconv.ParseUint(value, 10, 64)
		const maxDelay = time.Duration(1<<63 - 1)
		if err != nil || seconds > uint64(maxDelay/time.Second) {
			return maxDelay
		}
		return time.Duration(seconds) * time.Second
	}
	if date, err := http.ParseTime(value); err == nil {
		return max(0, date.Sub(now))
	}
	return 0
}

func retryableRequestError(err error) bool {
	var status *httpStatusError
	if errors.As(err, &status) {
		switch status.statusCode {
		case 429, 500, 502, 503, 504:
			return true
		default:
			return false
		}
	}
	// url.Error implements net.Error even for malformed URLs or redirect loops.
	// Inspect its cause instead of treating every request error as transient.
	var urlError *url.Error
	if errors.As(err, &urlError) {
		err = urlError.Err
	}
	var networkError net.Error
	return errors.As(err, &networkError) || errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, io.EOF)
}

// requestURLWithRetry reads the entire body into the returned response, so
// that a truncated or stalled body is retried like any other failure.
func requestURLWithRetry(rawURL string) (*response, error) {
	return requestURLWithRetryMode(rawURL, false)
}

func requestURLWithRetryMode(rawURL string, headersOnly bool) (*response, error) {
	deadline := retryNow().Add(retryTimeBudget)
	backoff := time.Second
	for attempt := 1; ; attempt++ {
		resp, err := requestURLMode(rawURL, headersOnly)
		if err == nil {
			return resp, nil
		}
		var status *httpStatusError
		hasStatus := errors.As(err, &status)
		failure := log.WithFields(log.Fields{"url": rawURL, "error": err, "attempt": attempt})
		if !retryableRequestError(err) {
			failure.Warn("request failed; not retrying")
			return resp, err
		}
		delay := backoff
		source := ""
		now := retryNow()
		if hasStatus {
			if retryAfter := retryAfterDelay(resp.header.Get("Retry-After"), now); retryAfter > delay {
				delay = retryAfter
				source = fmt.Sprintf(" (Retry-After from HTTP %d)", status.statusCode)
			}
		}
		remaining := max(0, deadline.Sub(now))
		if delay > remaining {
			failure.Warnf("request failed; not retrying: remaining time budget %s, required wait %s%s", remaining, delay, source)
			return resp, err
		}
		failure.Warnf("request failed; retrying in %s%s", delay, source)
		retrySleep(delay)
		backoff *= 2
	}
}

// response holds the parts of an HTTP response that callers use.
type response struct {
	statusCode int
	status     string
	header     http.Header
	body       []byte
}

// requestURL checks the status before reading the body, so incomplete error
// bodies cannot hide a permanent HTTP error or its Retry-After header.
func requestURL(rawURL string) (*response, error) { return requestURLMode(rawURL, false) }

func getURL(rawURL string, headersOnly bool) (*http.Response, error) {
	client := httpClient()
	if !headersOnly {
		return client.Get(rawURL)
	}
	req, err := http.NewRequest(http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Transport.RoundTrip(req)
	if err != nil {
		return nil, &url.Error{Op: "Get", URL: rawURL, Err: err}
	}
	return resp, nil
}

func requestURLMode(rawURL string, headersOnly bool) (*response, error) {
	resp, err := getURL(rawURL, headersOnly)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	result := &response{statusCode: resp.StatusCode, status: resp.Status, header: resp.Header}
	if headersOnly {
		switch resp.StatusCode {
		case 301, 302, 303, 307, 308, http.StatusNotFound:
			return result, nil
		}
	}
	if err := checkStatus(resp, rawURL); err != nil {
		return result, err
	}
	if headersOnly {
		return result, nil
	}
	var body bytes.Buffer
	if length := resp.ContentLength; length > 0 && length <= maxPreallocatedBody {
		// ReadFrom needs MinRead bytes of spare capacity for its final read.
		body.Grow(int(length) + bytes.MinRead)
	}
	_, err = body.ReadFrom(resp.Body)
	result.body = body.Bytes()
	if err != nil {
		err = &url.Error{Op: "Get", URL: rawURL, Err: err}
	}
	return result, err
}
