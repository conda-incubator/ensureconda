//go:build windows

package cmd

// Windows reports a file's creation time where Unix has its change time, and
// changes of contents or attributes leave it alone, so nothing is reused there.
func fileFingerprint(string) (fingerprint, bool) {
	return fingerprint{}, false
}
