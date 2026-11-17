package cmd

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/Wessie/appdirs"
)

func TestDataDirectoryOverride(t *testing.T) {
	previous := TestSitePath
	TestSitePath = ""
	t.Cleanup(func() { TestSitePath = previous })
	for _, directory := range []string{filepath.Join(t.TempDir(), "custom data"), "relative data"} {
		t.Run(directory, func(t *testing.T) {
			t.Setenv("ENSURECONDA_DATA_DIR", directory)
			t.Setenv("XDG_DATA_HOME", filepath.Join(t.TempDir(), "ignored"))
			if got := sitePath(); got != directory {
				t.Fatalf("sitePath() = %q, want %q", got, directory)
			}
			if _, err := os.Stat(directory); !os.IsNotExist(err) {
				t.Fatalf("discovery created the directory, or stat failed: %v", err)
			}
		})
	}
}

func TestDefaultDataDirectory(t *testing.T) {
	previous := TestSitePath
	TestSitePath = ""
	t.Cleanup(func() { TestSitePath = previous })
	for _, unset := range []bool{false, true} {
		t.Setenv("ENSURECONDA_DATA_DIR", "")
		if unset {
			if err := os.Unsetenv("ENSURECONDA_DATA_DIR"); err != nil {
				t.Fatal(err)
			}
		}
		want := appdirs.UserDataDir("ensure-conda", "", "", false)
		if got := sitePath(); got != want {
			t.Fatalf("sitePath() = %q, want %q", got, want)
		}
	}
}

func TestPlatformSubdir(t *testing.T) {
	tests := []struct {
		goos   string
		goarch string
		want   string
	}{
		{"linux", "386", "linux-32"},
		{"linux", "amd64", "linux-64"},
		{"linux", "arm64", "linux-aarch64"},
		{"linux", "ppc64", "linux-ppc64"},
		{"linux", "ppc64le", "linux-ppc64le"},
		{"linux", "riscv64", "linux-riscv64"},
		{"linux", "s390x", "linux-s390x"},
		{"darwin", "amd64", "osx-64"},
		{"darwin", "arm64", "osx-arm64"},
		{"windows", "386", "win-32"},
		{"windows", "amd64", "win-64"},
		{"windows", "arm64", "win-arm64"},
	}
	for _, tt := range tests {
		t.Run(tt.goos+"/"+tt.goarch, func(t *testing.T) {
			got, err := platformSubdir(tt.goos, tt.goarch)
			if err != nil {
				t.Fatalf("platformSubdir() error = %v", err)
			}
			if got != tt.want {
				t.Errorf("platformSubdir() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestPlatformSubdirUnsupported(t *testing.T) {
	for _, platform := range [][2]string{{"freebsd", "amd64"}, {"linux", "arm"}, {"linux", "mips64"}} {
		if got, err := platformSubdir(platform[0], platform[1]); err == nil {
			t.Errorf("platformSubdir(%q, %q) = %q, want an error", platform[0], platform[1], got)
		}
	}
}
