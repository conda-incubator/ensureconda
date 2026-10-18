package cmd

import (
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"
)

func acceptAny(string) (bool, error) { return true, nil }

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
