#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="https://github.com/Anshieee/opencode-team-room.git"
VERSION="v0.1.0"

for command in git python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$command" >&2
    exit 1
  fi
done

temporary="$(mktemp -d "${TMPDIR:-/tmp}/opencode-team-room.XXXXXXXX")"
trap 'rm -rf "$temporary"' EXIT
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$VERSION" "$REPOSITORY" "$temporary/source"
python3 "$temporary/source/install.py" "$@"
