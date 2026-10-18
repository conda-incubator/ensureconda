package cmd

import (
	"reflect"
	"testing"
)

func TestSelectCandidates(t *testing.T) {
	pkg := func(version, subdir, build string, labels []string) AnacondaPkg {
		return AnacondaPkg{Version: version, Labels: labels, Attrs: AnacondaPkgAttr{Subdir: subdir, Build: build}}
	}
	data := []AnacondaPkg{
		pkg("1.0", "linux-64", "h0_0", []string{"main"}),
		// conda-forge moves builds it marks as broken to the "broken" label.
		pkg("3.0", "linux-64", "h0_0", []string{"broken"}),
		pkg("not a version", "linux-64", "h0_0", []string{"main"}),
		pkg("2.0", "linux-64", "h0_onedir_0", []string{"main"}),
		// Entries without labels are kept.
		pkg("2.1", "linux-64", "h0_0", nil),
		pkg("4.0", "osx-64", "h0_0", []string{"main"}),
	}

	var versions []string
	for _, candidate := range selectCandidates(data, "linux-64") {
		versions = append(versions, candidate.Version)
	}

	if want := []string{"1.0", "2.1"}; !reflect.DeepEqual(versions, want) {
		t.Errorf("selectCandidates() versions = %v, want %v", versions, want)
	}
}
