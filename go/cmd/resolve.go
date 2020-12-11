package cmd

import (
	"errors"
	"github.com/hashicorp/go-version"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func executableHasMinVersion(minVersion *version.Version, prefix string) func(executable string) (bool, error) {
	return func(executable string) (bool, error) {
		stdout, err := exec.Command(executable, "--version").Output()
		if err != nil {
			return false, err
		}
		lines := strings.Split(string(stdout), "\n")
		for _, line := range lines {
			if strings.HasPrefix(line, prefix) {
				parts := strings.Split(line, " ")
				if exeVersion, err := version.NewVersion(parts[len(parts)-1]); err == nil && exeVersion.GreaterThanOrEqual(minVersion) {
					return true, nil
				}
			}
		}
		return false, nil
	}
}

func ResolveExecutable(executableName string, dataDir string, versionPredicate func(path string) (bool, error)) (string, error) {
	path := os.Getenv("PATH")
	defer os.Setenv("PATH", path)
	var filteredPaths []string
	// Append our special path first
	filteredPaths = append(filteredPaths, dataDir)

	for _, dir := range filepath.SplitList(path) {
		bad := filepath.Join(".pyenv", "shims")
		if !strings.Contains(dir, bad) {
			filteredPaths = append(filteredPaths, dir)
		}
	}
	newPathEnv := filepath.Join(filteredPaths...)
	os.Setenv("PATH", newPathEnv)
	return FindExecutable(executableName, versionPredicate)
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

func FindExecutable(file string, predicate func(path string) (bool, error)) (string, error) {
	path := os.Getenv("PATH")
	for _, dir := range filepath.SplitList(path) {
		if dir == "" {
			// Unix shell semantics: path element "" means "."
			dir = "."
		}
		path := filepath.Join(dir, file)
		if err := assertExecutable(path); err == nil {
			if result, err := predicate(path); err == nil && result == true {
				return path, nil
			}
		}
	}
	return "", errors.New("could not find executable")
}
