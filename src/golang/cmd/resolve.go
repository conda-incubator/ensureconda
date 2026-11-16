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
	"unicode"
	"unicode/utf8"
)

type versionCheck func(executable string) (*pep440.Version, error)

func executableWithMinVersion(minVersion pep440.Version, prefixes ...string) versionCheck {
	return func(executable string) (*pep440.Version, error) {
		stdout, err := exec.Command(executable, "--version").Output()
		entry := log.WithFields(log.Fields{
			"executable":    executable,
			"versionOutput": string(stdout),
			"minVersion":    minVersion.String(),
		})
		entry.Debug("Detecting executable version")
		if err != nil {
			entry.WithError(err).Debug("Skipping executable: could not determine its version")
			return nil, err
		}
		version, err := parseExecutableVersion(string(stdout), prefixes...)
		if err != nil {
			entry.WithError(err).Debug("Skipping executable: could not determine its version")
			return nil, err
		}
		if version == nil || version.LessThan(minVersion) {
			entry.WithField("version", version).Debug("No acceptable executable version found")
			return nil, nil
		}
		return version, nil
	}
}

// parseExecutableVersion follows Python's determine_*_version helpers: use the
// first matching line, trying another prefix only when no line matched. In
// particular, a rejected mamba v1 version must not fall back to conda's version.
func parseExecutableVersion(output string, prefixes ...string) (*pep440.Version, error) {
	if !utf8.ValidString(output) {
		return nil, errors.New("version output is not valid UTF-8")
	}
	// Python's strip/split also consider these four control characters whitespace.
	isSpace := func(r rune) bool { return unicode.IsSpace(r) || r >= '\x1c' && r <= '\x1f' }
	lines := strings.FieldsFunc(strings.TrimFunc(output, isSpace), func(r rune) bool {
		// Line boundaries recognized by Python's str.splitlines().
		switch r {
		case '\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\u0085', '\u2028', '\u2029':
			return true
		}
		return false
	})
	for _, prefix := range prefixes {
		for _, line := range lines {
			if strings.HasPrefix(line, prefix) {
				parts := strings.FieldsFunc(line, isSpace)
				if len(parts) == 0 {
					return nil, nil
				}
				version, err := pep440.Parse(parts[len(parts)-1])
				if err != nil {
					return nil, err
				}
				return &version, nil
			}
		}
	}
	return nil, nil
}

// resolveExecutableWithVersion keeps the version that accepted the candidate,
// so reporting metadata doesn't need another call to --version.
func resolveExecutableWithVersion(name, dataDir string, check versionCheck) (*condaExecutable, error) {
	var version *pep440.Version
	path, err := ResolveExecutable(name, dataDir, func(path string) (bool, error) {
		var err error
		version, err = check(path)
		return version != nil, err
	})
	if err != nil {
		return nil, err
	}
	return newCondaExecutable(path, name, *version), nil
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
