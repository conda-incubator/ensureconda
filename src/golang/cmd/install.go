package cmd

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/bzip2"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"sort"
	"strings"
	"syscall"
	"time"

	pep440 "github.com/aquasecurity/go-pep440-version"
	"github.com/gofrs/flock"
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
	// Sha256 is empty for older conda-forge files, which lack a digest.
	Sha256 string `json:"sha256"`
	// Labels is nil when the listing omits them.
	Labels []string `json:"labels"`
}

type AnacondaPkgs []AnacondaPkg

type ArchiveType int

const (
	UnrecognizedArchive ArchiveType = iota
	TarBz2Archive
	CondaArchive
)

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
	channel := os.Getenv("ENSURECONDA_CONDA_STANDALONE_CHANNEL")
	if channel == "" {
		channel = "conda-forge"
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

	unlock, err := lockInstall("conda_exe_install.lock")
	if err != nil {
		return "", err
	}
	defer unlock()

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
	installedExe, err := downloadAndUnpackArchive(
		downloadUrl, chosen.Sha256, map[string]string{
			"standalone_conda/conda.exe": target,
		})

	if err != nil {
		return "", fmt.Errorf("downloading or unpacking conda-standalone: %w", err)
	}
	return installedExe, nil
}

// computeCandidates returns the sorted list of available conda-standalone
// packages for the given channel and subdir (ascending by version/build/timestamp).
func computeCandidates(channel string, subdir string) ([]AnacondaPkg, error) {
	url := fmt.Sprintf("%s/package/%s/conda-standalone/files", anacondaAPIBaseURL, channel)
	client := httpClient()
	var data []AnacondaPkg
	resp, err := client.R().
		SetResult(&data).
		Get(url)
	if err != nil {
		return nil, fmt.Errorf("GET candidates: %w", err)
	}
	if err := checkStatus(resp, url); err != nil {
		return nil, err
	}

	candidates := selectCandidates(data, subdir)
	if len(candidates) == 0 {
		return nil, fmt.Errorf("no conda-standalone package found for %s in channel %s", subdir, channel)
	}
	sort.Sort(AnacondaPkgs(candidates))
	return candidates, nil
}

// selectCandidates returns the packages in data that are suitable for subdir.
func selectCandidates(data []AnacondaPkg, subdir string) []AnacondaPkg {
	candidates := make([]AnacondaPkg, 0)
	for _, datum := range data {
		if datum.Attrs.Subdir != subdir ||
			// Ignore onedir packages as workaround for
			// <https://github.com/conda/conda-standalone/issues/182>
			strings.Contains(datum.Attrs.Build, "_onedir_") ||
			// Builds marked as broken lose the "main" label.
			(datum.Labels != nil && !slices.Contains(datum.Labels, "main")) {
			continue
		}
		// Skip unparseable versions with a warning, to avoid crashes on new formats
		if _, err := pep440.Parse(datum.Version); err != nil {
			log.WithFields(log.Fields{
				"version": datum.Version,
				"subdir":  datum.Attrs.Subdir,
			}).Warn("skipping unparseable conda-standalone version")
			continue
		}
		candidates = append(candidates, datum)
	}
	return candidates
}

func inferArchiveTypeFromUrl(url string) ArchiveType {
	if strings.HasSuffix(url, "/latest") || strings.HasSuffix(url, ".tar.bz2") {
		return TarBz2Archive
	} else if strings.HasSuffix(url, ".conda") {
		return CondaArchive
	}
	return UnrecognizedArchive
}

// downloadAndUnpackArchive extracts the files in fileNameMap from the archive
// at url. If expectedSha256 is not empty, the whole archive is verified
// against it before anything is extracted.
func downloadAndUnpackArchive(url string, expectedSha256 string, fileNameMap map[string]string) (string, error) {
	archiveType := inferArchiveTypeFromUrl(url)

	switch archiveType {
	case TarBz2Archive:
		return downloadAndUnpackTarBz2(url, expectedSha256, fileNameMap)
	case CondaArchive:
		return downloadAndUnpackConda(url, expectedSha256, fileNameMap)
	default:
		return "", fmt.Errorf("unrecognized archive type for URL: %s", url)
	}
}

// verifySha256 returns an error unless data has the expected hex digest.
func verifySha256(data []byte, expected string) error {
	sum := sha256.Sum256(data)
	if actual := hex.EncodeToString(sum[:]); !strings.EqualFold(actual, expected) {
		return fmt.Errorf("SHA-256 mismatch: expected %s, got %s", expected, actual)
	}
	return nil
}

func downloadAndUnpackTarBz2(url string, expectedSha256 string, fileNameMap map[string]string) (string, error) {
	client := httpClient()
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
	if err := checkStatus(resp, url); err != nil {
		return "", err
	}

	var archive io.Reader = resp.RawBody()
	if expectedSha256 != "" {
		// Buffer the archive so that it can be verified before extraction.
		data, err := io.ReadAll(resp.RawBody())
		if err != nil {
			return "", err
		}
		if err := verifySha256(data, expectedSha256); err != nil {
			return "", fmt.Errorf("%s (HTTP %s): %w", url, resp.Status(), err)
		}
		archive = bytes.NewReader(data)
	}

	bzf := bzip2.NewReader(archive)
	tarReader := tar.NewReader(bzf)
	file, err := extractTarFiles(tarReader, fileNameMap)
	return file, err
}

func downloadAndUnpackConda(url string, expectedSha256 string, fileNameMap map[string]string) (string, error) {
	client := httpClient()
	resp, err := client.R().Get(url)

	if err != nil {
		return "", err
	}
	if err := checkStatus(resp, url); err != nil {
		return "", err
	}

	// Read the response body into a byte slice
	body := resp.Body()
	if expectedSha256 != "" {
		if err := verifySha256(body, expectedSha256); err != nil {
			return "", fmt.Errorf("%s (HTTP %s): %w", url, resp.Status(), err)
		}
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

// installMicromamba downloads micromamba from url, unless a concurrent process
// has just installed one that can be used.
func installMicromamba(url string) (string, error) {
	unlock, err := lockInstall("micromamba_install.lock")
	if err != nil {
		return "", err
	}
	defer unlock()

	// A concurrent process may have installed it while this one waited for the
	// lock. Check it instead of trusting its age: any micromamba that was here
	// before has already been rejected, or this install wouldn't be running.
	target := targetExeFilename("micromamba")
	if usable, _ := micromambaIsUsable(target); usable {
		return target, nil
	}

	// micro.mamba.pm publishes no digest to verify against.
	return downloadAndUnpackArchive(
		url, "", map[string]string{
			"Library/bin/micromamba.exe": target,
			"bin/micromamba":             target,
		})
}

// lockInstall takes the named download lock in the site path, so that
// concurrent processes download each executable only once. The lock names match
// the Python implementation, so the two coordinate.
func lockInstall(lockName string) (func(), error) {
	_ = os.MkdirAll(sitePath(), 0700)
	return lockFile(filepath.Join(sitePath(), lockName), "downloading")
}

// lockFile blocks until it holds the lock at lockPath, and returns a function
// that releases it. Like the Python implementation, it reports only when it has
// to wait for another process, which it describes as busy with activity.
func lockFile(lockPath string, activity string) (func(), error) {
	fileLock := flock.New(lockPath)
	locked, err := fileLock.TryLock()
	if err != nil {
		return nil, fmt.Errorf("locking %s: %w", lockPath, err)
	}
	if !locked {
		log.WithField("lockPath", lockPath).Infof("waiting for another process to finish %s", activity)
		start := time.Now()
		if err := fileLock.Lock(); err != nil {
			return nil, fmt.Errorf("locking %s: %w", lockPath, err)
		}
		log.WithField("lockPath", lockPath).Infof("lock acquired after %s", time.Since(start).Round(100*time.Millisecond))
	}
	return func() { _ = fileLock.Unlock() }, nil
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
			if targetFileName := fileNameMap[header.Name]; targetFileName != "" {
				if err := extractTarFile(header, targetFileName, tarReader); err != nil {
					return "", err
				}
				return targetFileName, nil
			}
		}
	}
	return "", errors.New("could not find file in the tarball")
}

// extractTarFile writes the current entry of tarReader to targetFileName as an
// executable. It writes to a temporary file and renames it into place while
// holding a lock on the target, the same lock the Python implementation uses,
// so that concurrent writers don't interfere. The temporary file is removed if
// anything fails.
func extractTarFile(header *tar.Header, targetFileName string, tarReader *tar.Reader) error {
	log.WithFields(log.Fields{
		"srcPath": header.Name,
		"dstPath": targetFileName,
	}).Debug("extracting from tarball")

	unlock, err := lockFile(targetFileName+".lock", "writing "+filepath.Base(targetFileName))
	if err != nil {
		return err
	}
	defer unlock()

	// Every extraction writes its temporary file while holding this lock, so
	// one that is here now was left behind by a run that was interrupted.
	removeTemporaryFiles(targetFileName)

	// A name of its own, as in the Python implementation. A writer that doesn't
	// take this lock, such as an older release, then can't write to the same file.
	tmpFileName := fmt.Sprintf("%s.%s.tmp", targetFileName, rand.Text())
	err = writeExecutable(tmpFileName, header, tarReader)
	if err == nil {
		err = os.Rename(tmpFileName, targetFileName)
	}
	if err != nil {
		// Don't leave a partial executable behind.
		_ = os.Remove(tmpFileName)
	}
	return err
}

// removeTemporaryFiles removes the temporary files of extractions to
// targetFileName. It leaves "<target>.tmp" alone, which older releases write
// without taking the lock on the target.
func removeTemporaryFiles(targetFileName string) {
	dir, name := filepath.Split(targetFileName)
	entries, _ := os.ReadDir(dir)
	for _, entry := range entries {
		if isTemporary, _ := filepath.Match(name+".*.tmp", entry.Name()); isTemporary {
			_ = os.Remove(filepath.Join(dir, entry.Name()))
		}
	}
}

// writeExecutable writes the current entry of tarReader to fileName and marks
// it executable.
func writeExecutable(fileName string, header *tar.Header, tarReader *tar.Reader) error {
	file, err := os.OpenFile(fileName, os.O_RDWR|os.O_CREATE|os.O_TRUNC, header.FileInfo().Mode().Perm())
	if err != nil {
		return err
	}
	n, err := io.Copy(file, tarReader)
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if n != header.Size {
		return fmt.Errorf("unexpected bytes written: wrote %d, want %d", n, header.Size)
	}
	st, err := os.Stat(fileName)
	if err != nil {
		return fmt.Errorf("stat temp file: %w", err)
	}
	return os.Chmod(fileName, st.Mode()|syscall.S_IXUSR)
}
