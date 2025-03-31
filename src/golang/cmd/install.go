package cmd

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/bzip2"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/ioutil"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/flowchartsman/retry"
	"github.com/gofrs/flock"
	"github.com/hashicorp/go-version"
	"github.com/klauspost/compress/zstd"
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
	url := fmt.Sprintf("https://micro.mamba.pm/api/micromamba/%s/latest", PlatformSubdir())
	return installMicromamba(url)
}

type AnacondaPkgAttr struct {
	Subdir      string `json:"subdir"`
	BuildNumber int32  `json:"build_number"`
	Timestamp   uint64 `json:"timestamp"`
}

type AnacondaPkg struct {
	Size        uint32          `json:"size"`
	Attrs       AnacondaPkgAttr `json:"attrs"`
	Type        string          `json:"type"`
	Version     string          `json:"version"`
	DownloadUrl string          `json:"download_url"`
}

type AnacondaPkgs []AnacondaPkg

type ArchiveType int

const (
	UnrecognizedArchive ArchiveType = iota
	TarBz2Archive
	CondaArchive
)

func (a AnacondaPkgs) Len() int { return len(a) }
func (a AnacondaPkgs) Less(i, j int) bool {
	versioni, _ := version.NewVersion(a[i].Version)
	versionj, _ := version.NewVersion(a[j].Version)
	if versioni.LessThan(versionj) {
		return true
	} else if versionj.LessThan(versioni) {
		return false
	} else {
		if a[i].Attrs.BuildNumber < a[j].Attrs.BuildNumber {
			return true
		} else if a[j].Attrs.BuildNumber < a[i].Attrs.BuildNumber {
			return false
		} else {
			return a[i].Attrs.Timestamp < a[j].Attrs.Timestamp
		}
	}
}
func (a AnacondaPkgs) Swap(i, j int) { a[i], a[j] = a[j], a[i] }

func getChannelName() (string, error) {
	channel := os.Getenv("ENSURECONDA_CONDA_STANDALONE_CHANNEL")
	if channel == "" {
		channel = "anaconda"
	}
	validChannelName := regexp.MustCompile(`^[a-zA-Z0-9_-]+$`)
	if !validChannelName.MatchString(channel) {
		return "", fmt.Errorf("invalid channel name %s. Channel names must be alphanumeric and may contain hyphens and underscores", channel)
	}

	return channel, nil
}

func InstallCondaStandalone() (string, error) {
	// Get the most recent conda-standalone
	subdir := PlatformSubdir()
	channel, err := getChannelName()
	if err != nil {
		return "", err
	}
	url := fmt.Sprintf("https://api.anaconda.org/package/%s/conda-standalone/files", channel)

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

	var candidates = make([]AnacondaPkg, 0)
	for _, datum := range data {
		if datum.Attrs.Subdir == subdir {
			candidates = append(candidates, datum)
		}
	}
	sort.Sort(AnacondaPkgs(candidates))

	if len(candidates) == 0 {
		return "", errors.New("No conda-standalone found for " + subdir)
	}
	chosen := candidates[len(candidates)-1]

	downloadUrl := "https:" + chosen.DownloadUrl
	installedExe, err := downloadAndUnpackArchive(
		downloadUrl, map[string]string{
			"standalone_conda/conda.exe": targetExeFilename("conda_standalone"),
		})

	return installedExe, err
}

func inferArchiveTypeFromUrl(url string) ArchiveType {
	if strings.HasSuffix(url, "/latest") || strings.HasSuffix(url, ".tar.bz2") {
		return TarBz2Archive
	} else if strings.HasSuffix(url, ".conda") {
		return CondaArchive
	}
	return UnrecognizedArchive
}

func downloadAndUnpackArchive(url string, fileNameMap map[string]string) (string, error) {
	archiveType := inferArchiveTypeFromUrl(url)

	switch archiveType {
	case TarBz2Archive:
		return downloadAndUnpackTarBz2(url, fileNameMap)
	case CondaArchive:
		return downloadAndUnpackConda(url, fileNameMap)
	default:
		return "", errors.New("Unrecognized archive type " + url)
	}
}

func downloadAndUnpackTarBz2(url string, fileNameMap map[string]string) (string, error) {
	resp, err := http.Get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	bzf := bzip2.NewReader(resp.Body)
	tarReader := tar.NewReader(bzf)
	file, err := extractTarFiles(tarReader, fileNameMap)
	return file, err
}

func downloadAndUnpackConda(url string, fileNameMap map[string]string) (string, error) {
	resp, err := http.Get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	// Read the response body into a byte slice
	body, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}

	byteReader := bytes.NewReader(body)

	zipReader, err := zip.NewReader(byteReader, int64(len(body)))
	if err != nil {
		return "", err
	}

	for _, f := range zipReader.File {
		if strings.HasPrefix(f.Name, "pkg-conda-standalone") && strings.HasSuffix(f.Name, ".tar.zst") {
			rc, err := f.Open()
			if err != nil {
				return "", err
			}
			defer rc.Close()

			zstReader, err := zstd.NewReader(rc)
			if err != nil {
				return "", err
			}
			defer zstReader.Close()

			tarReader := tar.NewReader(zstReader)
			file, err := extractTarFiles(tarReader, fileNameMap)
			return file, err
		}
	}
	return "", errors.New("could not find pkg-conda-standalone*.tar.zst file in the .conda archive")
}

func installMicromamba(url string) (string, error) {
	installedExe, err := downloadAndUnpackArchive(
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
