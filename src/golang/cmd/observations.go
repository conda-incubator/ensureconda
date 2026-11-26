package cmd

import (
	"errors"
	"os/exec"
	"path/filepath"
)

// fingerprint identifies a file and its state.
type fingerprint struct {
	dev, ino           uint64
	size, mtime, ctime int64
}

type observation struct {
	fingerprint fingerprint
	output      []byte
	err         error
}

// observer reuses the --version results of one call for files that don't
// change during it. Reuse assumes that what a wrapper or script runs doesn't
// change either. A nil observer runs every probe.
type observer struct {
	entries map[string]observation
}

func newObserver() *observer {
	return &observer{entries: map[string]observation{}}
}

func observationKey(path string) string {
	if absolute, err := filepath.Abs(path); err == nil {
		return absolute
	}
	return path
}

func (o *observer) versionOutput(executable string) ([]byte, error) {
	if o == nil {
		return exec.Command(executable, "--version").Output()
	}
	key := observationKey(executable)
	current, known := fileFingerprint(executable)
	// exec.Command looks up a bare name on PATH, so it may run another file.
	known = known && filepath.Base(executable) != executable
	if recorded, found := o.entries[key]; known && found && recorded.fingerprint == current {
		return recorded.output, recorded.err
	}
	output, err := exec.Command(executable, "--version").Output()
	var exited *exec.ExitError
	// A failure to start, such as a busy text file, may not happen again.
	if known && (err == nil || errors.As(err, &exited)) {
		// A change during the probe moves ctime past this record.
		o.entries[key] = observation{current, output, err}
	}
	return output, err
}

// detach removes the result for path, returning it if the file still matches it.
func (o *observer) detach(path string) *observation {
	if o == nil {
		return nil
	}
	key := observationKey(path)
	recorded, found := o.entries[key]
	delete(o.entries, key)
	if current, known := fileFingerprint(path); !found || !known || recorded.fingerprint != current {
		return nil
	}
	return &recorded
}

// attach keeps a detached result for the file it was renamed to at path.
func (o *observer) attach(path string, recorded *observation) {
	if o == nil || recorded == nil {
		return
	}
	after, known := fileFingerprint(path)
	moved := recorded.fingerprint
	// A rename updates ctime only.
	moved.ctime = after.ctime
	if known && moved == after {
		o.entries[observationKey(path)] = observation{after, recorded.output, recorded.err}
	}
}
