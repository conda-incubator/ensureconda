package cmd

import (
	"strings"

	pep440 "github.com/aquasecurity/go-pep440-version"
)

type condaExecutable struct {
	Path    string `json:"path"`
	Flavor  string `json:"flavor"`
	Version string `json:"version"`
}

func newCondaExecutable(path, flavor string, version pep440.Version) *condaExecutable {
	return &condaExecutable{Path: path, Flavor: flavor, Version: normalizedVersion(version)}
}

// normalizedVersion matches packaging.version.Version.__str__. The Go parser
// normalizes the public version, but retains separators and leading zeros in
// local version labels.
func normalizedVersion(version pep440.Version) string {
	if version.Local() == "" {
		return version.String()
	}
	parts := strings.FieldsFunc(version.Local(), func(r rune) bool {
		return r == '.' || r == '-' || r == '_'
	})
	for i, part := range parts {
		if strings.Trim(part, "0123456789") == "" {
			parts[i] = strings.TrimLeft(part, "0")
			if parts[i] == "" {
				parts[i] = "0"
			}
		}
	}
	return version.Public() + "+" + strings.Join(parts, ".")
}
