#!/usr/bin/env bash
# Check out upstream/ (Open-BFME-2 at the pinned commit) with its VC7.1
# toolchain and apply the modern variant to it. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/.."
git submodule update --init upstream
# Only this level: Open-BFME-1 references Open-BFME-2 in turn.
git -C upstream submodule update --init reference/open-bfme-1
patch="$PWD/patches/modern-variant.patch"
if git -C upstream apply --check --reverse "$patch" 2>/dev/null; then
    echo "modern variant already applied"
else
    git -C upstream apply --whitespace=nowarn "$patch"
fi
