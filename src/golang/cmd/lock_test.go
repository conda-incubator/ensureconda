package cmd

import (
	"archive/tar"
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gofrs/flock"
)

// tarWith returns a reader positioned before a single regular file entry.
func tarWith(t *testing.T, name string, content []byte) *tar.Reader {
	t.Helper()
	var buf bytes.Buffer
	w := tar.NewWriter(&buf)
	header := &tar.Header{Name: name, Mode: 0644, Size: int64(len(content)), Typeflag: tar.TypeReg}
	if err := w.WriteHeader(header); err != nil {
		t.Fatal(err)
	}
	if _, err := w.Write(content); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return tar.NewReader(&buf)
}

// useSitePath points the install location at a temporary directory.
func useSitePath(t *testing.T) {
	t.Helper()
	previous := TestSitePath
	TestSitePath = t.TempDir()
	t.Cleanup(func() { TestSitePath = previous })
}

func TestExtractTarFilesReleasesLock(t *testing.T) {
	target := filepath.Join(t.TempDir(), "tool")

	got, err := extractTarFiles(tarWith(t, "bin/tool", []byte("binary")), map[string]string{"bin/tool": target})
	if err != nil {
		t.Fatalf("extractTarFiles() error = %v", err)
	}
	if got != target {
		t.Errorf("extractTarFiles() = %v, want %v", got, target)
	}
	if content, err := os.ReadFile(target); err != nil || string(content) != "binary" {
		t.Errorf("target content = %q, %v; want %q", content, err, "binary")
	}
	if st, err := os.Stat(target); err == nil && runtime.GOOS != "windows" && st.Mode()&0100 == 0 {
		t.Errorf("target mode = %v, want it executable", st.Mode())
	}
	// Earlier versions locked the temporary file instead, and never released it.
	for _, lockPath := range []string{target + ".lock", target + ".tmp.lock"} {
		probe := flock.New(lockPath)
		locked, err := probe.TryLock()
		// Release the probe before the temporary directory is removed, which
		// fails on Windows while a handle to a file in it is open.
		t.Cleanup(func() { _ = probe.Unlock() })
		if err != nil || !locked {
			t.Errorf("%s still locked after extracting (locked=%v, err=%v)", lockPath, locked, err)
		}
	}
}

func TestInstallMicromambaReusesFreshInstall(t *testing.T) {
	useSitePath(t)
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		http.NotFound(w, r)
	}))
	defer server.Close()

	// As if another process installed micromamba while this one waited.
	target := targetExeFilename("micromamba")
	if err := os.WriteFile(target, []byte("micromamba"), 0755); err != nil {
		t.Fatal(err)
	}
	got, err := installMicromamba(server.URL + "/latest")
	if err != nil || got != target {
		t.Fatalf("installMicromamba() = %v, %v; want %v", got, err, target)
	}
	if n := requests.Load(); n != 0 {
		t.Errorf("made %d requests, want none for a fresh install", n)
	}

	// A stale executable is downloaded again; the test server refuses.
	old := time.Now().Add(-2 * redownloadWhenOlder)
	if err := os.Chtimes(target, old, old); err != nil {
		t.Fatal(err)
	}
	if _, err := installMicromamba(server.URL + "/latest"); err == nil {
		t.Error("installMicromamba() succeeded, want the refused download to fail")
	}
	if n := requests.Load(); n != 1 {
		t.Errorf("made %d requests, want 1 for a stale install", n)
	}
}
