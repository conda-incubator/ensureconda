package cmd

import (
	"bytes"
	"compress/gzip"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestRequestReadsResponseBodies(t *testing.T) {
	for _, mode := range []string{"known length", "chunked", "gzip", "empty", "oversized length"} {
		t.Run(mode, func(t *testing.T) {
			payload := bytes.Repeat([]byte("archive"), 4096)
			if mode == "empty" {
				payload = nil
			}
			wire := payload
			if mode == "gzip" {
				var compressed bytes.Buffer
				writer := gzip.NewWriter(&compressed)
				if _, err := writer.Write(payload); err != nil {
					t.Fatal(err)
				}
				if err := writer.Close(); err != nil {
					t.Fatal(err)
				}
				wire = compressed.Bytes()
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch mode {
				case "chunked":
					w.(http.Flusher).Flush()
				case "oversized length":
					w.Header().Set("Content-Length", "9223372036854775807")
				default:
					w.Header().Set("Content-Length", strconv.Itoa(len(wire)))
				}
				if mode == "gzip" {
					w.Header().Set("Content-Encoding", "gzip")
				}
				_, _ = w.Write(wire)
			}))
			defer server.Close()

			response, err := requestURL(server.URL)
			if mode == "oversized length" {
				if !errors.Is(err, io.ErrUnexpectedEOF) {
					t.Fatalf("error = %v, want unexpected EOF without allocating the advertised length", err)
				}
			} else if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(response.Body(), payload) {
				t.Errorf("body length = %d, want the %d-byte payload", len(response.Body()), len(payload))
			}
		})
	}
}

// useAnacondaAPI points computeCandidates at server for the duration of the test.
func useAnacondaAPI(t *testing.T, server *httptest.Server) {
	t.Helper()
	previous := anacondaAPIBaseURL
	anacondaAPIBaseURL = server.URL
	t.Cleanup(func() { anacondaAPIBaseURL = previous })
}

// useReadTimeout shortens the inactivity timeout for the duration of the test.
func useReadTimeout(t *testing.T, timeout time.Duration) {
	t.Helper()
	previous := readTimeout
	readTimeout = timeout
	t.Cleanup(func() { readTimeout = previous })
}

func TestComputeCandidatesReportsHTTPStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "slow down", http.StatusTooManyRequests)
	}))
	defer server.Close()
	useAnacondaAPI(t, server)

	_, err := computeCandidates("conda-forge", "linux-64")
	if err == nil || !strings.Contains(err.Error(), "HTTP 429") {
		t.Fatalf("computeCandidates() error = %v, want HTTP 429", err)
	}
}

func TestDownloadReportsHTTPStatus(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	defer server.Close()

	for _, name := range []string{"package.conda", "package.tar.bz2"} {
		t.Run(name, func(t *testing.T) {
			target := filepath.Join(t.TempDir(), "conda_standalone")
			_, err := downloadAndUnpackArchive(
				server.URL+"/"+name, "", map[string]string{"standalone_conda/conda.exe": target},
			)
			if err == nil || !strings.Contains(err.Error(), "HTTP 404") {
				t.Fatalf("downloadAndUnpackArchive() error = %v, want HTTP 404", err)
			}
		})
	}
}

func TestRequestTimesOutWhenServerStalls(t *testing.T) {
	useReadTimeout(t, 200*time.Millisecond)
	release := make(chan struct{})
	var attempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts.Add(1)
		if r.URL.Path == "/body.conda" {
			// Send the headers and part of the body, then stall.
			w.Header().Set("Content-Length", "1000")
			_, _ = w.Write([]byte("partial"))
			w.(http.Flusher).Flush()
		}
		<-release
	}))
	defer server.Close()
	defer close(release)

	for _, path := range []string{"/headers.conda", "/body.conda"} {
		t.Run(path, func(t *testing.T) {
			attempts.Store(0)
			start := time.Now()
			_, err := requestURL(server.URL + path)
			if err == nil || !strings.Contains(err.Error(), "timeout") {
				t.Fatalf("requestURL() error = %v, want a timeout", err)
			}
			if attempts.Load() != 1 {
				t.Errorf("made %d requests, want one", attempts.Load())
			}
			if elapsed := time.Since(start); elapsed > 5*time.Second {
				t.Errorf("gave up after %v, want about the 200ms inactivity timeout", elapsed)
			}
		})
	}
}

func TestSlowDownloadOutlastsInactivityTimeout(t *testing.T) {
	useReadTimeout(t, 300*time.Millisecond)
	body := `[{"version": "1.0", "download_url": "//example.invalid/conda-standalone-1.0.conda",` +
		` "attrs": {"subdir": "linux-64", "build": "h0_0", "build_number": 0, "timestamp": 0}}]`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Deliver the body in pieces over about a second, well beyond the
		// inactivity timeout, but never pausing for that long.
		w.Header().Set("Content-Type", "application/json")
		for i := 0; i < len(body); i += len(body)/10 + 1 {
			_, _ = w.Write([]byte(body[i:min(i+len(body)/10+1, len(body))]))
			w.(http.Flusher).Flush()
			time.Sleep(100 * time.Millisecond)
		}
	}))
	defer server.Close()
	useAnacondaAPI(t, server)

	candidates, err := computeCandidates("conda-forge", "linux-64")
	if err != nil {
		t.Fatalf("computeCandidates() error = %v", err)
	}
	if len(candidates) != 1 {
		t.Errorf("computeCandidates() returned %d candidates, want 1", len(candidates))
	}
}
