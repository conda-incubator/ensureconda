package cmd

import (
	"errors"
	pep440 "github.com/aquasecurity/go-pep440-version"
	log "github.com/sirupsen/logrus"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

func executableHasMinVersion(minVersion pep440.Version, prefix string) func(executable string) (bool, error) {
	return func(executable string) (bool, error) {
		stdout, err := exec.Command(executable, "--version").Output()
		log.WithFields(log.Fields{
			"executable":    executable,
			"versionOutput": string(stdout),
			"minVersion":    minVersion.String(),
		}).Debug("Detecting executable version")
		if err != nil {
			return false, err
		}
		lines := strings.Split(strings.ReplaceAll(string(stdout), "\r\n", "\n"), "\n")
		for _, line := range lines {
			if strings.HasPrefix(line, prefix) {
				parts := strings.Split(line, " ")
				v, err := pep440.Parse(parts[len(parts)-1])
				if err != nil {
					continue
				}
				if !v.LessThan(minVersion) {
					return true, nil
				}
			}
		}
		return false, nil
	}
}

func ResolveExecutable(executableName string, dataDir string, versionPredicate func(path string) (bool, error)) (string, error) {
	path := os.Getenv("PATH")
	var filteredPaths []string
	// Append our special path first
	filteredPaths = append(filteredPaths, dataDir)

	for _, dir := range filepath.SplitList(path) {
		bad := filepath.Join(".pyenv", "shims")
		if !strings.Contains(dir, bad) {
			filteredPaths = append(filteredPaths, dir)
		}
	}
	newPathEnv := strings.Join(filteredPaths, string(os.PathListSeparator))
	return FindExecutable(executableName, newPathEnv, versionPredicate)
}

// defaultPathExt is used on Windows when PATHEXT is unset.
const defaultPathExt = ".COM;.EXE;.BAT;.CMD"

// executableCandidates returns the file names that may hold the executable at
// path. Windows identifies executables by extension, so try each extension
// from PATHEXT in order, as cmd.exe does.
func executableCandidates(path string, goos string, pathExt string) []string {
	if goos != "windows" {
		return []string{path}
	}
	if pathExt == "" {
		pathExt = defaultPathExt
	}
	var candidates []string
	for _, ext := range strings.Split(pathExt, ";") {
		if ext != "" {
			candidates = append(candidates, path+ext)
		}
	}
	return candidates
}

func assertExecutable(file string) error {
	d, err := os.Stat(file)
	if err != nil {
		return err
	}
	m := d.Mode()
	if m.IsDir() {
		return os.ErrPermission
	}
	// Windows has no executable permission bits; Go reports regular files
	// there as 0666 or 0444, so the extension decides instead.
	if runtime.GOOS == "windows" || m&0111 != 0 {
		return nil
	}
	return os.ErrPermission
}

func FindExecutable(executableFileName string, searchPath string, predicate func(path string) (bool, error)) (string, error) {
	log.
		WithField("searchPath", searchPath).
		WithField("executable", executableFileName).
		Debug("Searching for executable")
	for _, dir := range filepath.SplitList(searchPath) {
		if dir == "" {
			// Unix shell semantics: searchPath element "" means "."
			dir = "."
		}
		path := filepath.Join(dir, executableFileName)
		for _, candidate := range executableCandidates(path, runtime.GOOS, os.Getenv("PATHEXT")) {
			if err := assertExecutable(candidate); err == nil {
				if result, err := predicate(candidate); err == nil && result {
					return candidate, nil
				}
			}
		}
	}
	return "", errors.New("could not find executable")
}
