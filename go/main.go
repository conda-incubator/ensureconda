package main

import (
	"github.com/conda-incubator/ensureconda/cmd"
	"os"
)

func main() {
	err := cmd.Execute()
	if err != nil {
		os.Exit(1)
	}
}
