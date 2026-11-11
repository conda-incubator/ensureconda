package cmd

import (
	"archive/tar"
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"testing"

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

// lockIsHeld reports whether the lock file at lockPath is currently held.
func lockIsHeld(lockPath string) (bool, error) {
	probe := flock.New(lockPath)
	acquired, err := probe.TryLock()
	if err != nil {
		return false, err
	}
	// Unlocking also closes the probe's handle. Windows can't remove a test's
	// temporary directory while a file in it is open.
	return !acquired, probe.Unlock()
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
		if held, err := lockIsHeld(lockPath); err != nil || held {
			t.Errorf("%s is still locked after extracting (error %v)", lockPath, err)
		}
	}
}

func TestExtractTarFilesRemovesTemporaryFileOnError(t *testing.T) {
	// An archive whose entry promises more content than it holds.
	var buf bytes.Buffer
	w := tar.NewWriter(&buf)
	header := &tar.Header{Name: "bin/tool", Mode: 0644, Size: 1 << 20, Typeflag: tar.TypeReg}
	if err := w.WriteHeader(header); err != nil {
		t.Fatal(err)
	}
	if _, err := w.Write([]byte("truncated")); err != nil {
		t.Fatal(err)
	}
	// Not closed: closing would report the missing content instead.
	target := filepath.Join(t.TempDir(), "tool")

	_, err := extractTarFiles(tar.NewReader(&buf), map[string]string{"bin/tool": target})

	if err == nil {
		t.Fatal("extractTarFiles() succeeded on a truncated archive")
	}
	for _, path := range []string{target, target + ".tmp"} {
		if _, statErr := os.Stat(path); !os.IsNotExist(statErr) {
			t.Errorf("%s exists after a failed extraction (stat error %v)", path, statErr)
		}
	}
}
