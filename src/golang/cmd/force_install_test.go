package cmd

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"
)

// serveForceInstall serves tiny packages containing shell scripts that report
// version 25.1 unless overridden by test environment variables, and checks
// the download lock on every request. The USTAR archive
// was compressed with Python's bz2 because Go has no bzip2 writer.
func serveForceInstall(t *testing.T) *[]string {
	t.Helper()
	archive, err := hex.DecodeString("425a683931415926535921d216ac0000f5df8aca907c03ff903f279f00fe679e6a0000801008083000f8039800000000000000039800000000000000011520a3c469a8c9a7a8069a0c4d346d234f50635351e95373066220584a2898f0d7b58da07a0929686651200f23addae095c4ee71373098ed6a663756c5f69c6e010bec057d7311abdf5e5e272391908eba832b0898e04d0927723a5d267731088d0d9a4e20c4ef763637c9cb1e1b0d65f225a5bc52d8147c3112b2b06a666f0c1a5bc3ae876db083fa6e0d91175a1fd3baa3a643405c42d739c1deeea4e54f7fa20525f48d8e16b849de12b346453396e690b9c6517363d6887f8bb9229c284810e90b5600")
	if err != nil {
		t.Fatal(err)
	}
	subdir, err := PlatformSubdir()
	if err != nil {
		t.Fatal(err)
	}
	condaVersion := os.Getenv("ENSURECONDA_TEST_CONDA_VERSION")
	if condaVersion == "" {
		condaVersion = "25.1"
	}
	microVersion := os.Getenv("ENSURECONDA_TEST_MICROMAMBA_VERSION")
	if microVersion == "" {
		microVersion = "25.1"
	}
	listing, err := json.Marshal([]AnacondaPkg{{
		Attrs: AnacondaPkgAttr{Subdir: subdir}, Version: condaVersion,
		DownloadUrl: "//example.invalid/conda.tar.bz2", Sha256: sha256Hex(archive),
	}})
	if err != nil {
		t.Fatal(err)
	}
	var requests []string
	client := &http.Client{Transport: roundTripper(func(r *http.Request) (*http.Response, error) {
		requests = append(requests, r.URL.String())
		lockName := "conda_exe_install.lock"
		if strings.Contains(r.URL.Path, "micromamba") {
			lockName = "micromamba_install.lock"
		}
		if held, err := lockIsHeld(filepath.Join(sitePath(), lockName)); err != nil || !held {
			t.Fatalf("request without %s: held=%v, error=%v", lockName, held, err)
		}
		if strings.HasSuffix(r.URL.Path, "/latest") {
			return &http.Response{StatusCode: 307, Status: "307 Temporary Redirect", Request: r, Header: http.Header{"Location": {"https://conda.anaconda.org/conda-forge/" + subdir + "/micromamba-" + microVersion + "-0.tar.bz2"}}, Body: http.NoBody}, nil
		}
		body := archive
		if strings.HasSuffix(r.URL.Path, "/files") {
			body = listing
			if strings.Contains(r.URL.Path, "micromamba") {
				body, _ = json.Marshal([]AnacondaPkg{{Attrs: AnacondaPkgAttr{Subdir: subdir}, Version: microVersion, DownloadUrl: "//conda.anaconda.org/conda-forge/" + subdir + "/micromamba-" + microVersion + "-0.tar.bz2"}})
			}
		}
		return &http.Response{
			StatusCode: 200, Status: "200 OK", Request: r,
			Body: io.NopCloser(bytes.NewReader(body)),
		}, nil
	})}
	previous := httpClient
	httpClient = func() *http.Client { return client }
	t.Cleanup(func() { httpClient = previous })
	return &requests
}

func TestForceInstallReplacesFreshCache(t *testing.T) {
	for _, tool := range []string{"micromamba", "conda_standalone"} {
		t.Run(tool, func(t *testing.T) {
			useSitePath(t)
			requests := serveForceInstall(t)
			target := targetExeFilename(tool)
			if err := os.WriteFile(target, []byte("old executable"), 0755); err != nil {
				t.Fatal(err)
			}
			install := installCondaStandalone
			lockName := "conda_exe_install.lock"
			wantRequests := 4 // listing and archive for each invocation
			if tool == "micromamba" {
				install = installMicromambaForPlatform
				lockName = "micromamba_install.lock"
				wantRequests = 4
			}
			for range 2 {
				got, err := install(true)
				if err != nil || got != target {
					t.Fatalf("force install = %q, %v; want %q", got, err, target)
				}
				if data, err := os.ReadFile(target); err != nil || !bytes.Contains(data, []byte("25.1")) {
					t.Fatalf("target contains %q, error %v; want the new executable", data, err)
				}
			}
			if len(*requests) != wantRequests {
				t.Errorf("made %d requests, want %d", len(*requests), wantRequests)
			}
			if held, err := lockIsHeld(filepath.Join(sitePath(), lockName)); err != nil || held {
				t.Errorf("download lock was not released: held=%v, error=%v", held, err)
			}
		})
	}
}

func TestForceInstallRetriesDownloadUnderLock(t *testing.T) {
	for _, tool := range []string{"micromamba", "conda_standalone"} {
		t.Run(tool, func(t *testing.T) {
			useSitePath(t)
			clock := useRetryClock(t)
			serveForceInstall(t)
			target := targetExeFilename(tool)
			if err := os.WriteFile(target, []byte("old executable"), 0755); err != nil {
				t.Fatal(err)
			}
			install := installCondaStandalone
			lockName := "conda_exe_install.lock"
			if tool == "micromamba" {
				install = installMicromambaForPlatform
				lockName = "micromamba_install.lock"
			}
			client := httpClient()
			transport := client.Transport
			attempts := 0
			client.Transport = roundTripper(func(r *http.Request) (*http.Response, error) {
				if !strings.HasSuffix(r.URL.Path, "/files") && !strings.HasSuffix(r.URL.Path, "/latest") {
					attempts++
					if attempts == 1 {
						if held, err := lockIsHeld(filepath.Join(sitePath(), lockName)); err != nil || !held {
							t.Errorf("failed request without download lock: held=%v, error=%v", held, err)
						}
						return &http.Response{
							StatusCode: 503, Status: "503 Service Unavailable", Request: r,
							Header: http.Header{"Retry-After": {"3"}}, Body: http.NoBody,
						}, nil
					}
				}
				return transport.RoundTrip(r)
			})
			got, err := install(true)
			if err != nil || got != target {
				t.Fatalf("force install = %q, %v; want %q", got, err, target)
			}
			if attempts != 2 || len(clock.delays) != 1 || clock.delays[0] != 3*time.Second {
				t.Errorf("made %d download attempts with delays %v; want two attempts with Retry-After honored", attempts, clock.delays)
			}
			if data, err := os.ReadFile(target); err != nil || !bytes.Contains(data, []byte("25.1")) {
				t.Fatalf("target contains %q, error %v; want the new executable", data, err)
			}
		})
	}
}

// existingForceTools puts usable tools both in the data directory and on PATH.
func existingForceTools(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("the stand-in executables are shell scripts")
	}
	bin := t.TempDir()
	t.Setenv("PATH", bin)
	for _, dir := range []string{sitePath(), bin} {
		for _, tool := range []string{"mamba", "micromamba", "conda", "conda_standalone"} {
			version := "25.0"
			if strings.HasPrefix(tool, "conda") {
				version = "conda " + version
			}
			if err := os.WriteFile(filepath.Join(dir, tool), []byte("#!/bin/sh\necho "+version+"\n"), 0755); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func TestForceInstallSkipsExistingTools(t *testing.T) {
	for _, tool := range []string{"micromamba", "conda_standalone"} {
		t.Run(tool, func(t *testing.T) {
			useSitePath(t)
			existingForceTools(t)
			serveForceInstall(t)
			got, err := ensureCondaWithMetadata(true, tool == "micromamba", true, true, false, true)
			if err != nil || (got == nil || got.Path != targetExeFilename(tool)) {
				t.Fatalf("ensureCondaWithMetadata(force) = %q, %v; want %s", got, err, tool)
			}
			if data, err := os.ReadFile(got.Path); err != nil || !bytes.Contains(data, []byte("25.1")) {
				t.Fatalf("target contains %q, error %v; want the new executable", data, err)
			}
		})
	}
}

func TestForceInstallDoesNotFallBackToExistingTools(t *testing.T) {
	for _, standalone := range []bool{true, false} {
		t.Run(fmt.Sprintf("standalone=%t", standalone), func(t *testing.T) {
			useSitePath(t)
			existingForceTools(t)
			requests := refuseDownloads(t)
			unpublishMicromamba(t)
			old, err := os.ReadFile(targetExeFilename("micromamba"))
			if err != nil {
				t.Fatal(err)
			}
			got, err := ensureCondaWithMetadata(true, true, true, standalone, false, true)
			if got != nil || err == nil {
				t.Fatalf("ensureCondaWithMetadata(force) = %q, %v; want a failed download", got, err)
			}
			wantRequests := int32(2)
			if standalone {
				wantRequests = 3
			} else {
				var notPublished *notPublishedError
				if !errors.As(err, &notPublished) {
					t.Errorf("error = %v, want notPublishedError", err)
				}
			}
			if requests.Load() != wantRequests {
				t.Errorf("made %d requests, want %d", requests.Load(), wantRequests)
			}
			if data, err := os.ReadFile(targetExeFilename("micromamba")); err != nil || !bytes.Equal(data, old) {
				t.Error("failed download changed the existing executable")
			}
		})
	}
}

func TestForceInstallCLI(t *testing.T) {
	tests := []struct {
		name  string
		args  []string
		want  string
		fails bool
	}{
		{"ordinary discovery", nil, "mamba", false},
		{"force micromamba", []string{"--force-install"}, "micromamba", false},
		{"force standalone", []string{"--force-install", "--no-micromamba"}, "conda_standalone", false},
		{"no install", []string{"--force-install", "--no-install"}, "cannot be combined with --no-install", true},
		{"no installable tools", []string{"--force-install", "--no-micromamba", "--no-conda-exe"}, "requires --micromamba or --conda-exe", true},
	}
	for _, tt := range tests {
		for _, asJSON := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/json=%t", tt.name, asJSON), func(t *testing.T) {
				useSitePath(t)
				if !tt.fails {
					existingForceTools(t)
				}
				args := append([]string{"-test.run=^TestForceInstallCLIHelper$", "--"}, tt.args...)
				if asJSON {
					args = append(args, "--json")
				}
				command := exec.Command(os.Args[0], args...)
				command.Env = append(os.Environ(), "ENSURECONDA_TEST_FORCE_SITE="+sitePath())
				var stderr bytes.Buffer
				command.Stderr = &stderr
				out, err := command.Output()
				if (err != nil) != tt.fails {
					t.Fatalf("CLI error = %v, stdout %s, stderr %s; want failure=%v", err, out, &stderr, tt.fails)
				}
				if tt.fails {
					if !strings.Contains(stderr.String(), tt.want) || strings.Contains(stderr.String(), "panic:") {
						t.Errorf("stderr %q, want %q without a panic", &stderr, tt.want)
					}
					if len(out) != 0 {
						t.Errorf("failed resolution printed output: %q", out)
					}
					return
				}
				wantVersion := "25.0"
				if slices.Contains(tt.args, "--force-install") {
					wantVersion = "25.1"
				}
				wantPath := targetExeFilename(tt.want)
				if asJSON {
					var got condaExecutable
					if err := json.Unmarshal(out, &got); err != nil {
						t.Fatalf("invalid JSON %q: %v", out, err)
					}
					want := condaExecutable{Path: wantPath, Flavor: tt.want, Version: wantVersion}
					if got != want {
						t.Errorf("JSON output = %v, want %v", got, want)
					}
				} else if string(out) != wantPath {
					t.Errorf("output %q, want %q", out, wantPath)
				}
				if data, err := os.ReadFile(wantPath); err != nil || !bytes.Contains(data, []byte(wantVersion)) {
					t.Errorf("returned executable contains %q, error %v; want version %s", data, err, wantVersion)
				}
			})
		}
	}
}

func TestForceStandaloneChecksumFailurePreservesCache(t *testing.T) {
	useSitePath(t)
	serveForceInstall(t)
	target := targetExeFilename("conda_standalone")
	old := []byte("old executable")
	if err := os.WriteFile(target, old, 0755); err != nil {
		t.Fatal(err)
	}
	client := httpClient()
	transport := client.Transport
	client.Transport = roundTripper(func(r *http.Request) (*http.Response, error) {
		response, err := transport.RoundTrip(r)
		if err == nil && !strings.HasSuffix(r.URL.Path, "/files") {
			_ = response.Body.Close()
			response.Body = io.NopCloser(strings.NewReader("corrupt archive"))
		}
		return response, err
	})
	if _, err := installCondaStandalone(true); err == nil || !strings.Contains(err.Error(), "SHA-256 mismatch") {
		t.Fatalf("force install error = %v; want checksum failure", err)
	}
	if data, err := os.ReadFile(target); err != nil || !bytes.Equal(data, old) {
		t.Errorf("checksum failure changed the cached executable to %q, error %v", data, err)
	}
}

func TestForceInstallCLIReportsVersionRejection(t *testing.T) {
	for _, rejectStandalone := range []bool{false, true} {
		for _, asJSON := range []bool{false, true} {
			t.Run(fmt.Sprintf("rejectStandalone=%t/json=%t", rejectStandalone, asJSON), func(t *testing.T) {
				useSitePath(t)
				existingForceTools(t)
				condaVersion := "25.1"
				if rejectStandalone {
					condaVersion = "0.1.0"
				}
				args := []string{"-test.run=^TestForceInstallCLIHelper$", "--", "--force-install", "--verbosity", "0"}
				if asJSON {
					args = append(args, "--json")
				}
				command := exec.Command(os.Args[0], args...)
				command.Env = append(os.Environ(),
					"ENSURECONDA_TEST_FORCE_SITE="+sitePath(),
					"ENSURECONDA_TEST_MICROMAMBA_VERSION=0.1.0",
					"ENSURECONDA_TEST_CONDA_VERSION="+condaVersion,
				)
				var stderr bytes.Buffer
				command.Stderr = &stderr
				out, err := command.Output()
				if (err != nil) != rejectStandalone {
					t.Fatalf("CLI error = %v, stdout %s, stderr %s; want failure=%t", err, out, &stderr, rejectStandalone)
				}
				microMessage := "satisfies >=" + DefaultMinMambaVersion + "; newest available is 0.1.0"
				if !strings.Contains(stderr.String(), microMessage) {
					t.Errorf("stderr %q, want %q", &stderr, microMessage)
				}
				condaMessage := "satisfies >=" + DefaultMinCondaVersion + "; newest available is 0.1.0"
				if rejectStandalone {
					if !strings.Contains(stderr.String(), condaMessage) || !strings.Contains(stderr.String(), "Could not find compatible executable") {
						t.Errorf("stderr %q, want standalone rejection and final failure", &stderr)
					}
					if len(out) != 0 {
						t.Errorf("failed resolution printed a path: %q", out)
					}
				} else if asJSON {
					var got condaExecutable
					if err := json.Unmarshal(out, &got); err != nil {
						t.Fatalf("invalid fallback JSON %q: %v", out, err)
					}
					want := condaExecutable{Path: targetExeFilename("conda_standalone"), Flavor: "conda_standalone", Version: "25.1"}
					if got != want {
						t.Errorf("fallback JSON = %v, want %v", got, want)
					}
				} else if string(out) != targetExeFilename("conda_standalone") {
					t.Errorf("fallback output = %q, want conda-standalone", out)
				}
			})
		}
	}
}

func TestForceInstallCLIHelper(t *testing.T) {
	TestSitePath = os.Getenv("ENSURECONDA_TEST_FORCE_SITE")
	if TestSitePath == "" {
		return
	}
	serveForceInstall(t)
	for i, arg := range os.Args {
		if arg == "--" {
			rootCmd.SetArgs(os.Args[i+1:])
			break
		}
	}
	if err := Execute("test"); err != nil {
		t.Fatal(err)
	}
}
