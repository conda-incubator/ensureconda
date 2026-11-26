//go:build unix

package cmd

import "golang.org/x/sys/unix"

func fileFingerprint(path string) (fingerprint, bool) {
	var st unix.Stat_t
	if err := unix.Stat(path, &st); err != nil {
		return fingerprint{}, false
	}
	return fingerprint{
		dev: uint64(st.Dev), ino: uint64(st.Ino), size: st.Size,
		mtime: st.Mtim.Nano(), ctime: st.Ctim.Nano(),
	}, true
}
