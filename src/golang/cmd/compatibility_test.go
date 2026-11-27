package cmd_test

import "github.com/conda-incubator/ensureconda/src/golang/cmd"

// These declarations also need to compile for callers upgrading from v1.7.
var _ func() error = cmd.Execute
var _ func() string = cmd.PlatformSubdir
