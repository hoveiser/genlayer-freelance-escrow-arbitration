#!/usr/bin/env bash
# One-time local cache seeding for GenLayer Direct-mode tests.
#
# genlayer-test's direct runner resolves GitHub's "latest" genvm release and
# downloads the asset `genvm-universal.tar.xz` (unversioned name). Recent
# releases ship the bundle as `genvm-universal-vX.Y.Z.tar.xz` (versioned), so
# the runner's download 404s. genvm-lint already downloads the correct bundle
# (and it contains the runner version this contract pins), so we seed the
# gltest cache from it.
#
# Prerequisite: run `genvm-lint check contracts/freelance_escrow.py` once so
# the bundle exists in ~/.cache/genvm-linter/.
set -euo pipefail

LINTER_CACHE="$HOME/.cache/genvm-linter"
GLTEST_CACHE="$HOME/.cache/gltest-direct"

latest_bundle="$(ls -1 "$LINTER_CACHE"/genvm-universal-*.tar.xz 2>/dev/null | sort -V | tail -1 || true)"
if [ -z "$latest_bundle" ]; then
  echo "No genvm bundle found in $LINTER_CACHE." >&2
  echo "Run 'genvm-lint check contracts/freelance_escrow.py' first." >&2
  exit 1
fi

mkdir -p "$GLTEST_CACHE"
name="$(basename "$latest_bundle")"
if [ ! -e "$GLTEST_CACHE/$name" ]; then
  ln "$latest_bundle" "$GLTEST_CACHE/$name" 2>/dev/null || cp "$latest_bundle" "$GLTEST_CACHE/$name"
  echo "Seeded $GLTEST_CACHE/$name"
else
  echo "Already seeded: $GLTEST_CACHE/$name"
fi
