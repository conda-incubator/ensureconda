package cmd

import (
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/gofrs/flock"
	log "github.com/sirupsen/logrus"
)

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
