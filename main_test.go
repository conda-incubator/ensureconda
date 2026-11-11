package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// releaseLinkerFlags returns the -ldflags value from the release workflow, with
// its GitHub expressions replaced by example values.
func releaseLinkerFlags(t *testing.T) string {
	t.Helper()
	workflow, err := os.ReadFile(filepath.Join(".github", "workflows", "release-golang.yml"))
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(string(workflow), "\n")
	var flags []string
	for i, line := range lines {
		if strings.TrimSpace(line) != "ldflags: >-" {
			continue
		}
		indent := len(line) - len(strings.TrimLeft(line, " "))
		for _, next := range lines[i+1:] {
			if strings.TrimSpace(next) == "" || len(next)-len(strings.TrimLeft(next, " ")) <= indent {
				break
			}
			flags = append(flags, strings.TrimSpace(next))
		}
	}
	if len(flags) == 0 {
		t.Fatal("no ldflags found in the release workflow")
	}
	return strings.NewReplacer(
		"${{ env.APP_VERSION }}", "v9.9.9",
		"${{ env.BUILD_TIME }}", "Sun Oct 18 14:57:13 UTC 2026",
		"${{ github.sha }}", "0123456789abcdef0123456789abcdef01234567",
	).Replace(strings.Join(flags, " "))
}

func TestVersionFlagWithReleaseLinkerFlags(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the binary")
	}
	binary := filepath.Join(t.TempDir(), "ensureconda")
	if runtime.GOOS == "windows" {
		binary += ".exe"
	}
	build := exec.Command("go", "build", "-o", binary, "-ldflags", releaseLinkerFlags(t), ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("go build: %v\n%s", err, out)
	}

	out, err := exec.Command(binary, "--version").Output()
	if err != nil {
		t.Fatalf("--version: %v", err)
	}
	want := "ensureconda version v9.9.9 (commit 0123456789ab, built Sun Oct 18 14:57:13 UTC 2026)\n"
	if string(out) != want {
		t.Errorf("--version printed %q, want %q", out, want)
	}

	// -v must still mean --verbosity rather than --version.
	help, err := exec.Command(binary, "--help").Output()
	if err != nil {
		t.Fatalf("--help: %v", err)
	}
	if !strings.Contains(string(help), "-v, --verbosity") {
		t.Errorf("--help no longer lists -v for --verbosity:\n%s", help)
	}
}

func TestVersionWithoutLinkerFlags(t *testing.T) {
	if got := version(); got == "" || strings.Contains(got, "commit") {
		t.Errorf("version() = %q, want the module version alone", got)
	}
}
