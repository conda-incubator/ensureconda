package cmd

import (
	"bytes"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"

	pep440 "github.com/aquasecurity/go-pep440-version"
	log "github.com/sirupsen/logrus"
)

func acceptAny(string) (bool, error) { return true, nil }

func TestParseExecutableVersion(t *testing.T) {
	for _, tt := range []struct {
		name, output, want string
		prefixes           []string
		wantErr            bool
	}{
		{"mamba v1", "mamba 1.5.12\nconda 24.11.3\n", "1.5.12", []string{"mamba", ""}, false},
		{"mamba v2", "2.9.0\n", "2.9.0", []string{"mamba", ""}, false},
		{"old mamba", "mamba 0.5.0\nconda 4.9.0\n", "0.5.0", []string{"mamba", ""}, false},
		{"invalid mamba", "mamba unknown\nconda 25.1.0\n", "", []string{"mamba", ""}, true},
		{"mamba line takes precedence", "conda 25.1.0\nmamba 1.5.12\n", "1.5.12", []string{"mamba", ""}, false},
		{"first micromamba line", "0.1.0\n2.9.0\n", "0.1.0", []string{""}, false},
		{"invalid first line", "warning: something\n2.9.0\n", "", []string{""}, true},
		{"first conda line", "conda 4.0.0\nconda 25.1.0\n", "4.0.0", []string{"conda"}, false},
		{"zero version", "0.0.0\n", "", []string{""}, false},
		{"conda placeholder", "conda 0.0.0.dev0+placeholder\n", "", []string{"conda"}, false},
		{"zero with local label", "0+unknown\n", "", []string{""}, false},
		{"nonzero patch", "0.0.1\n", "0.0.1", []string{""}, false},
		{"nonzero fourth number", "0.0.0.1\n", "0.0.0.1", []string{""}, false},
		{"trailing space", "2.9.0 \n", "2.9.0", []string{""}, false},
		{"tab", "micromamba\t2.9.0\n", "2.9.0", []string{""}, false},
		{"outer whitespace", "\n \tconda 25.1.0\t \r\n", "25.1.0", []string{"conda"}, false},
		{"carriage return", "warning\rconda 25.1.0\r", "25.1.0", []string{"conda"}, false},
		{"unicode whitespace", "\u2003conda\u00a025.1.0\u2003", "25.1.0", []string{"conda"}, false},
		{"unicode line separator", "warning\u2028conda 25.1.0", "25.1.0", []string{"conda"}, false},
		{"control whitespace", "\x1fconda\x1f25.1.0\x1f", "25.1.0", []string{"conda"}, false},
		{"no matching prefix", "2.9.0\n", "", []string{"conda"}, false},
		{"empty", "\n \t", "", []string{""}, false},
		{"invalid UTF-8", "2.9.0\n\xff", "", []string{""}, true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			version, err := parseExecutableVersion(tt.output, tt.prefixes...)
			if (err != nil) != tt.wantErr {
				t.Fatalf("parseExecutableVersion() error = %v, want error: %v", err, tt.wantErr)
			}
			got := ""
			if version != nil {
				got = version.String()
			}
			if got != tt.want {
				t.Errorf("parseExecutableVersion() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestExecutableVersionRejectionDiagnostics(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the stand-in executables are shell scripts")
	}
	for _, tt := range []struct {
		name, script, message, reason string
		wantErr                       bool
	}{
		{"execution error", "exit 7", "Skipping executable", "exit status 7", true},
		{"parse error", "echo 'warning: x'", "Skipping executable", "malformed version: x", true},
		{"below minimum", "printf '0.1.0\\n2.9.0\\n'", "No acceptable executable version found", "version=0.1.0", false},
		{"empty output", "exit 0", "No acceptable executable version found", "version=\"<nil>\"", false},
		{"zero version", "echo 0.0.0.dev0+placeholder", "No acceptable executable version found", "version=\"<nil>\"", false},
	} {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "micromamba")
			if err := os.WriteFile(path, []byte("#!/bin/sh\n"+tt.script+"\n"), 0755); err != nil {
				t.Fatal(err)
			}
			var output bytes.Buffer
			previousOutput, previousLevel := log.StandardLogger().Out, log.GetLevel()
			log.SetOutput(&output)
			log.SetLevel(log.DebugLevel)
			t.Cleanup(func() {
				log.SetOutput(previousOutput)
				log.SetLevel(previousLevel)
			})
			_, err := executableWithMinVersion(nil, pep440.MustParse(DefaultMinMambaVersion), log.InfoLevel, "")(path)
			if (err != nil) != tt.wantErr {
				t.Fatalf("version check error = %v, want error: %v", err, tt.wantErr)
			}
			var rejection string
			for _, line := range strings.Split(output.String(), "\n") {
				if strings.Contains(line, tt.message) {
					rejection = line
				}
			}
			for _, want := range []string{"level=debug", tt.message, path, "minVersion=" + DefaultMinMambaVersion, tt.reason} {
				if !strings.Contains(rejection, want) {
					t.Errorf("rejection log %q does not contain %q", rejection, want)
				}
			}
		})
	}
}

func TestExecutableCandidates(t *testing.T) {
	tests := []struct {
		name    string
		goos    string
		pathExt string
		want    []string
	}{
		{"unix ignores PATHEXT", "linux", ".EXE", []string{"tool"}},
		{"windows uses PATHEXT", "windows", ".COM;.EXE;.BAT", []string{"tool.COM", "tool.EXE", "tool.BAT"}},
		{"windows default PATHEXT", "windows", "", []string{"tool.COM", "tool.EXE", "tool.BAT", "tool.CMD"}},
		{"windows skips empty entries", "windows", ".EXE;;.BAT;", []string{"tool.EXE", "tool.BAT"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := executableCandidates("tool", tt.goos, tt.pathExt)
			if !reflect.DeepEqual(got, tt.want) {
				t.Errorf("executableCandidates() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestFindExecutable(t *testing.T) {
	t.Setenv("PATHEXT", ".EXE")
	dir := t.TempDir()
	fileName := "tool"
	if runtime.GOOS == "windows" {
		fileName = "tool.EXE"
	}
	want := filepath.Join(dir, fileName)
	if err := os.WriteFile(want, nil, 0755); err != nil {
		t.Fatal(err)
	}

	got, err := FindExecutable("tool", dir, acceptAny)
	if err != nil {
		t.Fatalf("FindExecutable() error = %v", err)
	}
	if got != want {
		t.Errorf("FindExecutable() = %v, want %v", got, want)
	}
}

func TestResolveExecutableDataDirectoryWithPathSeparator(t *testing.T) {
	t.Setenv("PATHEXT", ".EXE")
	dir := filepath.Join(t.TempDir(), "with"+string(os.PathListSeparator)+"separator")
	if err := os.Mkdir(dir, 0755); err != nil {
		t.Fatal(err)
	}
	pathDir := t.TempDir()
	t.Setenv("PATH", pathDir)
	fileName := "tool"
	if runtime.GOOS == "windows" {
		fileName += ".EXE"
	}
	// The data directory still takes precedence over a candidate on PATH.
	for _, parent := range []string{dir, pathDir} {
		if err := os.WriteFile(filepath.Join(parent, fileName), nil, 0755); err != nil {
			t.Fatal(err)
		}
	}
	want := filepath.Join(dir, fileName)
	got, err := ResolveExecutable("tool", dir, acceptAny)
	if err != nil || got != want {
		t.Fatalf("ResolveExecutable() = %q, %v; want %q", got, err, want)
	}
}

func TestFindExecutableSkipsNonExecutable(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows has no executable permission bits")
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "tool"), nil, 0644); err != nil {
		t.Fatal(err)
	}

	if got, err := FindExecutable("tool", dir, acceptAny); err == nil {
		t.Errorf("FindExecutable() = %v, want an error for a non-executable file", got)
	}
}

func TestVersionRejectionIsVisibleAtDefaultVerbosity(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the stand-in executable is a shell script")
	}
	path := filepath.Join(t.TempDir(), "micromamba")
	if err := os.WriteFile(path, []byte("#!/bin/sh\necho 0.1.0\n"), 0755); err != nil {
		t.Fatal(err)
	}
	messages := captureLog(t)
	version, err := checkExecutableMinVersion(nil, path, pep440.MustParse(DefaultMinMambaVersion), log.InfoLevel, "")
	if err != nil || version != nil {
		t.Fatalf("check = %v, %v; want rejection", version, err)
	}
	if got := messages.next(t); !strings.Contains(got, "requires >="+DefaultMinMambaVersion) {
		t.Fatalf("missing rejection reason: %s", got)
	}
}
