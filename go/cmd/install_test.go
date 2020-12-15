package cmd

import (
	"fmt"
	"github.com/hashicorp/go-version"
	"io/ioutil"
	"os"
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
			fmt.Sprintf("%s/micromamba%s", TestSitePath, pathExt),
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
			if got != tt.want {
				t.Errorf("InstallMicromamba() got = %v, want %v", got, tt.want)
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
			fmt.Sprintf("%s/conda_standalone%s", TestSitePath, pathExt),
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
			if got != tt.want {
				t.Errorf("InstallCondaStandalone() got = %v, want %v", got, tt.want)
			}

			version, _ := version.NewVersion("4.8.0")
			hasVersion, err := executableHasMinVersion(version, "conda")(got)
			if (err != nil) != tt.wantErr {
				t.Errorf("InstallCondaStandalone() error = %v", err)
			}
			if !hasVersion {
				t.Errorf("InstallCondaStandalone() didn't match minimal versions")
			}

		})
	}
}
