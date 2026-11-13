package cmd

import (
	"archive/tar"
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/gofrs/flock"
	log "github.com/sirupsen/logrus"
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

// useSitePath points the install location at a temporary directory.
func useSitePath(t *testing.T) {
	t.Helper()
	previous := TestSitePath
	TestSitePath = t.TempDir()
	t.Cleanup(func() { TestSitePath = previous })
}

// logMessages is a logrus hook that collects the message of each entry.
type logMessages chan string

func (m logMessages) Levels() []log.Level { return log.AllLevels }

func (m logMessages) Fire(entry *log.Entry) error {
	m <- entry.Message
	return nil
}

// next returns the next message, failing the test if none is logged in time.
func (m logMessages) next(t *testing.T) string {
	t.Helper()
	select {
	case message := <-m:
		return message
	case <-time.After(30 * time.Second):
		t.Fatal("timed out waiting for a log message")
		return ""
	}
}

// captureLog collects what the rest of the test logs at the default verbosity.
func captureLog(t *testing.T) logMessages {
	t.Helper()
	messages := make(logMessages, 100)
	logger := log.StandardLogger()
	previousLevel := logger.GetLevel()
	logger.SetLevel(log.InfoLevel)
	previousHooks := logger.ReplaceHooks(log.LevelHooks{})
	logger.AddHook(messages)
	t.Cleanup(func() {
		logger.ReplaceHooks(previousHooks)
		logger.SetLevel(previousLevel)
	})
	return messages
}

// expectStillWaiting fails the test if done reports a result within a moment.
// A caller that doesn't wait for its lock is done almost at once.
func expectStillWaiting(t *testing.T, done <-chan error) {
	t.Helper()
	select {
	case err := <-done:
		t.Fatalf("returned (error %v) while the lock was held", err)
	case <-time.After(100 * time.Millisecond):
	}
}

func TestLockInstallReportsOnlyWhenWaiting(t *testing.T) {
	useSitePath(t)
	messages := captureLog(t)

	unlock, err := lockInstall("test_install.lock")
	if err != nil {
		t.Fatalf("lockInstall() error = %v", err)
	}
	// Also if the test fails, so that the second caller doesn't wait forever.
	t.Cleanup(unlock)
	if len(messages) != 0 {
		t.Errorf("taking a free lock logged %q, want nothing", <-messages)
	}

	// A second caller has to wait for the first, and says so.
	done := make(chan error, 1)
	go func() {
		unlockSecond, err := lockInstall("test_install.lock")
		if err == nil {
			unlockSecond()
		}
		done <- err
	}()
	if message := messages.next(t); !strings.Contains(message, "waiting for another process to finish downloading") {
		t.Errorf("logged %q, want a notice about waiting", message)
	}
	expectStillWaiting(t, done)

	unlock()
	if err := <-done; err != nil {
		t.Fatalf("lockInstall() error = %v after the lock was released", err)
	}
	if message := messages.next(t); !strings.Contains(message, "lock acquired after") {
		t.Errorf("logged %q, want a notice that the lock was acquired", message)
	}
}

func TestExtractTarFilesReportsWaitingForLock(t *testing.T) {
	messages := captureLog(t)
	target := filepath.Join(t.TempDir(), "tool")
	// As another process that is writing the executable.
	writer := flock.New(target + ".lock")
	if locked, err := writer.TryLock(); err != nil || !locked {
		t.Fatalf("TryLock() = %v, %v; want the lock", locked, err)
	}
	t.Cleanup(func() { _ = writer.Unlock() })

	archive := tarWith(t, "bin/tool", []byte("binary"))
	done := make(chan error, 1)
	go func() {
		_, err := extractTarFiles(archive, map[string]string{"bin/tool": target})
		done <- err
	}()
	if message := messages.next(t); !strings.Contains(message, "waiting for another process to finish writing tool") {
		t.Errorf("logged %q, want a notice about waiting", message)
	}
	expectStillWaiting(t, done)

	if err := writer.Unlock(); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatalf("extractTarFiles() error = %v after the lock was released", err)
	}
	if message := messages.next(t); !strings.Contains(message, "lock acquired after") {
		t.Errorf("logged %q, want a notice that the lock was acquired", message)
	}
}
