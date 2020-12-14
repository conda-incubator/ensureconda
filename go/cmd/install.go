package cmd

import (
	"archive/tar"
	"compress/bzip2"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/flowchartsman/retry"
	"github.com/gofrs/flock"
	"github.com/hashicorp/go-version"
	log "github.com/sirupsen/logrus"
	"io"
	"io/ioutil"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"syscall"
	"time"
)

func targetExeFilename(exeName string) string {
	_ = os.MkdirAll(sitePath(), 0700)
	targetFileName := filepath.Join(sitePath(), exeName)
	if runtime.GOOS == "windows" {
		targetFileName = targetFileName + ".exe"
	}
	return targetFileName
}

func InstallMicromamba() (string, error) {
	url := fmt.Sprintf("https://micromamba.snakepit.net/api/micromamba/%s/latest", PlatformSubdir())
	return installMicromamba(url)
}

type AnacondaPkgAttr struct {
	Subdir      string
	Version     string
	BuildNumber int32
	Timestamp   uint64
	SourceUrl   string
	Md5         string
}

type AnacondaPkg struct {
	Size  uint32
	Attrs AnacondaPkgAttr
	Type  string
}

type AnacondaPkgAttrs []AnacondaPkgAttr

func (a AnacondaPkgAttrs) Len() int { return len(a) }
func (a AnacondaPkgAttrs) Less(i, j int) bool {
	versioni, _ := version.NewVersion(a[i].Version)
	versionj, _ := version.NewVersion(a[j].Version)
	if versioni.LessThan(versionj) {
		return true
	} else if versionj.LessThan(versioni) {
		return false
	} else {
		if a[i].BuildNumber < a[j].BuildNumber {
			return true
		} else if a[j].BuildNumber < a[i].BuildNumber {
			return false
		} else {
			return a[i].Timestamp < a[j].Timestamp
		}
	}
}
func (a AnacondaPkgAttrs) Swap(i, j int) { a[i], a[j] = a[j], a[i] }

func InstallCondaStandalone() (string, error) {
	// Get the most recent conda-standalone
	subdir := PlatformSubdir()
	const url = "https://api.anaconda.org/package/anaconda/conda-standalone/files"
	resp, err := http.Get(url)
	if err != nil {
		panic(err)
	}
	defer resp.Body.Close()

	body, err := ioutil.ReadAll(resp.Body)

	if err != nil {
		panic(err.Error())
	}

	var data []AnacondaPkg
	err = json.Unmarshal(body, &data)
	if err != nil {
		panic(err.Error())
	}

	var candidates = make([]AnacondaPkgAttr, 0)
	for _, datum := range data {
		if datum.Attrs.Subdir == subdir {
			candidates = append(candidates, datum.Attrs)
		}
	}
	sort.Sort(AnacondaPkgAttrs(candidates))

	chosen := candidates[len(candidates)-1]

	installedExe, err := downloadAndUnpackCondaTarBz2(
		chosen.SourceUrl, map[string]string{
			"standalone_conda/conda.exe": targetExeFilename("conda_standalone"),
		})

	return installedExe, err
}

func downloadAndUnpackCondaTarBz2(
	url string,
	fileNameMap map[string]string) (string, error) {
	resp, err := http.Get(url)
	if err != nil {
		panic(err)
	}
	defer resp.Body.Close()

	bzf := bzip2.NewReader(resp.Body)
	tarReader := tar.NewReader(bzf)
	file, err := extractTarFiles(tarReader, fileNameMap)
	return file, err
}

func installMicromamba(url string) (string, error) {
	installedExe, err := downloadAndUnpackCondaTarBz2(
		url, map[string]string{
			"Library/bin/micromamba.exe": targetExeFilename("micromamba"),
			"bin/micromamba":             targetExeFilename("micromamba"),
		})

	return installedExe, err
}

func extractTarFiles(tarReader *tar.Reader, fileNameMap map[string]string) (string, error) {
	for true {
		header, err := tarReader.Next()

		if err == io.EOF {
			break
		}
		if err != nil {
			return "", err
		}

		switch header.Typeflag {
		case tar.TypeReg:
			targetFileName := fileNameMap[header.Name]
			if targetFileName != "" {
				err2 := extractTarFile(header, targetFileName, tarReader)
				if err2 != nil {
					return "", err2
				}
				st, _ := os.Stat(targetFileName)
				if err = os.Chmod(targetFileName, st.Mode()|syscall.S_IXUSR); err != nil {
					return "", err
				}
				return targetFileName, nil
			}
		}
	}
	return "", errors.New("could not find file in the tarball")
}

func extractTarFile(header *tar.Header, targetFileName string, tarReader *tar.Reader) error {
	log.WithFields(log.Fields{
		"srcPath": header.Name,
		"dstPath": targetFileName,
	}).Debug("extracting from tarball")

	fileInfo := header.FileInfo()
	r := retry.NewRetrier(10, 100*time.Millisecond, 5*time.Second)
	fileLock := flock.New(targetFileName + ".lock")

	err := r.Run(func() error {
		locked, err := fileLock.TryLock()
		if err != nil {
			return err
		}
		if !locked {
			return errors.New("could not lock")
		}

		file, err := os.OpenFile(targetFileName, os.O_RDWR|os.O_CREATE|os.O_TRUNC, fileInfo.Mode().Perm())
		if err != nil {
			return err
		}
		n, cpErr := io.Copy(file, tarReader)
		if closeErr := file.Close(); closeErr != nil { // close file immediately
			return closeErr
		}
		if cpErr != nil {
			return cpErr
		}
		if n != fileInfo.Size() {
			return fmt.Errorf("unexpected bytes written: wrote %d, want %d", n, fileInfo.Size())
		}
		return err
	})

	return err
}
