# BFME II modernizer

Smoother presentation for *The Battle for Middle-earth II* 1.06 and 1.09 and
*The Rise of the Witch-king* 2.01, built from the modern variant of the
[Open-BFME-2](https://github.com/Open-BFME/Open-BFME-2) decompilation:

- 120 FPS presentation; game logic keeps its original speed;
- smooth units, animations, projectiles, construction and selection circles;
- Hor+ widescreen and twice the zoom-out range;
- HUD and menus at their own proportions, the palantir and side command bar
  at the screen edges;
- every display mode in the options menu, including wide ones.

## Install

1. Take the zip for your game. The modernizer only starts with this exact
   `game.dat`; any other is left alone and the game runs unmodified.

   | Zip | Game | `game.dat` SHA-256 |
   | --- | --- | --- |
   | `bfme2-106` | BFME II 1.06 | `f008b587570bad693981dc7218588c81d192a1e064b0f7f861539c51156a7640` |
   | `bfme2-109` | BFME II 1.09 (community patch) | `01ad4ce0d6177d23f6d5055ea83512311c4412d23e4b8cb1b1e273f06dd2465f` |
   | `rotwk-201` | The Rise of the Witch-king 2.01 | `5481de75b63e22483660ea8097478faef594f90b9ade7de893e092256ba68a26` |

   The BFME All In One Launcher installs these versions.
2. Copy `dinput8.dll` and the `.blob` into the game folder, next to `game.dat`.
3. Start the game as usual.

`game.dat` is never modified. To uninstall, delete the two files.

## Settings

Set these as environment variables before starting the game:

| Variable | Effect |
| --- | --- |
| `BFME2_MODERN_FPS` | presentation rate, default 120; `0` follows the display; `legacy` turns it off |
| `BFME2_MODERN_INTERPOLATE=0` | present at the modern rate but show only the latest update (no smoothing), for comparisons |
| `BFME2_MODERN_HUD=stretch` | retail's stretched HUD |
| `BFME2_MODERN_HUD=4:3` or `16:9` | 1.09 and RotWK: the HUD box's shape. 1.09's HUD art is drawn for 16:9, so 1.09 defaults to 16:9 and RotWK to 4:3 |
| `BFME2_MODERN_HUD_EDGES=0` | keep the palantir and side bar in the centred HUD box |
| `BFME2_MODERN_HORPLUS=0` | retail's field of view on wide screens |
| `BFME2_MODERN_ZOOM=0` | retail's zoom-out limit |
| `BFME2_MODERN_TRACE=1` | write `modern_trace.csv` (pacing events) |
| `BFME2_MODERN_DISABLE=1` | do not load the modernizer |

Each start writes `bfme2-modern.log` in the game folder: which blob was used,
and why the modernizer stayed off if it did.

## Known limits

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
| `patches/layout-201.patch` | the few classes the RotWK 2.01 engine (also BFME II 1.09's game.dat) lays out longer, under `BFME2_LAYOUT_201` |
| `targets/*.json` | what linking against the 1.09 and RotWK game.dat read from it: addresses, the hooked functions' first 5 bytes and the file's hash, so a build needs neither game.dat |
| `builder/modern_blob.py` | pairs each changed file's reference and modern objects, links the functions that differ into a blob for one game.dat and checks every relocation after a move |
| `loader/` | `dinput8.dll`: game.dat imports one function from it, so Windows loads it first; it maps the blob and jumps each changed function's retail entry to its modern copy |

On Windows with Git Bash, Python 3.12 and Visual Studio's C++ x86 tools:

```sh
git clone https://github.com/Anchorist/bfme2-modernizer.git
cd bfme2-modernizer
scripts/prepare.sh        # checks out upstream/ and its toolchain, applies the patch
python -m pip install -r upstream/reference/open-bfme-1/tools/requirements.txt
scripts/build.sh          # runs prepare.sh, then writes build/blob/dinput8.dll and the three blobs
python -m pytest -q tests
```

When the modern code needs something new from the 1.09 or RotWK game.dat, the
replay stops with "run --describe"; describe that target again from a copy of
its game.dat and commit the new `targets/<target>.json`:

```sh
python builder/modern_blob.py --target rotwk-201 --describe /path/to/game.dat
```

`.github/workflows/build.yml` runs the same on every push and publishes a
release for a `v*` tag. Moving to the newer
[BFME2-Rebuild](https://github.com/Open-BFME/BFME2-Rebuild) is planned once it
has every function the modern variant changes
([BFME2-Rebuild#20](https://github.com/Open-BFME/BFME2-Rebuild/issues/20)).

## Licence

GPL-3.0, as Open-BFME-2. Not affiliated with or endorsed by Electronic Arts.
