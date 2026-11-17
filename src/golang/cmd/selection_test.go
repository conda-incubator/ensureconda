package cmd

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
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
			for _, message := range logged(messages) {
				if tt.status == http.StatusNotFound && strings.Contains(message, "request failed") {
					t.Errorf("expected metadata fallback to handle the 404; logged %q", message)
				}
				if strings.HasPrefix(message, "micromamba: latest") {
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
