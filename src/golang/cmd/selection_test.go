package cmd

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"

	pep440 "github.com/aquasecurity/go-pep440-version"
)

type headersOnlyBody struct {
	t      *testing.T
	closed bool
}

func (b *headersOnlyBody) Read([]byte) (int, error) {
	b.t.Fatal("latest lookup read the archive body")
	return 0, io.EOF
}
func (b *headersOnlyBody) Close() error { b.closed = true; return nil }

func TestLatestCandidateAndMetadataFallback(t *testing.T) {
	for _, tt := range []struct {
		name                    string
		status                  int
		location, minimum, want string
		metadata                bool
		missing                 bool
	}{
		{"latest fits", 307, "micromamba-2.10-0.tar.bz2", "1", "2.10", false, false},
		{"latest too old", 307, "micromamba-0.1-0.tar.bz2", "1", "2.10", true, false},
		{"no matching version", 307, "micromamba-2.10-0.tar.bz2", "3", "", true, false},
		{"latest missing", 404, "", "1", "2.10", true, false},
		{"unexpected body", 200, "", "1", "2.10", true, false},
		{"unparseable redirect", 307, "unknown", "1", "2.10", true, false},
		{"malformed redirect URL", 307, "bad%zz", "1", "2.10", true, false},
		{"missing build", 307, "micromamba-2.10-.tar.bz2", "1", "2.10", true, false},
		{"nothing published", 404, "", "1", "", true, true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			urls := []string{}
			body := &headersOnlyBody{t: t}
			client := &http.Client{Transport: roundTripper(func(r *http.Request) (*http.Response, error) {
				urls = append(urls, r.URL.String())
				if strings.HasSuffix(r.URL.Path, "/latest") {
					return &http.Response{StatusCode: tt.status, Status: http.StatusText(tt.status), Request: r, Body: body,
						Header: http.Header{"Location": {"//conda.anaconda.org/conda-forge/linux-64/" + tt.location}}}, nil
				}
				if !strings.HasSuffix(r.URL.Path, "/micromamba/files") {
					t.Fatalf("unexpected archive request: %s", r.URL)
				}
				packages := []AnacondaPkg{{Attrs: AnacondaPkgAttr{Subdir: "linux-64"}, Version: "2.10", DownloadUrl: "//example.invalid/micromamba-2.10-0.tar.bz2"}}
				if tt.missing {
					packages = nil
				}
				encoded, _ := json.Marshal(packages)
				return &http.Response{StatusCode: 200, Status: "200 OK", Request: r, Body: io.NopCloser(strings.NewReader(string(encoded)))}, nil
			})}
			previous := httpClient
			httpClient = func() *http.Client { return client }
			t.Cleanup(func() { httpClient = previous })
			messages := captureLog(t)
			candidate, err := selectMicromamba("https://micro.mamba.pm/api/micromamba/linux-64/latest", "linux-64", pep440.MustParse(tt.minimum))
			reasons := 0
			entries := logged(messages)
			if tt.status == http.StatusNotFound && !slices.Contains(entries, "micromamba: no build of the latest release for linux-64; checking release metadata") {
				t.Errorf("expected a platform-specific 404 diagnostic, got %q", entries)
			}
			for _, message := range entries {
				if tt.status == http.StatusNotFound && strings.Contains(message, "request failed") {
					t.Errorf("expected metadata fallback to handle the 404; logged %q", message)
				}
				if strings.HasSuffix(message, "; checking release metadata") {
					reasons++
				}
			}
			if want := map[bool]int{false: 0, true: 1}[tt.metadata]; reasons != want {
				t.Errorf("logged %d reasons for a metadata lookup, want %d", reasons, want)
			}
			if tt.want != "" {
				if err != nil || candidate.Version != tt.want {
					t.Fatalf("candidate=%v, %v; want %s", candidate, err, tt.want)
				}
			} else if tt.missing {
				var unpublished *notPublishedError
				if !errors.As(err, &unpublished) {
					t.Fatalf("want unpublished, got %v", err)
				}
			} else {
				var incompatible *noCompatibleVersionError
				if !errors.As(err, &incompatible) {
					t.Fatalf("want incompatible, got %v", err)
				}
			}
			wantRequests := 1
			if tt.metadata {
				wantRequests = 2
			}
			if len(urls) != wantRequests || !body.closed {
				t.Fatalf("requests=%v, latest body closed=%t", urls, body.closed)
			}
		})
	}
}

func TestLatestServerFailureDoesNotBecomeMetadataFallback(t *testing.T) {
	useRetryClock(t)
	attempts := 0
	client := &http.Client{Transport: roundTripper(func(r *http.Request) (*http.Response, error) {
		attempts++
		if !strings.HasSuffix(r.URL.Path, "/latest") {
			t.Fatalf("unexpected metadata fallback: %s", r.URL)
		}
		return &http.Response{StatusCode: 503, Status: "503 Service Unavailable", Request: r, Body: http.NoBody}, nil
	})}
	previous := httpClient
	httpClient = func() *http.Client { return client }
	t.Cleanup(func() { httpClient = previous })
	_, err := selectMicromamba("https://micro.mamba.pm/api/micromamba/linux-64/latest", "linux-64", pep440.MustParse("1"))
	var status *httpStatusError
	if !errors.As(err, &status) || status.statusCode != 503 || attempts < 2 {
		t.Fatalf("error=%v, attempts=%d", err, attempts)
	}
}

func TestValidateDownloadBeforeReplacingCache(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell fixtures; native Windows installs are covered by test_go_binaries.py")
	}
	for _, tt := range []struct {
		name, script, expected string
		succeeds               bool
		logs                   string
	}{
		{"compatible", "echo 2", "2", true, "micromamba: validated executable version 2"},
		{"packaging revision", "echo 2", "2.post1", true, "micromamba: validated executable version 2 (package version 2.post1)"},
		{"too old", "echo 1", "2", false, ""},
		{"cannot execute", "exit 7", "2", false, ""},
		{"unknown version", "echo nonsense", "2", false, ""},
	} {
		t.Run(tt.name, func(t *testing.T) {
			target := filepath.Join(t.TempDir(), "micromamba")
			if err := os.WriteFile(target, []byte("old"), 0755); err != nil {
				t.Fatal(err)
			}
			validation := &downloadValidation{flavor: "micromamba", minimum: pep440.MustParse("2"), expected: pep440.MustParse(tt.expected)}
			script := []byte("#!/bin/sh\n" + tt.script + "\n")
			messages := captureLog(t)
			_, err := extractTarFiles(tarWith(t, "bin/micromamba", script), map[string]string{"bin/micromamba": target}, validation)
			if (err == nil) != tt.succeeds {
				t.Fatalf("error=%v, want success=%t", err, tt.succeeds)
			}
			if tt.logs != "" {
				found := false
				for _, message := range logged(messages) {
					found = found || message == tt.logs
				}
				if !found {
					t.Fatalf("logged %q, want %q", logged(messages), tt.logs)
				}
			}
			var invalid *downloadedExecutableError
			if !tt.succeeds && !errors.As(err, &invalid) {
				t.Fatalf("error=%v, want a downloadedExecutableError", err)
			}
			if tt.name == "cannot execute" {
				var exit *exec.ExitError
				if !errors.As(err, &exit) || exit.ExitCode() != 7 {
					t.Fatalf("error=%v, want exit status 7", err)
				}
			}
			data, err := os.ReadFile(target)
			if err != nil {
				t.Fatal(err)
			}
			expected := string(script)
			if !tt.succeeds {
				expected = "old"
			}
			if string(data) != expected {
				t.Fatalf("cache contains %q, want %q", data, expected)
			}
			leftovers, _ := filepath.Glob(target + ".*.tmp*")
			if len(leftovers) != 0 {
				t.Fatalf("temporary files left: %v", leftovers)
			}
		})
	}
}
