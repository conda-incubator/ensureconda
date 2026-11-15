package cmd

import "testing"

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
			got := platformSubdir(tt.goos, tt.goarch)
			if got != tt.want {
				t.Errorf("platformSubdir() = %v, want %v", got, tt.want)
			}
		})
	}
}
