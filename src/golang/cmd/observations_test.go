package cmd

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
)

// standIn writes a script that records each run in runs.log next to it.
func standIn(t *testing.T, path, body string) {
	t.Helper()
	log := filepath.Join(filepath.Dir(path), "runs.log")
	script := "#!/bin/sh\necho run >> '" + log + "'\n" + body + "\n"
	if err := os.WriteFile(path, []byte(script), 0755); err != nil {
		t.Fatal(err)
	}
}

func runs(t *testing.T, dir string) int {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, "runs.log"))
	if os.IsNotExist(err) {
		return 0
	}
	if err != nil {
		t.Fatal(err)
	}
	return len(strings.Fields(string(data)))
}

func skipOnWindows(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the stand-ins are shell scripts")
	}
}

func TestObserverReusesUnchangedFile(t *testing.T) {
	skipOnWindows(t)
	dir := t.TempDir()
	micromamba := filepath.Join(dir, "micromamba")
	standIn(t, micromamba, "echo 2.9.0")

	obs := newObserver()
	for range 2 {
		if output, err := obs.versionOutput(micromamba); err != nil || string(output) != "2.9.0\n" {
			t.Fatalf("versionOutput() = %q, %v", output, err)
		}
	}
	if got := runs(t, dir); got != 1 {
		t.Errorf("ran %d times for one observer, want once", got)
	}
	// Without an observer, nothing is kept.
	var none *observer
	for range 2 {
		_, _ = none.versionOutput(micromamba)
	}
	if got := runs(t, dir); got != 3 {
		t.Errorf("ran %d times in total, want 3", got)
	}
}

func TestObserverRunsChangedFileAgain(t *testing.T) {
	skipOnWindows(t)
	for _, tt := range []struct{ name, first, second string }{
		{"changed between probes", "echo 2.9.0", "echo 2.10.0"},
		{"changed while probing", "echo '#' >> \"$0\"\necho 2.9.0", ""},
	} {
		t.Run(tt.name, func(t *testing.T) {
			dir := t.TempDir()
			micromamba := filepath.Join(dir, "micromamba")
			standIn(t, micromamba, tt.first)
			obs := newObserver()

			_, _ = obs.versionOutput(micromamba)
			if tt.second != "" {
				standIn(t, micromamba, tt.second)
			}
			_, _ = obs.versionOutput(micromamba)

			if got := runs(t, dir); got != 2 {
				t.Errorf("ran %d times, want twice", got)
			}
		})
	}
}

func TestObserverKeepsFailures(t *testing.T) {
	skipOnWindows(t)
	dir := t.TempDir()
	micromamba := filepath.Join(dir, "micromamba")
	standIn(t, micromamba, "exit 1")
	obs := newObserver()

	for range 2 {
		if _, err := obs.versionOutput(micromamba); err == nil {
			t.Fatal("versionOutput() succeeded, want the exit status")
		}
	}
	if got := runs(t, dir); got != 1 {
		t.Errorf("ran %d times, want once", got)
	}
}

func TestObserverDoesNotCachePathLookupAgainstLocalFile(t *testing.T) {
	skipOnWindows(t)
	work, bin := t.TempDir(), t.TempDir()
	t.Chdir(work)
	standIn(t, filepath.Join(work, "conda"), "echo conda 4.7")
	t.Setenv("PATH", bin)
	obs := newObserver()
	for _, version := range []string{"25.1", "25.2"} {
		standIn(t, filepath.Join(bin, "conda"), "echo conda "+version)
		output, err := obs.versionOutput("conda")
		if err != nil || string(output) != "conda "+version+"\n" {
			t.Fatalf("versionOutput() = %q, %v; want conda %s", output, err, version)
		}
	}
}

func TestObserverCarriesResultAcrossRename(t *testing.T) {
	skipOnWindows(t)
	for _, tt := range []struct {
		name, body, target, want string
		runs                     int
	}{
		{name: "renamed", body: "echo 2.9.0", want: "2.9.0\n", runs: 1},
		{name: "another file", body: "echo 2.9.0", target: "echo 2.10.0", want: "2.10.0\n", runs: 2},
		// Only the change time records the permission change.
		{name: "changed during validation", body: "chmod -x \"$0\"\necho 2.9.0", runs: 1},
	} {
		t.Run(tt.name, func(t *testing.T) {
			dir := t.TempDir()
			staged, target := filepath.Join(dir, "staged"), filepath.Join(dir, "micromamba")
			standIn(t, staged, tt.body)
			obs := newObserver()

			_, _ = obs.versionOutput(staged)
			validated := obs.detach(staged)
			if tt.target == "" {
				if err := os.Rename(staged, target); err != nil {
					t.Fatal(err)
				}
			} else {
				standIn(t, target, tt.target)
			}
			obs.attach(target, validated)
			output, err := obs.versionOutput(target)

			if tt.want == "" && err == nil {
				t.Errorf("accepted a file that can no longer run: %q", output)
			} else if tt.want != "" && string(output) != tt.want {
				t.Errorf("output %q, want %q", output, tt.want)
			}
			if got := runs(t, dir); got != tt.runs {
				t.Errorf("ran %d times, want %d", got, tt.runs)
			}
		})
	}
}

func TestEnsureCondaRunsEachUnchangedFileOnce(t *testing.T) {
	skipOnWindows(t)
	useSitePath(t)
	bin := t.TempDir()
	t.Setenv("PATH", bin)
	standIn(t, filepath.Join(bin, "micromamba"), "echo 0.1.0")
	standIn(t, filepath.Join(sitePath(), "micromamba"), "echo 0.1.0")
	serveForceInstall(t)

	got, err := EnsureConda(false, true, false, false, false)

	if err != nil || filepath.Dir(got) != sitePath() {
		t.Fatalf("EnsureConda() = %q, %v; want the installed micromamba", got, err)
	}
	// The installer's check under the download lock reuses the search's result.
	if cached := runs(t, sitePath()); cached != 1 {
		t.Errorf("the cached micromamba ran %d times, want once", cached)
	}
	if onPath := runs(t, bin); onPath != 1 {
		t.Errorf("the micromamba on PATH ran %d times, want once", onPath)
	}
}

func TestRelativePathEntriesProbeTheFileThatWasFound(t *testing.T) {
	skipOnWindows(t)
	useSitePath(t)
	work, bin := t.TempDir(), t.TempDir()
	t.Chdir(work)
	standIn(t, filepath.Join(bin, "conda"), "echo conda 4.7.12")
	standIn(t, filepath.Join(work, "conda"), "echo conda 25.1.0")
	for _, path := range []string{bin + ":.", bin + ":", ".:empty", ":empty"} {
		t.Run("PATH="+path, func(t *testing.T) {
			t.Setenv("PATH", path)
			got, err := EnsureConda(false, false, true, false, true)
			if want := filepath.Join(work, "conda"); err != nil || got != want {
				t.Fatalf("EnsureConda() = %q, %v; want %q", got, err, want)
			}
		})
	}
}

func TestOverlappingCallsKeepTheirOwnObservations(t *testing.T) {
	skipOnWindows(t)
	useSitePath(t)
	bin := t.TempDir()
	started, release, version := filepath.Join(bin, "started"), filepath.Join(bin, "release"), filepath.Join(bin, "version")
	// PATH holds only the stand-ins below.
	sleep, err := exec.LookPath("sleep")
	if err != nil {
		t.Fatal(err)
	}
	standIn(t, filepath.Join(bin, "conda"), fmt.Sprintf("echo ready > %q\nwhile [ ! -f %q ]; do %q 0.01; done\necho conda 25.1.0", started, release, sleep))
	// A wrapper whose answer depends on another file.
	standIn(t, filepath.Join(bin, "micromamba"), fmt.Sprintf("read v < %q\necho \"$v\"", version))
	if err := os.WriteFile(version, []byte("2.9.0\n"), 0644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin)
	done := make(chan error, 1)
	go func() {
		_, err := EnsureConda(false, false, true, false, true)
		done <- err
	}()
	t.Cleanup(func() {
		_ = os.WriteFile(release, nil, 0644)
		select {
		case err := <-done:
			if err != nil {
				t.Error(err)
			}
		case <-time.After(5 * time.Second):
			t.Error("the overlapping call did not finish")
		}
	})
	for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(time.Millisecond) {
		if _, err := os.Stat(started); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the overlapping call did not start")
		}
	}

	if path, err := EnsureConda(false, true, false, false, true); err != nil || path == "" {
		t.Fatalf("first micromamba call = %q, %v", path, err)
	}
	// The wrapper's dependency changes between the calls, not during one.
	if err := os.WriteFile(version, []byte("0.1.0\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if path, _ := EnsureConda(false, true, false, false, true); path != "" {
		t.Errorf("second micromamba call accepted version 0.1.0 from the first call's observation: %q", path)
	}
}

func TestObserverRetriesFailureToStart(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("relies on Linux reporting a busy text file")
	}
	dir := t.TempDir()
	micromamba := filepath.Join(dir, "micromamba")
	standIn(t, micromamba, "echo 2.9.0")
	writer, err := os.OpenFile(micromamba, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	obs := newObserver()

	_, busy := obs.versionOutput(micromamba)
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	output, err := obs.versionOutput(micromamba)

	if !errors.Is(busy, syscall.ETXTBSY) || err != nil || string(output) != "2.9.0\n" {
		t.Errorf("versionOutput() = %v, then %q, %v; want a busy text file, then 2.9.0", busy, output, err)
	}
	if got := runs(t, dir); got != 1 {
		t.Errorf("ran %d times, want once", got)
	}
}
