package cmd

import (
	"archive/tar"
	"compress/bzip2"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"syscall"
	"time"

	pep440 "github.com/aquasecurity/go-pep440-version"
	"github.com/flowchartsman/retry"
	"github.com/go-resty/resty/v2"
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
	Build       string `json:"build"`
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

// If the conda executable is older than this, it will be redownloaded
const redownloadWhenOlder = 24 * time.Hour

// If the age is below this negative tolerance, consider timestamp invalid and redownload
const negativeAgeTolerance = -60 * time.Second

func (a AnacondaPkgs) Len() int { return len(a) }
func (a AnacondaPkgs) Less(i, j int) bool {
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
	if a[i].Attrs.BuildNumber < a[j].Attrs.BuildNumber {
		return true
	}
	if a[j].Attrs.BuildNumber < a[i].Attrs.BuildNumber {
		return false
	}
	return a[i].Attrs.Timestamp < a[j].Attrs.Timestamp
}
func (a AnacondaPkgs) Swap(i, j int) { a[i], a[j] = a[j], a[i] }

func getChannelName() (string, error) {
	return "anaconda", nil
}

func InstallCondaStandalone() (string, error) {
	// Get the most recent conda-standalone
	subdir := PlatformSubdir()
	channel, err := getChannelName()
	if err != nil {
		return "", err
	}

	// Ensure site path exists before locking
	_ = os.MkdirAll(sitePath(), 0700)

	// Lock the install to prevent concurrent downloads, similar to Python implementation
	lockPath := filepath.Join(sitePath(), "conda_exe_install.lock")
	fileLock := flock.New(lockPath)

	// Block until we acquire the lock (mimics Python's lock_with_feedback behavior)
	log.WithFields(log.Fields{"lockPath": lockPath}).Info("acquiring conda download lock")
	if err := fileLock.Lock(); err != nil {
		return "", fmt.Errorf("acquiring conda download lock: %w", err)
	}
	defer func() { _ = fileLock.Unlock() }()
	log.WithFields(log.Fields{"lockPath": lockPath}).Info("acquired conda download lock")

	// Check if already installed and fresh
	target := targetExeFilename("conda_standalone")
	if st, statErr := os.Stat(target); statErr == nil {
		age := time.Since(st.ModTime())
		if age < redownloadWhenOlder && age > negativeAgeTolerance {
			return target, nil
		}
	}

	// Download and install
	candidates, err := computeCandidates(channel, subdir)
	if err != nil {
		return "", fmt.Errorf("listing conda-standalone candidates: %w", err)
	}
	chosen := candidates[len(candidates)-1]

	downloadUrl := "https:" + chosen.DownloadUrl
	log.WithFields(log.Fields{"url": downloadUrl}).Info("downloading conda-standalone")
	installedExe, err := downloadAndUnpackCondaTarBz2(
		downloadUrl, map[string]string{
			"standalone_conda/conda.exe": targetExeFilename("conda_standalone"),
		})

	if err != nil {
		return "", fmt.Errorf("downloading or unpacking conda-standalone: %w", err)
	}
	return installedExe, nil
}

// computeCandidates returns the sorted list of available conda-standalone
// packages for the given channel and subdir (ascending by version/build/timestamp).
func computeCandidates(channel string, subdir string) ([]AnacondaPkg, error) {
	url := fmt.Sprintf("https://api.anaconda.org/package/%s/conda-standalone/files", channel)
	client := resty.New()
	var data []AnacondaPkg
	_, err := client.R().
		SetResult(&data).
		Get(url)
	if err != nil {
		return nil, fmt.Errorf("GET candidates: %w", err)
	}

	var candidates = make([]AnacondaPkg, 0)
	for _, datum := range data {
		if datum.Attrs.Subdir == subdir &&
			// Ignore onedir packages as workaround for
			// <https://github.com/conda/conda-standalone/issues/182>
			!strings.Contains(datum.Attrs.Build, "_onedir_") {
			candidates = append(candidates, datum)
		}
	}

	// Filter out unparseable versions with a warning, to avoid crashes on new formats
	filtered := make([]AnacondaPkg, 0, len(candidates))
	for _, c := range candidates {
		if _, err := pep440.Parse(c.Version); err != nil {
			log.WithFields(log.Fields{
				"version": c.Version,
				"subdir":  c.Attrs.Subdir,
			}).Warn("skipping unparseable conda-standalone version")
			continue
		}
		filtered = append(filtered, c)
	}
	if len(filtered) == 0 {
		return nil, fmt.Errorf("no parseable conda-standalone versions found for %s", subdir)
	}

	sort.Sort(AnacondaPkgs(filtered))
	return filtered, nil
}

func downloadAndUnpackCondaTarBz2(
	url string,
	fileNameMap map[string]string) (string, error) {
	client := resty.New()
	resp, err := client.R().
		SetDoNotParseResponse(true).
		Get(url)

	if err != nil {
		return "", err
	}
	if resp.RawBody() == nil {
		return "", fmt.Errorf("nil response body from %s", url)
	}
	defer resp.RawBody().Close()

	bzf := bzip2.NewReader(resp.RawBody())
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
				st, err := os.Stat(tmpFileName)
				if err != nil {
					return "", fmt.Errorf("stat temp file: %w", err)
				}
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
