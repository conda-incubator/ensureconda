package cmd

import (
	"fmt"
	pep440 "github.com/aquasecurity/go-pep440-version"
	"io/ioutil"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	log "github.com/sirupsen/logrus"
)

var pathExt = ""

func initTetEnv() {
	log.SetLevel(log.DebugLevel)
	if runtime.GOOS == "windows" {
		pathExt = ".exe"
	}
	dir, err := ioutil.TempDir(".", "")
	if err != nil {
		log.Fatal(err)
	}
	TestSitePath = dir
}

func TestInstallMicromamba(t *testing.T) {
	initTetEnv()
	defer os.RemoveAll(TestSitePath)
	defer func() { TestSitePath = "" }()

	tests := []struct {
		name    string
		want    string
		wantErr bool
	}{
		{"simple",
			filepath.Join(TestSitePath, fmt.Sprintf("micromamba%s", pathExt)),
			false,
		},
		// TODO: Add test cases.
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {

			got, err := InstallMicromamba()
			if (err != nil) != tt.wantErr {
				t.Errorf("InstallMicromamba() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			gotClean := filepath.Clean(got)
			wantClean := filepath.Clean(tt.want)

			if gotClean != wantClean {
				t.Errorf("InstallMicromamba() got = %v, want %v", gotClean, wantClean)
			}
		})
	}
}

func TestInstallCondaStandalone(t *testing.T) {
	initTetEnv()
	defer os.RemoveAll(TestSitePath)
	defer func() { TestSitePath = "" }()

	tests := []struct {
		name    string
		want    string
		wantErr bool
	}{
		{"simple",
			filepath.Join(TestSitePath, fmt.Sprintf("conda_standalone%s", pathExt)),
			false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := InstallCondaStandalone()
			if (err != nil) != tt.wantErr {
				t.Errorf("InstallCondaStandalone() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			gotClean := filepath.Clean(got)
			wantClean := filepath.Clean(tt.want)

			if gotClean != wantClean {
				t.Errorf("InstallCondaStandalone() got = %v, want %v", gotClean, wantClean)
			}

			exeVersion, _ := pep440.Parse("4.8.0")
			hasVersion, err := executableHasMinVersion(exeVersion, "conda")(got)
			if (err != nil) != tt.wantErr {
				t.Errorf("InstallCondaStandalone() error = %v", err)
			}
			if !hasVersion {
				t.Errorf("InstallCondaStandalone() didn't match minimal versions")
			}

		})
	}
}
