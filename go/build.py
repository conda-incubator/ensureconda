import os
import subprocess


build_arches = [
    ["linux", "amd64"],
    ["linux", "arm64"],
    ["linux", "ppc64le"],
    ["darwin", "amd64"],
    # https://github.com/golang/go/issues/38485
    # ["darwin", "arm64"],
    ["windows", "amd64"],
    # ["windows", "arm64"],
]
for goos, goarch in build_arches:
    env = dict(os.environ)
    env["GOOS"] = goos
    env["GOARCH"] = goarch
    ext = ".exe" if goos == "windows" else ""

    print(f"Building {goos}-{goarch}")
    subprocess.run(
        [
            "go",
            "build",
            "-v",
            "-o",
            f"ensureconda-{goos}-{goarch}{ext}",
            "-ldflags",
            "-s -w",
        ],
        env=env,
    )
