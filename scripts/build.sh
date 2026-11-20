#!/usr/bin/env bash
# Build the blob and dinput8.dll into build/blob. Windows: VC7.1 comes from
# upstream's Open-BFME-1 submodule, the loader needs Visual Studio's x86 tools.
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/prepare.sh
(
    cd upstream
    mapfile -t sources < <(git grep -l BFME2_MODERN -- 'Code/*.cpp' 'Code/*.c')
    # Reference objects first: a modern object built before them counts as stale.
    python tools/build.py "${sources[@]}"
    python tools/build_variant.py --compile "${sources[@]}" > /dev/null
)
python builder/modern_blob.py --target bfme2-106 --loader --out build/blob
# The RotWK 2.01-engine targets replay their recorded descriptions (targets/).
for target in bfme2-109 rotwk-201; do
    python builder/modern_blob.py --target "$target" --out build/blob
done
