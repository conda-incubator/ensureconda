package cmd

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

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

func TestDownloadTimesOutWhenServerStalls(t *testing.T) {
	useReadTimeout(t, 200*time.Millisecond)
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
			start := time.Now()
			_, err := downloadAndUnpackArchive(server.URL+path, "", map[string]string{})
			if err == nil || !strings.Contains(err.Error(), "timeout") {
				t.Fatalf("downloadAndUnpackArchive() error = %v, want a timeout", err)
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
