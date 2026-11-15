package cmd

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"testing"
)

// useMicromambaSubdirs makes micromamba count as published for exactly the
// given platforms.
func useMicromambaSubdirs(t *testing.T, subdirs ...string) {
	t.Helper()
	previous := micromambaSubdirs
	micromambaSubdirs = map[string]bool{}
	for _, subdir := range subdirs {
		micromambaSubdirs[subdir] = true
	}
	t.Cleanup(func() { micromambaSubdirs = previous })
}

func TestMicromambaInstallError(t *testing.T) {
	notFound := &httpStatusError{url: "https://micro.mamba.pm", statusCode: 404, status: "404 Not Found"}
	serverError := &httpStatusError{url: "https://micro.mamba.pm", statusCode: 500, status: "500 Internal Server Error"}
	tests := []struct {
		name             string
		err              error
		subdir           string
		wantNotPublished bool
	}{
		{"404 for a platform without micromamba", notFound, "linux-s390x", true},
		{"404 for a platform with micromamba", notFound, "linux-64", false},
		{"other status", serverError, "linux-s390x", false},
		{"other failure", errors.New("connection refused"), "linux-s390x", false},
		{"success", nil, "linux-s390x", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := micromambaInstallError(tt.err, tt.subdir)

			var notPublished *notPublishedError
			if errors.As(got, &notPublished) != tt.wantNotPublished {
				t.Fatalf("micromambaInstallError() = %v, want a notPublishedError: %v", got, tt.wantNotPublished)
			}
			if tt.wantNotPublished {
				if want := "micromamba is not published for linux-s390x"; got.Error() != want {
					t.Errorf("micromambaInstallError() = %q, want %q", got, want)
				}
			} else if got != tt.err {
				t.Errorf("micromambaInstallError() = %v, want the error unchanged: %v", got, tt.err)
			}
		})
	}
}

func TestInstallMicromambaTellsUnpublishedFromFailedDownload(t *testing.T) {
	subdir, err := PlatformSubdir()
	if err != nil {
		t.Fatal(err)
	}

	t.Run("not published", func(t *testing.T) {
		useSitePath(t)
		refuseDownloads(t)
		useMicromambaSubdirs(t)

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
		useMicromambaSubdirs(t, subdir)

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
	useMicromambaSubdirs(t)
	messages := captureLog(t)

	_, err = EnsureConda(false, true, false, true, false)

	var status *httpStatusError
	if !errors.As(err, &status) {
		t.Errorf("EnsureConda() error = %v, want the refused conda-standalone download", err)
	}
	if n := requests.Load(); n != 2 {
		t.Errorf("made %d requests, want one for micromamba and one for conda-standalone", n)
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
	useMicromambaSubdirs(t)

	got, err := EnsureConda(false, true, true, false, false)

	if err != nil || got != conda {
		t.Errorf("EnsureConda() = %v, %v; want %v", got, err, conda)
	}
}

func TestEnsureCondaReportsUnpublishedMicromambaWithNothingLeftToTry(t *testing.T) {
	useSitePath(t)
	t.Setenv("PATH", "")
	requests := refuseDownloads(t)
	useMicromambaSubdirs(t)

	_, err := EnsureConda(false, true, false, false, false)

	var notPublished *notPublishedError
	if !errors.As(err, &notPublished) {
		t.Errorf("EnsureConda() error = %v, want a notPublishedError", err)
	}
	if n := requests.Load(); n != 1 {
		t.Errorf("made %d requests, want 1", n)
	}
}
