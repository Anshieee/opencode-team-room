#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="https://github.com/Anshieee/opencode-team-room.git"
VERSION="v0.1.1"

for command in git python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$command" >&2
    exit 1
  fi
done

temporary="$(mktemp -d "${TMPDIR:-/tmp}/opencode-team-room.XXXXXXXX")"
trap 'rm -rf "$temporary"' EXIT
git init --quiet "$temporary/source"
git -C "$temporary/source" remote add origin "$REPOSITORY"
git -C "$temporary/source" fetch --quiet --depth 1 origin "refs/tags/$VERSION:refs/tags/$VERSION"
git -C "$temporary/source" checkout --quiet --detach "$VERSION^{commit}"
python3 "$temporary/source/install.py" "$@"
