package main

import (
	"os"
	"runtime/debug"
	"strings"

	"github.com/conda-incubator/ensureconda/src/golang/cmd"
	log "github.com/sirupsen/logrus"
)

// Release builds set these with -ldflags "-X main.appVersion=...".
var (
	appVersion string
	gitCommit  string
	buildTime  string
)

func init() {
	log.SetOutput(os.Stderr)
	log.SetLevel(log.InfoLevel)
	log.SetFormatter(&log.TextFormatter{})
}

// version describes this build for --version.
func version() string {
	v := appVersion
	if v == "" {
		// `go install ...@version` records the module version instead.
		v = "(devel)"
		if info, ok := debug.ReadBuildInfo(); ok && info.Main.Version != "" {
			v = info.Main.Version
		}
	}
	var details []string
	if gitCommit != "" {
		details = append(details, "commit "+gitCommit[:min(len(gitCommit), 12)])
	}
	if buildTime != "" {
		details = append(details, "built "+buildTime)
	}
	if len(details) > 0 {
		v += " (" + strings.Join(details, ", ") + ")"
	}
	return v
}

func main() {
	err := cmd.Execute(version())
	if err != nil {
		os.Exit(1)
	}
}
