module example.com/app

go 1.22

toolchain go1.22.5

require (
	github.com/pkg/errors v0.9.1
	github.com/old/dep v0.1.0
	golang.org/x/mod v0.17.0 // indirect
)

require github.com/spf13/cobra v1.8.0

// keep new/dep pinned until upstream merges the patch
replace github.com/old/dep => github.com/new/dep v1.2.3

replace github.com/pinned/dep v1.0.0 => github.com/pinned/dep v1.0.4

replace github.com/local/dep => ./internal/local/dep
