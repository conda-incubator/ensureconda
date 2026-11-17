package cmd

import (
	"reflect"
	"slices"
	"testing"
)

func TestSelectCandidates(t *testing.T) {
	pkg := func(version, subdir, build string, labels []string) AnacondaPkg {
		return AnacondaPkg{DownloadUrl: "//example.invalid/package.conda", Version: version, Labels: labels, Attrs: AnacondaPkgAttr{Subdir: subdir, Build: build}}
	}
	data := []AnacondaPkg{
		pkg("1.0", "linux-64", "h0_0", []string{"main"}),
		// conda-forge labels builds it marks as broken "broken", and some of
		// them keep "main".
		pkg("3.0", "linux-64", "h0_0", []string{"broken"}),
		pkg("2.5", "linux-64", "h0_0", []string{"main", "broken"}),
		pkg("not a version", "linux-64", "h0_0", []string{"main"}),
		pkg("2.0", "linux-64", "h0_onedir_0", []string{"main"}),
		// Entries without labels are kept.
		pkg("2.1", "linux-64", "h0_0", nil),
		pkg("4.0", "osx-64", "h0_0", []string{"main"}),
	}
	for _, unusable := range []string{"//[broken", "//example.invalid/package.zip"} {
		entry := pkg("2.2", "linux-64", "h0_0", []string{"main"})
		entry.DownloadUrl = unusable
		data = append(data, entry)
	}

	messages := captureLog(t)
	var versions []string
	for _, candidate := range selectCandidates(data, "linux-64") {
		versions = append(versions, candidate.Version)
	}

	if want := []string{"1.0", "2.1"}; !reflect.DeepEqual(versions, want) {
		t.Errorf("selectCandidates() versions = %v, want %v", versions, want)
	}
	if all := logged(messages); !slices.Contains(all, "Skipping conda-standalone 2.2 for linux-64: invalid download URL") {
		t.Errorf("malformed candidate was skipped without its reason: %v", all)
	}
}
