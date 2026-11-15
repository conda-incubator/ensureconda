package cmd

import (
	"errors"
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
