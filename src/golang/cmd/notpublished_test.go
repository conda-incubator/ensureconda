package cmd

import (
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
)

// Make the successful metadata lookup report no builds for this platform.
func unpublishMicromamba(t *testing.T) {
	t.Helper()
	client := httpClient()
	transport := client.Transport
	client.Transport = roundTripper(func(r *http.Request) (*http.Response, error) {
		response, err := transport.RoundTrip(r)
		if err == nil && strings.HasSuffix(r.URL.Path, "/micromamba/files") {
			response.Body.Close()
			response.StatusCode, response.Status = 200, "200 OK"
			response.Body = io.NopCloser(strings.NewReader("[]"))
		}
		return response, err
	})
}

func TestInstallMicromambaTellsUnpublishedFromFailedDownload(t *testing.T) {
	subdir, err := PlatformSubdir()
	if err != nil {
		t.Fatal(err)
	}

	t.Run("not published", func(t *testing.T) {
		useSitePath(t)
		refuseDownloads(t)
		unpublishMicromamba(t)

		_, err := InstallMicromamba()

		var notPublished *notPublishedError
		if !errors.As(err, &notPublished) {
			t.Fatalf("InstallMicromamba() error = %v, want a notPublishedError", err)
		}
		if want := "micromamba is not published for " + subdir; err.Error() != want {
			t.Errorf("InstallMicromamba() error = %q, want %q", err, want)
		}
	})

	t.Run("published", func(t *testing.T) {
		useSitePath(t)
		refuseDownloads(t)
		// A failed metadata lookup stays an HTTP error.

		_, err := InstallMicromamba()

		var status *httpStatusError
		if !errors.As(err, &status) || status.statusCode != 404 {
			t.Fatalf("InstallMicromamba() error = %v, want the HTTP 404", err)
		}
	})
}

// logged returns the messages collected so far.
func logged(messages logMessages) []string {
	var all []string
	for {
		select {
		case message := <-messages:
			all = append(all, message)
		default:
			return all
		}
	}
}

func TestEnsureCondaFallsBackWhenMicromambaIsNotPublished(t *testing.T) {
	subdir, err := PlatformSubdir()
	if err != nil {
		t.Fatal(err)
	}
	useSitePath(t)
	// Nothing to find elsewhere.
	t.Setenv("PATH", "")
	requests := refuseDownloads(t)
	unpublishMicromamba(t)
	messages := captureLog(t)

	_, err = EnsureConda(false, true, false, true, false)

	var status *httpStatusError
	if !errors.As(err, &status) {
		t.Errorf("EnsureConda() error = %v, want the refused conda-standalone download", err)
	}
	if n := requests.Load(); n != 3 {
		t.Errorf("made %d requests, want latest and metadata for micromamba, then conda-standalone", n)
	}
	want := "micromamba is not published for " + subdir + "; trying conda-standalone instead"
	if got := logged(messages); !slices.Contains(got, want) {
		t.Errorf("logged %q, want %q among it", got, want)
	}
}

func TestEnsureCondaFindsCondaWhenMicromambaIsNotPublished(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the stand-in for conda is a shell script")
	}
	useSitePath(t)
	conda := filepath.Join(t.TempDir(), "conda")
	if err := os.WriteFile(conda, []byte("#!/bin/sh\necho conda 25.1.1\n"), 0755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", filepath.Dir(conda))
	refuseDownloads(t)
	unpublishMicromamba(t)

	got, err := EnsureConda(false, true, true, false, false)

	if err != nil || got != conda {
		t.Errorf("EnsureConda() = %v, %v; want %v", got, err, conda)
	}
}

func TestEnsureCondaReportsUnpublishedMicromambaWithNothingLeftToTry(t *testing.T) {
	useSitePath(t)
	t.Setenv("PATH", "")
	requests := refuseDownloads(t)
	unpublishMicromamba(t)

	_, err := EnsureConda(false, true, false, false, false)

	var notPublished *notPublishedError
	if !errors.As(err, &notPublished) {
		t.Errorf("EnsureConda() error = %v, want a notPublishedError", err)
	}
	if n := requests.Load(); n != 2 {
		t.Errorf("made %d requests, want 2", n)
	}
}
