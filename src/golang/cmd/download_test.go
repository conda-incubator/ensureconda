package cmd

import (
	"archive/zip"
	"bytes"
	"compress/bzip2"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/klauspost/compress/zstd"
)

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// testArchive contains bin/tool with the contents "binary". Go has no bzip2
// writer, so use a tiny USTAR archive compressed with Python's bz2.compress.
func testArchive(t *testing.T, name string) []byte {
	t.Helper()
	bz2, err := hex.DecodeString("425a6839314159265359f34ef6af00006e7b80c98000044000e780004070259e2008082000543484c98118469b4d41248832190321a1f7311c841edc84234ee7035ce38a043031d4de27b08e1c82e36bc9556b88e374f8198538c226d35422203f177245385090f34ef6af")
	if err != nil {
		t.Fatal(err)
	}
	if strings.HasSuffix(name, ".tar.bz2") {
		return bz2
	}
	var buf bytes.Buffer
	zipWriter := zip.NewWriter(&buf)
	entry, err := zipWriter.Create("pkg-conda-standalone-test.tar.zst")
	if err != nil {
		t.Fatal(err)
	}
	zstWriter, err := zstd.NewWriter(entry)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(zstWriter, bzip2.NewReader(bytes.NewReader(bz2))); err != nil {
		t.Fatal(err)
	}
	if err := zstWriter.Close(); err != nil {
		t.Fatal(err)
	}
	if err := zipWriter.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestArchiveDownloadBeforeExtraction(t *testing.T) {
	for _, name := range []string{"package.tar.bz2", "package.conda"} {
		for _, verify := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/verify=%t", name, verify), func(t *testing.T) {
				payload := testArchive(t, name)
				target := filepath.Join(t.TempDir(), "tool")
				if err := os.WriteFile(target, []byte("original"), 0755); err != nil {
					t.Fatal(err)
				}
				var attempts atomic.Int32
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if body, err := os.ReadFile(target); err != nil || string(body) != "original" {
						t.Errorf("target changed before successful download: %q, %v", body, err)
					}
					attempts.Add(1)
					_, _ = w.Write(payload)
				}))
				defer server.Close()
				digest := ""
				if verify {
					digest = sha256Hex(payload)
				}
				got, err := downloadAndUnpackArchive(server.URL+"/"+name, digest, map[string]string{"bin/tool": target})
				if err != nil || got != target || attempts.Load() != 1 {
					t.Fatalf("download = %q, %v; attempts %d", got, err, attempts.Load())
				}
				if body, err := os.ReadFile(target); err != nil || string(body) != "binary" {
					t.Errorf("target = %q, %v; want binary", body, err)
				}
			})
		}
	}
}

func TestArchiveDownloadDoesNotExtractAfterFailure(t *testing.T) {
	for _, name := range []string{"package.tar.bz2", "package.conda"} {
		for _, failure := range []string{"404", "truncated", "checksum", "invalid archive"} {
			t.Run(name+"/"+failure, func(t *testing.T) {
				payload := testArchive(t, name)
				target := filepath.Join(t.TempDir(), "tool")
				var attempts atomic.Int32
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					attempts.Add(1)
					switch failure {
					case "404":
						http.NotFound(w, r)
						return
					case "truncated":
						w.Header().Set("Content-Length", fmt.Sprint(len(payload)+1000))
					case "invalid archive":
						_, _ = io.WriteString(w, "invalid archive")
						return
					}
					_, _ = w.Write(payload)
				}))
				defer server.Close()
				digest := ""
				if failure == "checksum" {
					digest = sha256Hex([]byte("different archive"))
				}
				_, err := downloadAndUnpackArchive(server.URL+"/"+name, digest, map[string]string{"bin/tool": target})
				if err == nil {
					t.Fatal("download unexpectedly succeeded")
				}
				wantAttempts := int32(1)
				if attempts.Load() != wantAttempts {
					t.Errorf("got %d attempts, want %d", attempts.Load(), wantAttempts)
				}
				if _, err := os.Stat(target); !os.IsNotExist(err) {
					t.Errorf("target was installed after a failed download: %v", err)
				}
			})
		}
	}
}

func TestVerifySha256(t *testing.T) {
	data := []byte("archive")
	digest := sha256Hex(data)

	if err := verifySha256(data, digest); err != nil {
		t.Errorf("verifySha256() with matching digest: %v", err)
	}
	if err := verifySha256(data, strings.ToUpper(digest)); err != nil {
		t.Errorf("verifySha256() with uppercase digest: %v", err)
	}
	if err := verifySha256(data, sha256Hex([]byte("other"))); err == nil {
		t.Error("verifySha256() accepted a mismatched digest")
	}
}

func TestAnacondaPkgDecodesSha256(t *testing.T) {
	var pkgs AnacondaPkgs
	data := `[{"sha256": "abc123"}, {"sha256": null}, {}]`
	if err := json.Unmarshal([]byte(data), &pkgs); err != nil {
		t.Fatal(err)
	}
	for i, want := range []string{"abc123", "", ""} {
		if pkgs[i].Sha256 != want {
			t.Errorf("pkgs[%d].Sha256 = %q, want %q", i, pkgs[i].Sha256, want)
		}
	}
}

func TestDownloadRejectsSha256Mismatch(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("not the expected archive"))
	}))
	defer server.Close()

	for _, name := range []string{"package.conda", "package.tar.bz2"} {
		t.Run(name, func(t *testing.T) {
			target := filepath.Join(t.TempDir(), "conda_standalone")
			_, err := downloadAndUnpackArchive(
				server.URL+"/"+name,
				sha256Hex([]byte("expected archive")),
				map[string]string{"standalone_conda/conda.exe": target},
			)
			if err == nil || !strings.Contains(err.Error(), "SHA-256 mismatch") {
				t.Fatalf("downloadAndUnpackArchive() error = %v, want a SHA-256 mismatch", err)
			}
			if _, statErr := os.Stat(target); !os.IsNotExist(statErr) {
				t.Errorf("target was written despite the mismatch: %v", statErr)
			}
		})
	}
}
