package cmd

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	pep440 "github.com/aquasecurity/go-pep440-version"
	log "github.com/sirupsen/logrus"
	"github.com/spf13/cobra"
)

func TestNormalizedVersion(t *testing.T) {
	for _, tt := range []struct{ input, want string }{
		{"2.9.0", "2.9.0"},
		{"v02.009.00", "2.9.0"},
		{"01!02.0RC01.post02.dev03", "1!2.0rc1.post2.dev3"},
		{"2.9alpha", "2.9a0"},
		{"2.9PREVIEW02", "2.9rc2"},
		{"2.9-rev", "2.9.post0"},
		{"2.9-03", "2.9.post3"},
		{"2.9DEV", "2.9.dev0"},
		{"2.9+Ubuntu_007-000.01abc", "2.9+ubuntu.7.0.01abc"},
		{"2.9+000123456789012345678901234567890", "2.9+123456789012345678901234567890"},
	} {
		t.Run(tt.input, func(t *testing.T) {
			if got := normalizedVersion(pep440.MustParse(tt.input)); got != tt.want {
				t.Errorf("normalizedVersion(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

func TestPrintExecutable(t *testing.T) {
	for _, path := range []string{`C:\a path\"tool"`, `C:\Users\José\𝄞\a&b<c>`} {
		t.Run(path, func(t *testing.T) {
			info := &condaExecutable{Path: path, Flavor: "conda_standalone", Version: "25.1.0"}
			for _, asJSON := range []bool{false, true} {
				var out bytes.Buffer
				command := &cobra.Command{}
				command.SetOut(&out)
				if err := printExecutable(command, info, asJSON); err != nil {
					t.Fatal(err)
				}
				if !asJSON {
					if out.String() != info.Path {
						t.Errorf("plain output = %q, want exactly %q", out.String(), info.Path)
					}
					continue
				}
				for _, b := range out.Bytes() {
					if b >= 0x80 {
						t.Fatalf("JSON must be ASCII-only, got %q", out.String())
					}
				}
				var got map[string]string
				if err := json.Unmarshal(out.Bytes(), &got); err != nil {
					t.Fatal(err)
				}
				if len(got) != 3 || got["path"] != info.Path || got["flavor"] != info.Flavor || got["version"] != info.Version {
					t.Errorf("JSON output = %v, want path, flavor and version from %v", got, info)
				}
			}
		})
	}
}

func TestPrintExecutableInvalidUTF8Path(t *testing.T) {
	info := &condaExecutable{Path: "/lat\xe9/micromamba", Flavor: "micromamba", Version: "2.9.0"}
	for _, asJSON := range []bool{false, true} {
		var out bytes.Buffer
		command := &cobra.Command{}
		command.SetOut(&out)
		err := printExecutable(command, info, asJSON)
		if asJSON {
			if err == nil || !strings.Contains(err.Error(), "not valid UTF-8") || out.Len() != 0 {
				t.Errorf("JSON output = %q, %v; want an error and no output", out.String(), err)
			}
		} else if err != nil || out.String() != info.Path {
			t.Errorf("plain output = %q, %v; want original path bytes %q", out.String(), err, info.Path)
		}
	}
}

func TestEnsureCondaMetadata(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the stand-in executables are shell scripts")
	}
	for _, tt := range []struct {
		name, flavor, output, version string
		probes                        int
	}{
		{"mamba v1", "mamba", "mamba 1.5.12\nconda 24.11.3\n", "1.5.12", 1},
		{"mamba v2", "mamba", "2.9.0\n", "2.9.0", 1},
		{"old mamba", "mamba", "mamba 0.5.0\nconda 4.9.0\n", "", 1},
		{"invalid mamba", "mamba", "mamba unknown\nconda 25.1.0\n", "", 1},
		{"micromamba", "micromamba", "v02.009.00+LOCAL_007\n", "2.9.0+local.7", 1},
		{"conda", "conda", "conda 25.1.0\r\n", "25.1.0", 1},
		{"standalone", "conda_standalone", "conda 25.1.0\n", "25.1.0", 1},
	} {
		t.Run(tt.name, func(t *testing.T) {
			useSitePath(t)
			t.Setenv("PATH", "")
			t.Setenv("TEST_VERSION_OUTPUT", tt.output)
			calls := filepath.Join(t.TempDir(), "calls")
			t.Setenv("TEST_VERSION_CALLS", calls)
			// The filename and output alone cannot identify the search branch.
			target := filepath.Join(t.TempDir(), "tool")
			script := "#!/bin/sh\necho probe >> \"$TEST_VERSION_CALLS\"\nprintf '%s' \"$TEST_VERSION_OUTPUT\"\n"
			if err := os.WriteFile(target, []byte(script), 0755); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(sitePath(), tt.flavor)
			if err := os.Symlink(target, path); err != nil {
				t.Fatal(err)
			}

			got, err := ensureCondaWithMetadata(tt.flavor == "mamba", tt.flavor == "micromamba", tt.flavor == "conda", tt.flavor == "conda_standalone", true, false)
			want := condaExecutable{Path: path, Flavor: tt.flavor, Version: tt.version}
			if tt.version == "" {
				if err != nil || got != nil {
					t.Fatalf("ensureCondaWithMetadata() = %v, %v; want no match", got, err)
				}
			} else if err != nil || got == nil || *got != want {
				t.Fatalf("ensureCondaWithMetadata() = %v, %v; want %v", got, err, want)
			}
			probes, err := os.ReadFile(calls)
			if err != nil {
				t.Fatal(err)
			}
			if n := strings.Count(string(probes), "probe\n"); n != tt.probes {
				t.Errorf("ran --version %d times, want %d", n, tt.probes)
			}
		})
	}
}

func TestEnsureCondaInstalledMetadata(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the stand-in executables are shell scripts")
	}
	// A USTAR archive compressed with Python's bz2.compress (Go has no bzip2
	// writer). Both bin/micromamba and standalone_conda/conda.exe contain:
	// #!/bin/sh
	// printf 'probe\n' >> "$TEST_VERSION_CALLS"
	// printf '%s\n' "$TEST_VERSION_OUTPUT"
	archive, err := hex.DecodeString("425a6839314159265359fc504b210000cddf94cc907e81ff812b25df04ff67de400400040008083000d868c00000000000000003489a11ea79200d3232190623264d1a69a182a9229e44da43469e48c80d0335193400d1ea7969c4b925f9812a0842cc9f299ba56c2dd5e42121c06c6656d85299c6d0d0acb980a5f06e15f2253c4edf22a4885bdd3a1d58e4a19929e2ceb927cecf7d630bb5ada8c85390ef69789697d797ad29584c6a6824a8d4a980fb5ad62c03ac555959f08d68ffe4956f22fc907b1206418992f004ab32bd80a24044a6288fc6dfa3cd79fa8a36aa5d7314632e65ca883fc5dc914e14243f1412c840")
	if err != nil {
		t.Fatal(err)
	}
	subdir, err := currentPlatformSubdir()
	if err != nil {
		t.Fatal(err)
	}
	listing, err := json.Marshal([]AnacondaPkg{{
		Attrs:       AnacondaPkgAttr{Subdir: subdir},
		Version:     "99.0", // Metadata must come from the executable, not the listing.
		DownloadUrl: "//example.invalid/package.tar.bz2",
		Sha256:      sha256Hex(archive),
	}})
	if err != nil {
		t.Fatal(err)
	}
	for _, tt := range []struct{ flavor, output, version string }{
		{"micromamba", "v02.009.00+LOCAL_007", "2.9.0+local.7"},
		{"conda_standalone", "conda v025.01RC01+LOCAL_007", "25.1rc1+local.7"},
	} {
		for _, forceInstall := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/force=%t", tt.flavor, forceInstall), func(t *testing.T) {
				useSitePath(t)
				t.Setenv("PATH", "")
				t.Setenv("ENSURECONDA_CONDA_STANDALONE_CHANNEL", "conda-forge")
				t.Setenv("TEST_VERSION_OUTPUT", tt.output)
				calls := filepath.Join(t.TempDir(), "calls")
				t.Setenv("TEST_VERSION_CALLS", calls)
				downloads := 0
				client := &http.Client{Transport: roundTripper(func(r *http.Request) (*http.Response, error) {
					if strings.HasSuffix(r.URL.Path, "/latest") {
						return &http.Response{StatusCode: 307, Status: "307 Temporary Redirect", Body: http.NoBody, Request: r, Header: http.Header{"Location": {"https://conda.anaconda.org/conda-forge/" + subdir + "/micromamba-99.0-0.tar.bz2"}}}, nil
					}
					body := archive
					if strings.HasSuffix(r.URL.Path, "/files") {
						body = listing
					} else {
						downloads++
					}
					return &http.Response{StatusCode: 200, Status: "200 OK", Body: io.NopCloser(bytes.NewReader(body)), Request: r}, nil
				})}
				previous := httpClient
				httpClient = func() *http.Client { return client }
				t.Cleanup(func() { httpClient = previous })

				if forceInstall {
					// Force must not probe or reuse the old cached executable.
					script := "#!/bin/sh\necho cached >> \"$TEST_VERSION_CALLS\"\necho 99.0\n"
					if err := os.WriteFile(targetExeFilename(tt.flavor), []byte(script), 0755); err != nil {
						t.Fatal(err)
					}
				}
				got, err := ensureCondaWithMetadata(false, tt.flavor == "micromamba", false, tt.flavor == "conda_standalone", false, forceInstall)
				want := condaExecutable{Path: targetExeFilename(tt.flavor), Flavor: tt.flavor, Version: tt.version}
				if err != nil || got == nil || *got != want {
					t.Fatalf("ensureCondaWithMetadata() = %v, %v; want %v", got, err, want)
				}
				if downloads != 1 {
					t.Errorf("downloaded %d archives, want one fresh installation", downloads)
				}
				probes, err := os.ReadFile(calls)
				if err != nil {
					t.Fatal(err)
				}
				if string(probes) != "probe\n" {
					t.Errorf("version probes = %q, want the staged validation only, reused for the installed file", probes)
				}
			})
		}
	}
}

func TestResolveExecutableWithVersionSkipsRejectedCandidates(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the stand-in executables are shell scripts")
	}
	for _, script := range []string{"exit 1", "echo unknown", "echo 0.1.0"} {
		t.Run(script, func(t *testing.T) {
			useSitePath(t)
			if err := os.WriteFile(filepath.Join(sitePath(), "micromamba"), []byte("#!/bin/sh\n"+script+"\n"), 0755); err != nil {
				t.Fatal(err)
			}
			dir := t.TempDir()
			t.Setenv("PATH", dir)
			check := executableWithMinVersion(nil, pep440.MustParse(DefaultMinMambaVersion), log.InfoLevel, "")
			if got, err := resolveExecutableWithVersion("micromamba", sitePath(), check); got != nil || err == nil {
				t.Fatalf("rejected candidate returned %v, %v", got, err)
			}
			path := filepath.Join(dir, "micromamba")
			if err := os.WriteFile(path, []byte("#!/bin/sh\necho 2.9.0\n"), 0755); err != nil {
				t.Fatal(err)
			}
			got, err := resolveExecutableWithVersion("micromamba", sitePath(), check)
			if err != nil || got == nil || got.Path != path || got.Version != "2.9.0" {
				t.Errorf("fallback returned %v, %v; want %s at version 2.9.0", got, err, path)
			}
		})
	}
}
