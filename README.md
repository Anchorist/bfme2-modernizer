# BFME II modernizer (1.06)

Smoother presentation for *The Battle for Middle-earth II* 1.06, built from
the modern variant of the [Open-BFME-2](https://github.com/Open-BFME/Open-BFME-2)
decompilation:

- 120 FPS presentation; game logic keeps its original speed;
- smooth units, animations, projectiles, construction and selection circles;
- Hor+ widescreen and twice the zoom-out range;
- HUD and menus at their own proportions, the palantir and side command bar
  at the screen edges;
- every display mode in the options menu, including wide ones.

## Install

1. You need BFME II 1.06 with this `game.dat` (the BFME All In One Launcher's
   1.06 installs it):
   `SHA-256 f008b587570bad693981dc7218588c81d192a1e064b0f7f861539c51156a7640`.
   Any other `game.dat` is left alone and the game runs unmodified.
2. Copy `dinput8.dll` and `bfme2-modern-106.blob` into the game folder, next to
   `game.dat`.
3. Start the game as usual.

`game.dat` is never modified. To uninstall, delete the two files.

## Settings

Set these as environment variables before starting the game:

| Variable | Effect |
| --- | --- |
| `BFME2_MODERN_FPS` | presentation rate, default 120; `0` follows the display; `legacy` turns it off |
| `BFME2_MODERN_HUD=stretch` | retail's stretched HUD |
| `BFME2_MODERN_HUD_EDGES=0` | keep the palantir and side bar in the centred HUD box |
| `BFME2_MODERN_HORPLUS=0` | retail's field of view on wide screens |
| `BFME2_MODERN_ZOOM=0` | retail's zoom-out limit |
| `BFME2_MODERN_TRACE=1` | write `modern_trace.csv` (pacing events) |
| `BFME2_MODERN_DISABLE=1` | do not load the modernizer |

Each start writes `bfme2-modern.log` in the game folder: which blob was used,
and why the modernizer stayed off if it did.

## Known limits

- Only BFME II 1.06. The 1.09 community patch and RotWK are not supported yet.
- Not tested online. Game logic is unchanged, but mixed games with unmodified
  players have not been played.
- A launcher that syncs the game folder (switching patch or mod) may remove the
  two files; copy them again afterwards.
- Health bars over hordes and arrow streaks still move at the logic rate.

## How it is built

| Path | What it is |
| --- | --- |
| `upstream/` | Open-BFME-2 (submodule) at a pinned commit: the byte-matched source of game.dat 1.06, its ledger and its VC7.1 toolchain |
| `patches/modern-variant.patch` | the modern variant: every change sits under `#if defined(BFME2_MODERN)`, so the reference build of each file still matches retail |
| `builder/modern_blob.py` | compiles nothing itself; pairs each changed file's reference and modern objects, links the functions that differ into a blob for retail 1.06 and checks every relocation after a move |
| `loader/` | `dinput8.dll`: game.dat imports one function from it, so Windows loads it first; it maps the blob and jumps each changed function's retail entry to its modern copy |

On Windows with Git Bash, Python 3.12 and Visual Studio's C++ x86 tools:

```sh
git clone https://github.com/Anchorist/bfme2-modernizer.git
cd bfme2-modernizer
scripts/prepare.sh        # checks out upstream/ and its toolchain, applies the patch
python -m pip install -r upstream/reference/open-bfme-1/tools/requirements.txt
scripts/build.sh          # runs prepare.sh, then writes build/blob/dinput8.dll and bfme2-modern-106.blob
python -m pytest -q tests
```

`.github/workflows/build.yml` runs the same on every push and publishes a
release for a `v*` tag. Moving to the newer
[BFME2-Rebuild](https://github.com/Open-BFME/BFME2-Rebuild) is planned once it
has every function the modern variant changes
([BFME2-Rebuild#20](https://github.com/Open-BFME/BFME2-Rebuild/issues/20)).

## Licence

GPL-3.0, as Open-BFME-2. Not affiliated with or endorsed by Electronic Arts.
