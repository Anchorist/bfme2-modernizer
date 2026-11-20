#!/usr/bin/env bash
# Check out upstream/ (Open-BFME-2 at the pinned commit) with its VC7.1
# toolchain and apply the patches to it. upstream/ is a build tree: every run
# resets its tracked files and drops untracked ones (build outputs, which git
# ignores, stay).
set -euo pipefail
cd "$(dirname "$0")/.."
git submodule update --init upstream
# Only this level (Open-BFME-1 references Open-BFME-2 in turn), and only the
# pinned commit: its full history is several GB.
git -C upstream submodule update --init --depth 1 reference/open-bfme-1
git -C upstream reset -q --hard
git -C upstream clean -q -fd -- Code tools
# The modern variant, then the RotWK 2.01 engine's layout (active only under
# BFME2_LAYOUT_201, so the 1.06 build is unchanged).
for patch in modern-variant layout-201; do
    git -C upstream apply --whitespace=nowarn "$PWD/patches/$patch.patch"
done
