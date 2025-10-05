package cmd

import (
	"errors"
	pep440 "github.com/aquasecurity/go-pep440-version"
	log "github.com/sirupsen/logrus"
	"os"
	"os/exec"
	"path/filepath"
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

func assertExecutable(file string) error {
	d, err := os.Stat(file)
	if err != nil {
		return err
	}
	if m := d.Mode(); !m.IsDir() && m&0111 != 0 {
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
		if err := assertExecutable(path); err == nil {
			if result, err := predicate(path); err == nil && result {
				return path, nil
			}
		}
	}
	return "", errors.New("could not find executable")
}
