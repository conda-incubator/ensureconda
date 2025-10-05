package cmd

import (
	"archive/tar"
	"compress/bzip2"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/ioutil"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"syscall"
	"time"

	pep440 "github.com/aquasecurity/go-pep440-version"
	"github.com/flowchartsman/retry"
	"github.com/gofrs/flock"
	log "github.com/sirupsen/logrus"
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
	Subdir      string `json:"subdir"`
	Version     string `json:"version"`
	BuildNumber int32  `json:"build_number"`
	Timestamp   uint64 `json:"timestamp"`
	SourceUrl   string `json:"source_url"`
	Md5         string `json:"md5"`
}

type AnacondaPkg struct {
	Size  uint32          `json:"size"`
	Attrs AnacondaPkgAttr `json:"attrs"`
	Type  string          `json:"type"`
}

type AnacondaPkgAttrs []AnacondaPkgAttr

func (a AnacondaPkgAttrs) Len() int { return len(a) }
func (a AnacondaPkgAttrs) Less(i, j int) bool {
	// By this point, InstallCondaStandalone has filtered out unparseable versions.
	// If parsing fails here, treat it as a programmer error.
	iVer, err := pep440.Parse(a[i].Version)
	if err != nil {
		panic(err)
	}
	jVer, err := pep440.Parse(a[j].Version)
	if err != nil {
		panic(err)
	}
	if iVer.LessThan(jVer) {
		return true
	}
	if jVer.LessThan(iVer) {
		return false
	}
	if a[i].BuildNumber < a[j].BuildNumber {
		return true
	}
	if a[j].BuildNumber < a[i].BuildNumber {
		return false
	}
	return a[i].Timestamp < a[j].Timestamp
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

	// Filter out unparseable versions with a warning, to avoid crashes on new formats
	filtered := make([]AnacondaPkgAttr, 0, len(candidates))
	for _, c := range candidates {
		if _, err := pep440.Parse(c.Version); err != nil {
			log.WithFields(log.Fields{
				"version": c.Version,
				"subdir":  c.Subdir,
			}).Warn("skipping unparseable conda-standalone version")
			continue
		}
		filtered = append(filtered, c)
	}
	if len(filtered) == 0 {
		return "", fmt.Errorf("no parseable conda-standalone versions found for %s", subdir)
	}

	sort.Sort(AnacondaPkgAttrs(filtered))

	chosen := filtered[len(filtered)-1]

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
	for {
		header, err := tarReader.Next()

		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return "", err
		}

		switch header.Typeflag {
		case tar.TypeReg:
			targetFileName := fileNameMap[header.Name]
			tmpFileName := targetFileName + ".tmp"
			if targetFileName != "" {
				err2 := extractTarFile(header, tmpFileName, tarReader)
				if err2 != nil {
					return "", err2
				}
				st, _ := os.Stat(tmpFileName)
				if err = os.Chmod(tmpFileName, st.Mode()|syscall.S_IXUSR); err != nil {
					return "", err
				}
				if err = os.Rename(tmpFileName, targetFileName); err != nil {
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
