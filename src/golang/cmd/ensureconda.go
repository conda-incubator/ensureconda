package cmd

import (
	"errors"
	"fmt"
	"os"
	"runtime"
	"strconv"

	"github.com/Wessie/appdirs"
	"github.com/hashicorp/go-version"
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

			executable, err := EnsureConda(mamba, micromamba, conda, condaExe, true)
			if err != nil {
				panic(err)
			}

			if executable != "" {
				log.Debugf("Found executable %s", executable)
				fmt.Print(executable)
				os.Exit(0)
			}
			if !noInstall {
				log.Debugf("Attempting to install")
				executable, err = EnsureConda(mamba, micromamba, conda, condaExe, noInstall)
				if err != nil {
					er(err)
				}
				if executable != "" {
					log.Debugf("Found executable after installing %s", executable)
					fmt.Print(executable)
					os.Exit(0)
				}
			}
			os.Exit(1)
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
	return appdirs.UserDataDir("ensure-conda", "", "", false)
}

func EnsureConda(mamba bool, micromamba bool, conda bool, condaStandalone bool, noInstall bool) (string, error) {
	var executable string
	dataDir := sitePath()
	minMambaVersion, _ := version.NewVersion(DefaultMinMambaVersion)
	minCondaVersion, _ := version.NewVersion(DefaultMinCondaVersion)

	microMambaVersionCheck := executableHasMinVersion(minMambaVersion, "")
	condaVersionCheck := executableHasMinVersion(minCondaVersion, "conda")
	mambaVersionCheck := func(executable string) (bool, error) {
		log.WithFields(log.Fields{
			"executable":      executable,
			"minMambaVersion": minMambaVersion.String(),
		}).Debug("Starting verbose mamba version check")
		log.Debug("Attempting v1 style version check (prefix 'mamba')")
		v1Check, err := executableHasMinVersion(minMambaVersion, "mamba")(executable)
		if err != nil {
			log.WithError(err).Error("v1 style check encountered an error")
			return false, fmt.Errorf("v1 style check failed: %w", err)
		}
		log.WithField("v1Result", v1Check).Debug("v1 style check result")
		if v1Check {
			log.Debug("v1 style check succeeded")
			return true, nil
		}
		log.Debug("v1 style check did not succeed; attempting micromamba style check (empty prefix)")
		v2Check, err := executableHasMinVersion(minMambaVersion, "")(executable)
		if err != nil {
			log.WithError(err).Error("micromamba style check encountered an error")
			return false, fmt.Errorf("micromamba style check failed: %w", err)
		}
		log.WithField("v2Result", v2Check).Debug("micromamba style check result")
		if v2Check {
			log.Debug("micromamba style check succeeded")
			return true, nil
		}
		log.Debug("Neither v1 nor micromamba style checks succeeded; returning false")
		return false, nil
	}

	if mamba {
		log.Debug("Checking for mamba")
		executable, _ = ResolveExecutable("mamba", dataDir, mambaVersionCheck)
		if executable != "" {
			return executable, nil
		}
	}
	if micromamba {
		log.Debug("Checking for micromamba")
		executable, _ = ResolveExecutable("micromamba", dataDir, microMambaVersionCheck)
		if executable != "" {
			return executable, nil
		}
		if !noInstall {
			exe, err := InstallMicromamba()
			if err != nil {
				return "", err
			}
			if valid, _ := microMambaVersionCheck(exe); valid {
				return exe, nil
			}
		}
	}
	if conda {
		log.Debug("Checking for conda")
		// TODO: check $CONDA_EXE
		executable, _ = ResolveExecutable("conda", dataDir, condaVersionCheck)
		if executable != "" {
			return executable, nil
		}
	}
	if condaStandalone {
		log.Debug("Checking for conda_standalone")
		executable, _ = ResolveExecutable("conda_standalone", dataDir, condaVersionCheck)
		if executable != "" {
			return executable, nil
		}
		if !noInstall {
			exe, err := InstallCondaStandalone()
			if err != nil {
				return "", err
			}

			if valid, _ := condaVersionCheck(exe); valid {
				return exe, nil
			}
		}
	}

	return "", nil
}

type ArchSpec struct {
	os   string
	arch string
}

func PlatformSubdir() string {
	os_ := runtime.GOOS
	arch := runtime.GOARCH

	platformMap := map[ArchSpec]string{
		{"darwin", "amd64"}:  "osx-64",
		{"darwin", "arm64"}:  "osx-arm64",
		{"linux", "amd64"}:   "linux-64",
		{"linux", "arm64"}:   "linux-aarch64",
		{"linux", "ppc64le"}: "linux-ppc64le",
		{"windows", "amd64"}: "win-64",
	}

	return platformMap[ArchSpec{os_, arch}]
}

// Execute executes the root command.
func Execute() error {
	return rootCmd.Execute()
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

	// TODO: implement logger + verbosity
	rootCmd.PersistentFlags().IntP("verbosity", "v", 1, "verbosity level (0-3)")

}
