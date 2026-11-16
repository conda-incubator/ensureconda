package cmd

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/bzip2"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
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

// micromambaSubdirs are the platforms that micromamba is published for. Keep
// it in step with MICROMAMBA_SUBDIRS in src/python/ensureconda/installer.py.
// It is a variable so that tests can change it.
var micromambaSubdirs = map[string]bool{
	"linux-64":      true,
	"linux-aarch64": true,
	"linux-ppc64le": true,
	"linux-riscv64": true,
	"osx-64":        true,
	"osx-arm64":     true,
	"win-64":        true,
	"win-arm64":     true,
}

// notPublishedError means that nothing to install is published for the platform.
type notPublishedError struct {
	message string
}

func (e *notPublishedError) Error() string { return e.message }

func InstallMicromamba() (string, error) {
	subdir, err := PlatformSubdir()
	if err != nil {
		return "", err
	}
	url := fmt.Sprintf("https://micro.mamba.pm/api/micromamba/%s/latest", subdir)
	exe, err := installMicromamba(url)
	return exe, micromambaInstallError(err, subdir)
}

// micromambaInstallError turns a 404 into a notPublishedError, unless
// micromamba is published for the platform: micro.mamba.pm also answers 404
// for a release that hasn't been uploaded for a platform yet.
func micromambaInstallError(err error, subdir string) error {
	var status *httpStatusError
	if errors.As(err, &status) && status.statusCode == 404 && !micromambaSubdirs[subdir] {
		return &notPublishedError{fmt.Sprintf("micromamba is not published for %s", subdir)}
	}
	return err
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
	subdir, err := PlatformSubdir()
	if err != nil {
		return "", err
	}
	channel, err := getChannelName()
	if err != nil {
		return "", err
	}

	target := targetExeFilename("conda_standalone")
	return installUnlessUsable("conda_exe_install.lock", target, usableCondaVersion, func() (string, error) {
		return downloadCondaStandalone(channel, subdir, target)
	})
}

// downloadCondaStandalone installs the latest conda-standalone for subdir from
// channel as target.
func downloadCondaStandalone(channel string, subdir string, target string) (string, error) {
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
	resp, err := requestURLWithRetry(url)
	if err != nil {
		return nil, fmt.Errorf("GET candidates: %w", err)
	}
	var data []AnacondaPkg
	if err := json.Unmarshal(resp.body, &data); err != nil {
		return nil, fmt.Errorf("decoding candidates from %s: %w", url, err)
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
	if archiveType == UnrecognizedArchive {
		return "", fmt.Errorf("unrecognized archive type for URL: %s", url)
	}

	resp, err := requestURLWithRetry(url)
	if err != nil {
		return "", err
	}
	archive := resp.body
	if expectedSha256 != "" {
		if err := verifySha256(archive, expectedSha256); err != nil {
			return "", fmt.Errorf("%s (HTTP %s): %w", url, resp.status, err)
		}
	}
	if archiveType == TarBz2Archive {
		return unpackTarBz2(bytes.NewReader(archive), fileNameMap)
	}
	return unpackConda(archive, fileNameMap)
}

// verifySha256 returns an error unless data has the expected hex digest.
func verifySha256(data []byte, expected string) error {
	sum := sha256.Sum256(data)
	if actual := hex.EncodeToString(sum[:]); !strings.EqualFold(actual, expected) {
		return fmt.Errorf("SHA-256 mismatch: expected %s, got %s", expected, actual)
	}
	return nil
}

func unpackTarBz2(archive io.Reader, fileNameMap map[string]string) (string, error) {
	bzf := bzip2.NewReader(archive)
	tarReader := tar.NewReader(bzf)
	file, err := extractTarFiles(tarReader, fileNameMap)
	return file, err
}

func unpackConda(archive []byte, fileNameMap map[string]string) (string, error) {
	zipReader, err := zip.NewReader(bytes.NewReader(archive), int64(len(archive)))
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
	target := targetExeFilename("micromamba")
	return installUnlessUsable("micromamba_install.lock", target, usableMicromambaVersion, func() (string, error) {
		// micro.mamba.pm publishes no digest to verify against.
		return downloadAndUnpackArchive(
			url, "", map[string]string{
				"Library/bin/micromamba.exe": target,
				"bin/micromamba":             target,
			})
	})
}

// installUnlessUsable calls download while holding the named download lock,
// unless target passes checkVersion once the lock is held.
func installUnlessUsable(lockName string, target string, checkVersion versionCheck, download func() (string, error)) (string, error) {
	unlock, err := lockInstall(lockName)
	if err != nil {
		return "", err
	}
	defer unlock()

	// A concurrent process may have installed it while this one waited for the
	// lock. Check it instead of trusting its age: any executable that was here
	// before has already been rejected, or this install wouldn't be running.
	if version, _ := checkVersion(target); version != nil {
		return target, nil
	}
	return download()
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
	var locked bool
	err := retryWhileBeingDeleted(runtime.GOOS, func() (err error) {
		locked, err = fileLock.TryLock()
		return err
	})
	if err != nil {
		return nil, fmt.Errorf("locking %s: %w", lockPath, err)
	}
	if !locked {
		log.WithField("lockPath", lockPath).Infof("waiting for another process to finish %s", activity)
		start := time.Now()
		if err := retryWhileBeingDeleted(runtime.GOOS, fileLock.Lock); err != nil {
			return nil, fmt.Errorf("locking %s: %w", lockPath, err)
		}
		log.WithField("lockPath", lockPath).Infof("lock acquired after %s", time.Since(start).Round(100*time.Millisecond))
	}
	return func() { _ = fileLock.Unlock() }, nil
}

// How often and how long apart to try to open a lock file that is being
// deleted, before reporting the failure. The delay is a variable so that tests
// don't have to wait.
const lockOpenAttempts = 50

var lockOpenRetryDelay = 20 * time.Millisecond

// retryWhileBeingDeleted calls acquire until it no longer fails because its
// lock file is being deleted. On Windows, the Python implementation deletes a
// lock file when it releases the lock, and the file can't be opened while that
// is under way. The Python implementation treats that as a held lock too.
func retryWhileBeingDeleted(goos string, acquire func() error) error {
	err := acquire()
	for attempt := 1; attempt < lockOpenAttempts && isBeingDeleted(goos, err); attempt++ {
		time.Sleep(lockOpenRetryDelay)
		err = acquire()
	}
	return err
}

// isBeingDeleted reports whether err is what opening a file that is being
// deleted fails with: access denied or a sharing violation on Windows, and
// nothing elsewhere.
func isBeingDeleted(goos string, err error) bool {
	const windowsSharingViolation = syscall.Errno(32)
	return goos == "windows" && (errors.Is(err, os.ErrPermission) || errors.Is(err, windowsSharingViolation))
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
	// A name of its own, as in the Python implementation. A writer that doesn't
	// take the lock below, such as an older release, then can't write to the
	// same file.
	tmpFileName := fmt.Sprintf("%s.%s.tmp", targetFileName, rand.Text())
	log.WithFields(log.Fields{
		"srcPath": header.Name,
		"dstPath": tmpFileName,
	}).Debug("extracting from tarball")

	unlock, err := lockFile(targetFileName+".lock", "writing "+filepath.Base(targetFileName))
	if err != nil {
		return err
	}
	defer unlock()

	// Every extraction writes its temporary file while holding this lock, so
	// one that is here now was left behind by a run that was interrupted.
	removeTemporaryFiles(targetFileName)

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
	fileInfo := header.FileInfo()
	file, err := os.OpenFile(fileName, os.O_RDWR|os.O_CREATE|os.O_TRUNC, fileInfo.Mode().Perm())
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

	st, err := os.Stat(fileName)
	if err != nil {
		return fmt.Errorf("stat temp file: %w", err)
	}
	return os.Chmod(fileName, st.Mode()|syscall.S_IXUSR)
}
