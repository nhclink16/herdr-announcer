#!/bin/sh
set -eu

# During cutover prep, herdr-plugin.toml.v2 is the staged 1.0 manifest while
# herdr-plugin.toml deliberately remains the live Python 0.9.1 manifest. In
# that state, enforce Cargo == staged manifest == top versioned CHANGELOG
# heading. After cutover (`mv herdr-plugin.toml.v2 herdr-plugin.toml`), the
# staged file no longer exists and the same rule enforces Cargo == live
# manifest == top versioned CHANGELOG heading.

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cargo_version=$(awk '
  /^\[package\]$/ { package = 1; next }
  /^\[/ && package { exit }
  package && /^version[[:space:]]*=/ {
    value = $0
    sub(/^[^=]*=[[:space:]]*"/, "", value)
    sub(/".*/, "", value)
    print value
    exit
  }
' "$root/Cargo.toml")
version_from_manifest() {
  awk '
  /^version[[:space:]]*=/ {
    value = $0
    sub(/^[^=]*=[[:space:]]*"/, "", value)
    sub(/".*/, "", value)
    print value
    exit
  }
  ' "$1"
}

changelog_version=$(sed -n 's/^## \[\([0-9][^]]*\)\].*/\1/p' "$root/CHANGELOG.md" | sed -n '1p')
if [ -z "$changelog_version" ]; then
  echo "version check: CHANGELOG.md needs a versioned heading" >&2
  exit 1
fi

if [ -f "$root/herdr-plugin.toml.v2" ]; then
  manifest="$root/herdr-plugin.toml.v2"
  manifest_label=herdr-plugin.toml.v2
  state="cutover pending"
else
  manifest="$root/herdr-plugin.toml"
  manifest_label=herdr-plugin.toml
  state="live"
fi
plugin_version=$(version_from_manifest "$manifest")

if [ "$cargo_version" != "$plugin_version" ] || [ "$cargo_version" != "$changelog_version" ]; then
  echo "version mismatch: Cargo.toml=$cargo_version $manifest_label=$plugin_version CHANGELOG.md=$changelog_version" >&2
  exit 1
fi

echo "version check: $cargo_version ($state; checked $manifest_label)"
