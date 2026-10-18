package cmd

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
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
