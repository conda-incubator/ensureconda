package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"

	"github.com/Wessie/appdirs"
	pep440 "github.com/aquasecurity/go-pep440-version"
	"github.com/spf13/cobra"

	log "github.com/sirupsen/logrus"
)

var (
	// Used for flags.

	rootCmd = &cobra.Command{
		Use:   "ensureconda",
		Short: "",
		Long:  ``,
		Run: func(cmd *cobra.Command, args []string) {
			mamba, err := evaluateFlagPair(cmd, "mamba")
			if err != nil {
				panic(err)
			}
			micromamba, err := evaluateFlagPair(cmd, "micromamba")
			if err != nil {
				panic(err)
			}
			conda, err := evaluateFlagPair(cmd, "conda")
			if err != nil {
				panic(err)
			}
			condaExe, err := evaluateFlagPair(cmd, "conda-exe")
			if err != nil {
				panic(err)
			}
			noInstall, err := cmd.Flags().GetBool("no-install")
			if err != nil {
				panic(err)
			}

			forceInstall, err := cmd.Flags().GetBool("force-install")
			if err != nil {
				panic(err)
			}
			if err := validateForceInstall(micromamba, condaExe, noInstall, forceInstall); err != nil {
				er(err)
			}

			asJSON, err := cmd.Flags().GetBool("json")
			if err != nil {
				panic(err)
			}

			verbosity, err := cmd.Flags().GetInt("verbosity")
			if err != nil {
				panic(err)
			}
			switch verbosity {
			case 3:
				log.SetLevel(log.TraceLevel)
			case 2:
				log.SetLevel(log.DebugLevel)
			case 1:
				log.SetLevel(log.InfoLevel)
			case 0:
				log.SetLevel(log.WarnLevel)
			default:
				log.SetLevel(log.InfoLevel)
			}

			var executable *condaExecutable
			if !forceInstall {
				executable, err = ensureCondaWithMetadata(mamba, micromamba, conda, condaExe, true, false)
				if err != nil {
					panic(err)
				}
			}

			if executable != nil {
				log.Infof("Selected %s %s at %s", executable.Flavor, executable.Version, executable.Path)
				if err := printExecutable(cmd, executable, asJSON); err != nil {
					er(err)
				}
				os.Exit(0)
			}
			if !noInstall {
				log.Debugf("Attempting to install")
				executable, err = ensureCondaWithMetadata(mamba, micromamba, conda, condaExe, noInstall, forceInstall)
				if err != nil {
					er(err)
				}
				if executable != nil {
					log.Infof("Selected %s %s at %s", executable.Flavor, executable.Version, executable.Path)
					if err := printExecutable(cmd, executable, asJSON); err != nil {
						er(err)
					}
					os.Exit(0)
				}
			}
			er("Could not find compatible executable")
		},
	}
)

const DefaultMinMambaVersion = "0.7.3"
const DefaultMinCondaVersion = "4.8.2"

var TestSitePath string

func sitePath() string {
	if TestSitePath != "" {
		return TestSitePath
	}
	if override := os.Getenv("ENSURECONDA_DATA_DIR"); override != "" {
		return override
	}
	return appdirs.UserDataDir("ensure-conda", "", "", false)
}

// usableMicromambaVersion returns the version if executable runs and reports
// at least the minimum micromamba version, or nil otherwise.
func usableMicromambaVersion(executable string) (*pep440.Version, error) {
	return micromambaCheck(log.InfoLevel)(executable)
}

func micromambaCheck(rejectionLevel log.Level) versionCheck {
	minMambaVersion, _ := pep440.Parse(DefaultMinMambaVersion)
	return executableWithMinVersion(minMambaVersion, rejectionLevel, "")
}

// usableCondaVersion applies the conda minimum to both conda and conda-standalone.
func usableCondaVersion(executable string) (*pep440.Version, error) {
	return condaCheck(log.InfoLevel)(executable)
}

func condaCheck(rejectionLevel log.Level) versionCheck {
	minCondaVersion, _ := pep440.Parse(DefaultMinCondaVersion)
	return executableWithMinVersion(minCondaVersion, rejectionLevel, "conda")
}

func EnsureConda(mamba bool, micromamba bool, conda bool, condaStandalone bool, noInstall bool) (string, error) {
	result, err := ensureCondaWithMetadata(mamba, micromamba, conda, condaStandalone, noInstall, false)
	if result == nil {
		return "", err
	}
	return result.Path, err
}

func validateForceInstall(micromamba, condaStandalone, noInstall, forceInstall bool) error {
	if forceInstall && noInstall {
		return errors.New("--force-install cannot be combined with --no-install")
	}
	if forceInstall && !micromamba && !condaStandalone {
		return errors.New("--force-install requires --micromamba or --conda-exe")
	}
	return nil
}

// ensureCondaWithMetadata keeps EnsureConda's order: each tool is searched for
// and, if installable, installed before the next tool is considered.
func ensureCondaWithMetadata(mamba, micromamba, conda, condaStandalone, noInstall, forceInstall bool) (*condaExecutable, error) {
	if err := validateForceInstall(micromamba, condaStandalone, noInstall, forceInstall); err != nil {
		return nil, err
	}
	var steps []resolutionStep
	for _, flavor := range enabledFlavors(mamba, micromamba, conda, condaStandalone) {
		if !forceInstall {
			steps = append(steps, resolutionStep{flavor: flavor})
		}
		if installable(flavor) && !noInstall {
			steps = append(steps, resolutionStep{flavor: flavor, install: true})
		}
	}
	return resolveSteps(steps, forceInstall)
}

// resolutionStep searches for an installed flavor, or runs its installer.
type resolutionStep struct {
	flavor  string
	install bool
}

func enabledFlavors(mamba, micromamba, conda, condaStandalone bool) []string {
	var flavors []string
	for _, tool := range []struct {
		flavor  string
		enabled bool
	}{{"mamba", mamba}, {"micromamba", micromamba}, {"conda", conda}, {"conda_standalone", condaStandalone}} {
		if tool.enabled {
			flavors = append(flavors, tool.flavor)
		}
	}
	return flavors
}

func installable(flavor string) bool {
	return flavor == "micromamba" || flavor == "conda_standalone"
}

func resolveSteps(steps []resolutionStep, forceInstall bool) (*condaExecutable, error) {
	dataDir := sitePath()
	minMambaVersion, _ := pep440.Parse(DefaultMinMambaVersion)
	rejectionLevel := log.InfoLevel
	if forceInstall {
		rejectionLevel = log.WarnLevel
	}
	// TODO: check $CONDA_EXE when searching for conda
	searchChecks := map[string]versionCheck{
		"mamba":            executableWithMinVersion(minMambaVersion, log.InfoLevel, "mamba", ""),
		"micromamba":       usableMicromambaVersion,
		"conda":            usableCondaVersion,
		"conda_standalone": usableCondaVersion,
	}
	var lastInvalidDownload error
	for i, step := range steps {
		if !step.install {
			log.Debugf("Checking for %s", step.flavor)
			if executable, _ := resolveExecutableWithVersion(step.flavor, dataDir, searchChecks[step.flavor]); executable != nil {
				return executable, nil
			}
			continue
		}
		var exe string
		var err error
		check := condaCheck(rejectionLevel)
		if step.flavor == "micromamba" {
			exe, err = installMicromambaForPlatform(forceInstall)
			check = micromambaCheck(rejectionLevel)
		} else {
			exe, err = installCondaStandalone(forceInstall)
		}
		var noCompatible *noCompatibleVersionError
		var notPublished *notPublishedError
		var invalidDownload *downloadedExecutableError
		switch {
		case errors.As(err, &noCompatible):
			log.StandardLogger().Log(rejectionLevel, err)
		case errors.As(err, &notPublished) || errors.As(err, &invalidDownload):
			if invalidDownload != nil {
				lastInvalidDownload = err
			}
			if remaining := remainingTools(steps[i+1:]); len(remaining) > 0 {
				log.Warnf("%v; trying %s instead", err, strings.Join(remaining, " or "))
			} else if notPublished != nil && lastInvalidDownload != nil {
				log.Warn(err)
				return nil, lastInvalidDownload
			} else {
				return nil, err
			}
		case err != nil:
			return nil, err
		default:
			if version, _ := check(exe); version != nil {
				return newCondaExecutable(exe, step.flavor, *version), nil
			}
		}
	}
	return nil, lastInvalidDownload
}

// remainingTools names the tools that later steps would try, in order.
func remainingTools(steps []resolutionStep) []string {
	var names []string
	for _, step := range steps {
		name := strings.ReplaceAll(step.flavor, "_", "-")
		if !slices.Contains(names, name) {
			names = append(names, name)
		}
	}
	return names
}

type ArchSpec struct {
	os   string
	arch string
}

func PlatformSubdir() (string, error) {
	return platformSubdir(runtime.GOOS, runtime.GOARCH)
}

func platformSubdir(goos string, goarch string) (string, error) {
	// Keep this in step with subdir_for and windows_subdir in
	// src/python/ensureconda/resolve.py. GOARCH can't tell ARMv6 from ARMv7,
	// and Go has no z/OS port, so those entries exist only there.
	platformMap := map[ArchSpec]string{
		{"darwin", "amd64"}:  "osx-64",
		{"darwin", "arm64"}:  "osx-arm64",
		{"linux", "386"}:     "linux-32",
		{"linux", "amd64"}:   "linux-64",
		{"linux", "arm64"}:   "linux-aarch64",
		{"linux", "ppc64"}:   "linux-ppc64",
		{"linux", "ppc64le"}: "linux-ppc64le",
		{"linux", "riscv64"}: "linux-riscv64",
		{"linux", "s390x"}:   "linux-s390x",
		{"windows", "386"}:   "win-32",
		{"windows", "amd64"}: "win-64",
		{"windows", "arm64"}: "win-arm64",
	}

	subdir, ok := platformMap[ArchSpec{goos, goarch}]
	if !ok {
		return "", fmt.Errorf("unsupported platform %s/%s: no conda subdir is known for it", goos, goarch)
	}
	return subdir, nil
}

// Execute executes the root command.
func Execute(version string) error {
	// Cobra adds --version; -v already belongs to --verbosity.
	rootCmd.Version = version
	return rootCmd.Execute()
}

func printExecutable(cmd *cobra.Command, executable *condaExecutable, asJSON bool) error {
	if asJSON {
		if !utf8.ValidString(executable.Path) {
			return fmt.Errorf("cannot print JSON: executable path is not valid UTF-8: %q", executable.Path)
		}
		encoded, err := json.Marshal(executable)
		if err != nil {
			return err
		}
		// Keep JSON ASCII-only so shells using a non-UTF-8 console encoding
		// preserve paths. Marshal already handled quoting and control bytes;
		// any remaining non-ASCII characters occur inside JSON strings.
		var ascii strings.Builder
		for _, r := range string(encoded) {
			switch {
			case r < utf8.RuneSelf:
				ascii.WriteByte(byte(r))
			case r <= 0xffff:
				fmt.Fprintf(&ascii, "\\u%04x", r)
			default:
				high, low := utf16.EncodeRune(r)
				fmt.Fprintf(&ascii, "\\u%04x\\u%04x", high, low)
			}
		}
		_, err = fmt.Fprintln(cmd.OutOrStdout(), ascii.String())
		return err
	}
	_, err := fmt.Fprint(cmd.OutOrStdout(), executable.Path)
	return err
}

func er(msg interface{}) {
	log.Error(msg)
	os.Exit(1)
}

func evaluateFlagPair(cmd *cobra.Command, flag string) (bool, error) {
	posFlag := cmd.Flag(flag)
	negFlag := cmd.Flag("no-" + flag)
	if posFlag.Changed && negFlag.Changed {
		return false, errors.New("flags are mutually exclusive")
	}
	negVal, err := strconv.ParseBool(negFlag.Value.String())
	if err != nil {
		return false, err
	}
	if negVal {
		return false, nil
	}
	return cmd.Flags().GetBool(flag)
}

func init() {
	rootCmd.PersistentFlags().Bool("mamba", true, "Search for mamba")
	rootCmd.PersistentFlags().Bool("no-mamba", false, "")

	rootCmd.PersistentFlags().Bool("micromamba", true, "Search for micromamba, Can install")
	rootCmd.PersistentFlags().Bool("no-micromamba", false, "")

	rootCmd.PersistentFlags().Bool("conda", true, "Search for conda")
	rootCmd.PersistentFlags().Bool("no-conda", false, "")

	rootCmd.PersistentFlags().Bool("conda-exe", true, "Search for conda.exe/ conda standalong.  Can install")
	rootCmd.PersistentFlags().Bool("no-conda-exe", false, "")

	rootCmd.PersistentFlags().Bool("no-install", false, "Don't install stuff")
	rootCmd.PersistentFlags().Bool("force-install", false, "Skip existing executables and download a fresh copy")
	rootCmd.PersistentFlags().Bool("json", false, "Print the executable's path, flavor and version as JSON")

	rootCmd.PersistentFlags().IntP("verbosity", "v", 1, "verbosity level (0-3)")

}
