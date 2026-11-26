package cmd

import (
	"archive/tar"
	"bytes"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"sync/atomic"
	"syscall"
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

// fileNames returns the names of the entries in dir, sorted.
func fileNames(t *testing.T, dir string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	names := make([]string, len(entries))
	for i, entry := range entries {
		names[i] = entry.Name()
	}
	return names
}

func TestExtractTarFilesReleasesLock(t *testing.T) {
	target := filepath.Join(t.TempDir(), "tool")

	got, err := extractTarFiles(tarWith(t, "bin/tool", []byte("binary")), map[string]string{"bin/tool": target}, nil)
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
	dir := t.TempDir()
	target := filepath.Join(dir, "tool")

	_, err := extractTarFiles(tar.NewReader(&buf), map[string]string{"bin/tool": target}, nil)

	if err == nil {
		t.Fatal("extractTarFiles() succeeded on a truncated archive")
	}
	// Neither the executable nor its temporary file, whatever that was called.
	if names, want := fileNames(t, dir), []string{"tool.lock"}; !slices.Equal(names, want) {
		t.Errorf("the directory holds %v after a failed extraction, want %v", names, want)
	}
}

func TestExtractTarFilesReportsReplacementFailure(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "tool")
	// A nonempty directory makes the final rename fail on every platform.
	if err := os.Mkdir(target, 0700); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(target, "existing")
	if err := os.WriteFile(marker, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}

	_, err := extractTarFiles(tarWith(t, "bin/tool", []byte("binary")), map[string]string{"bin/tool": target}, nil)
	if err == nil || !strings.Contains(err.Error(), "could not replace executable "+target) {
		t.Fatalf("replacement error = %v, want context naming the executable", err)
	}
	var cause *os.LinkError
	if !errors.As(err, &cause) {
		t.Errorf("replacement error %v lost the underlying rename error", err)
	}
	if runtime.GOOS == "windows" && !strings.Contains(err.Error(), "close processes using it and retry") {
		t.Errorf("replacement error = %v, want an in-use hint on Windows", err)
	}
	if content, err := os.ReadFile(marker); err != nil || string(content) != "keep" {
		t.Errorf("existing target changed: %q, %v", content, err)
	}
	if names, want := fileNames(t, dir), []string{"tool", "tool.lock"}; !slices.Equal(names, want) {
		t.Errorf("directory holds %v after failed replacement, want %v", names, want)
	}
}

func TestExtractTarFilesLeavesOtherTemporaryFilesAlone(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "tool")
	// Releases that locked the temporary file wrote it here. They don't take the
	// lock on the target, so one of them may be writing it at the same time.
	otherWriter := target + ".tmp"
	if err := os.WriteFile(otherWriter, []byte("partial"), 0644); err != nil {
		t.Fatal(err)
	}

	if _, err := extractTarFiles(tarWith(t, "bin/tool", []byte("binary")), map[string]string{"bin/tool": target}, nil); err != nil {
		t.Fatalf("extractTarFiles() error = %v", err)
	}

	if content, err := os.ReadFile(otherWriter); err != nil || string(content) != "partial" {
		t.Errorf("the other writer's file holds %q, %v; want it untouched", content, err)
	}
	if names, want := fileNames(t, dir), []string{"tool", "tool.lock", "tool.tmp"}; !slices.Equal(names, want) {
		t.Errorf("the directory holds %v after extracting, want %v", names, want)
	}
}

func TestExtractTarFilesRemovesLeftoverTemporaryFiles(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "tool")
	// As an extraction leaves behind when it is killed while writing.
	leftover := target + ".4Z65VGLRJJTMOIYW5CPAR3EMTQ.tmp"
	if err := os.WriteFile(leftover, []byte("partial"), 0755); err != nil {
		t.Fatal(err)
	}

	if _, err := extractTarFiles(tarWith(t, "bin/tool", []byte("binary")), map[string]string{"bin/tool": target}, nil); err != nil {
		t.Fatalf("extractTarFiles() error = %v", err)
	}

	if names, want := fileNames(t, dir), []string{"tool", "tool.lock"}; !slices.Equal(names, want) {
		t.Errorf("the directory holds %v after extracting, want %v", names, want)
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

	_, unlock, err := lockInstall("tool", "test_install.lock")
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
		_, unlockSecond, err := lockInstall("tool", "test_install.lock")
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
		_, err := extractTarFiles(archive, map[string]string{"bin/tool": target}, nil)
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

func TestRetryWhileBeingDeleted(t *testing.T) {
	previousDelay := lockOpenRetryDelay
	lockOpenRetryDelay = 0
	t.Cleanup(func() { lockOpenRetryDelay = previousDelay })

	sharingViolation := &os.PathError{Op: "open", Path: "test.lock", Err: syscall.Errno(32)}
	accessDenied := &os.PathError{Op: "open", Path: "test.lock", Err: os.ErrPermission}
	diskFull := &os.PathError{Op: "open", Path: "test.lock", Err: syscall.ENOSPC}
	tests := []struct {
		name string
		goos string
		// The error of each call in turn; the last one repeats.
		results   []error
		wantCalls int
		wantErr   error
	}{
		{"the lock file is free", "windows", []error{nil}, 1, nil},
		{"the deletion finishes", "windows", []error{sharingViolation, accessDenied, nil}, 3, nil},
		{"the lock file stays out of reach", "windows", []error{accessDenied}, lockOpenAttempts, accessDenied},
		{"another failure", "windows", []error{diskFull}, 1, diskFull},
		{"another system", "linux", []error{accessDenied}, 1, accessDenied},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			calls := 0
			err := retryWhileBeingDeleted(tt.goos, func() error {
				result := tt.results[min(calls, len(tt.results)-1)]
				calls++
				return result
			})
			if err != tt.wantErr || calls != tt.wantCalls {
				t.Errorf("retryWhileBeingDeleted() = %v after %d calls, want %v after %d", err, calls, tt.wantErr, tt.wantCalls)
			}
		})
	}
}

func TestLockFileWhileLockFileIsDeleted(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("only Windows refuses to open a file that is being deleted")
	}
	lockPath := filepath.Join(t.TempDir(), "test.lock")
	logger := log.StandardLogger()
	previousOutput := logger.Out
	logger.SetOutput(io.Discard)
	t.Cleanup(func() { logger.SetOutput(previousOutput) })

	// As the Python implementation on Windows: take the lock, release it, and
	// delete the lock file, over and over.
	stop := make(chan struct{})
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		for {
			select {
			case <-stop:
				return
			default:
			}
			peer := flock.New(lockPath)
			if locked, _ := peer.TryLock(); locked {
				_ = peer.Unlock()
			}
			_ = os.Remove(lockPath)
		}
	}()
	t.Cleanup(func() {
		close(stop)
		<-stopped
	})

	// In CI, two seconds of this were enough for a plain TryLock to fail.
	for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); {
		unlock, err := lockFile(lockPath, "testing")
		if err != nil {
			t.Fatalf("lockFile() error = %v", err)
		}
		unlock()
	}
}

func TestInstallersHoldDownloadLock(t *testing.T) {
	useRetryClock(t)
	tests := []struct {
		name string
		// The Python implementation uses the same names, so the two coordinate.
		lockName string
		install  func(serverURL string) (string, error)
	}{
		{"micromamba", "micromamba_install.lock", func(serverURL string) (string, error) {
			return installMicromamba(nil, serverURL+"/latest", false)
		}},
		{"conda-standalone", "conda_exe_install.lock", func(string) (string, error) {
			return InstallCondaStandalone()
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			useSitePath(t)
			lockPath := filepath.Join(sitePath(), tt.lockName)
			var requests, requestsUnderLock atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				if held, err := lockIsHeld(lockPath); err == nil && held {
					requestsUnderLock.Add(1)
				}
				http.NotFound(w, r)
			}))
			defer server.Close()
			// InstallCondaStandalone starts by asking the Anaconda API.
			useAnacondaAPI(t, server)

			if _, err := tt.install(server.URL); err == nil {
				t.Fatal("the install succeeded, want the refused download to fail")
			}
			if n, locked := requests.Load(), requestsUnderLock.Load(); n == 0 || locked != n {
				t.Errorf("%d of %d requests were made while holding %s, want all of at least one", locked, n, tt.lockName)
			}
			if held, err := lockIsHeld(lockPath); err != nil || held {
				t.Errorf("%s is still locked after the failed download (error %v)", tt.lockName, err)
			}
		})
	}
}

func TestInstallersReuseUsableInstall(t *testing.T) {
	useRetryClock(t)
	if runtime.GOOS == "windows" {
		t.Skip("the stand-ins for micromamba and conda-standalone are shell scripts")
	}
	tests := []struct {
		name    string
		exeName string
		version string
		install func(serverURL string) (string, error)
	}{
		{"micromamba", "micromamba", "2.0.0", func(serverURL string) (string, error) {
			return installMicromamba(nil, serverURL+"/latest", false)
		}},
		{"conda-standalone", "conda_standalone", "conda 25.1.1", func(string) (string, error) {
			return InstallCondaStandalone()
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			useSitePath(t)
			var requests atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				http.NotFound(w, r)
			}))
			defer server.Close()
			useAnacondaAPI(t, server)

			// As if another process installed it while this one waited.
			target := targetExeFilename(tt.exeName)
			if err := os.WriteFile(target, []byte("#!/bin/sh\necho "+tt.version+"\n"), 0755); err != nil {
				t.Fatal(err)
			}
			got, err := tt.install(server.URL)
			if err != nil || got != target {
				t.Fatalf("install = %v, %v; want %v", got, err, target)
			}
			if n := requests.Load(); n != 0 {
				t.Errorf("made %d requests, want none for a usable install", n)
			}
		})
	}
}

func TestInstallersDoNotRepeatSearchRejections(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the stand-ins for micromamba and conda-standalone are shell scripts")
	}
	for _, tt := range []struct {
		name, exeName, version string
		install                func() (string, error)
	}{
		{"micromamba", "micromamba", "0.1.0", func() (string, error) {
			return installMicromamba(nil, "https://micro.mamba.pm/api/micromamba/linux-64/latest", false)
		}},
		{"conda-standalone", "conda_standalone", "conda 0.1.0", InstallCondaStandalone},
	} {
		t.Run(tt.name, func(t *testing.T) {
			useSitePath(t)
			refuseDownloads(t)
			target := targetExeFilename(tt.exeName)
			if err := os.WriteFile(target, []byte("#!/bin/sh\necho "+tt.version+"\n"), 0755); err != nil {
				t.Fatal(err)
			}
			messages := captureLog(t)
			if _, err := tt.install(); err == nil {
				t.Fatal("the install succeeded, want the refused download to fail")
			}
			for len(messages) > 0 {
				if message := <-messages; strings.Contains(message, "Rejected") {
					t.Errorf("the locked recheck logged %q at the default verbosity", message)
				}
			}
		})
	}
}

// refuseDownloads answers every request of the shared HTTP client with a 404,
// and returns the number of requests made.
func refuseDownloads(t *testing.T) *atomic.Int32 {
	t.Helper()
	useRetryClock(t)
	var requests atomic.Int32
	client := &http.Client{Transport: roundTripper(func(r *http.Request) (*http.Response, error) {
		requests.Add(1)
		return &http.Response{StatusCode: http.StatusNotFound, Status: "404 Not Found", Body: http.NoBody, Request: r}, nil
	})}
	previous := httpClient
	httpClient = func() *http.Client { return client }
	t.Cleanup(func() { httpClient = previous })
	return &requests
}

type roundTripper func(*http.Request) (*http.Response, error)

func (f roundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestEnsureCondaReplacesUnusableInstall(t *testing.T) {
	micromamba := func() (string, error) { return EnsureConda(false, true, false, false, false) }
	condaStandalone := func() (string, error) { return EnsureConda(false, false, false, true, false) }
	tests := []struct {
		name     string
		exeName  string
		ensure   func() (string, error)
		content  string
		mode     os.FileMode
		unixOnly bool
	}{
		{"micromamba/corrupt", "micromamba", micromamba, "not an executable", 0755, false},
		{"micromamba/older than the minimum version", "micromamba", micromamba, "#!/bin/sh\necho 0.1.0\n", 0755, true},
		{"micromamba/not executable", "micromamba", micromamba, "#!/bin/sh\necho 2.0.0\n", 0644, true},
		{"conda-standalone/corrupt", "conda_standalone", condaStandalone, "not an executable", 0755, false},
		{"conda-standalone/older than the minimum version", "conda_standalone", condaStandalone, "#!/bin/sh\necho conda 4.0.0\n", 0755, true},
		{"conda-standalone/not executable", "conda_standalone", condaStandalone, "#!/bin/sh\necho conda 25.1.1\n", 0644, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if tt.unixOnly && runtime.GOOS == "windows" {
				t.Skip("the stand-in is a shell script")
			}
			useSitePath(t)
			// Nothing to find elsewhere.
			t.Setenv("PATH", "")
			requests := refuseDownloads(t)
			// Newly written, as an executable that was installed today.
			if err := os.WriteFile(targetExeFilename(tt.exeName), []byte(tt.content), tt.mode); err != nil {
				t.Fatal(err)
			}

			_, err := tt.ensure()

			if err == nil {
				t.Error("EnsureConda() succeeded, want the refused download to fail")
			}
			wantRequests := int32(1)
			if tt.exeName == "micromamba" {
				wantRequests = 2
			}
			if n := requests.Load(); n != wantRequests {
				t.Errorf("made %d requests, want %d to replace the executable", n, wantRequests)
			}
		})
	}
}
