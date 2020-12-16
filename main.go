package main

import (
	"github.com/conda-incubator/ensureconda/src/golang/cmd"
	log "github.com/sirupsen/logrus"
	"os"
)

func init() {
	log.SetOutput(os.Stderr)
	log.SetLevel(log.InfoLevel)
	log.SetFormatter(&log.TextFormatter{})
}

func main() {
	err := cmd.Execute()
	if err != nil {
		os.Exit(1)
	}
}
