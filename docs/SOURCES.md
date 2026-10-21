# Sources and coverage matrix

**English** · [简体中文](SOURCES.zh-CN.md)

Every way intelbrew can obtain a binary, what it guarantees, and whether it has been
exercised. “Tested” means: run end-to-end on an Intel Mac (macOS 13.7, Ventura) — not
“should work”.

## Source table

| Source (`Candidate.source`) | Used for | Contract | Integrity | Provenance | Status |
|---|---|---|---|---|---|
| `registry` | anything with a curated recipe | same | sha256 (recipe) | optional GPG/codesign | tested (`doctor`) |
| `brew-bottle` | formula has a bottle for this or an older macOS | same | Homebrew bottle digests | Homebrew / ghcr TLS | implemented; download speed depends on route to ghcr.io |
| `node-dist` | `node`, `node@18/20/22/24` | same | sha256 from `SHASUMS256.txt` | Apple signature `HX7739G8FX` verified | **tested** |
| `github-release` | single-binary CLIs (uv, ripgrep, fd, jq, gh, …) | same | sha256 from `asset.digest` | none (digest only) | **tested** |
| `adoptium-jdk` | `openjdk`, `openjdk@21` | same | sha256 from Adoptium API | Temurin builds | implemented |
| `pypi-zipapp` / `pypi-wheel` | Python CLIs (poetry, pre-commit, …) | same CLI | sha256 from PyPI JSON | none | implemented |
| `conda-forge` | ffmpeg, imagemagick, redis, sqlite, … | different runtime | sha256 from repodata | conda-forge | implemented, partial |
| `npm-global` | JS tools only (allowlist) | npm CLI | sha1 from registry | none | **tested** |

## Archive formats

| Format | Support | Notes |
|---|---|---|
| `.tar.gz` / `.tgz` | ✅ | universal |
| `.tar.xz` / `.txz` | ✅ | stdlib `lzma` |
| `.tar.bz2` | ✅ | stdlib |
| `.tar.zst` | ✅ | Python ≥ 3.14 native, else `zstandard` package |
| `.zip` (incl. zip64) | ✅ | symlinks restored from unix mode bits |
| `.pkg` / `.dmg` | ❌ | installers, not archives — refused on purpose |
| `.7z` / `.rar` | ❌ | refused |

## Platform matrix

| Host | `ibrew install` upstream | `brew` shim | Managed `node` | Notes |
|---|---|---|---|---|
| Intel macOS 12–15 | ✅ | ✅ | ✅ | primary target |
| Intel macOS 10.16–11 | ✅ | ⚠️ older bottles only | ⚠️ Node ≤ 18 line | upstream may drop old builds |
| Apple Silicon macOS 14–26 | ✅ (arm64 assets) | ✅ (usually unnecessary: bottles exist) | ✅ | works, but Homebrew already ships bottles |
| macOS 26 (Tahoe) Intel | ⚠️ | ⚠️ | ⚠️ | upstream Intel builds are getting rare; `ibrew census` will show it |
| Linux x86_64 / arm64 | ⚠️ | ⚠️ | ✅ Node mirror logic works | only the brew shim / node parts are meaningful |

## Node.js ceilings (upstream, not intelbrew's choice)

`ibrew node` picks the newest release whose **official** `darwin-x64` tarball declares a
compatible minimum macOS. As of the last validation run on Ventura 13.7 x86_64:

| Node major | darwin-x64 available | Runs on Ventura 13 |
|---|---|---|
| 18 | ✅ (EOL) | ✅ |
| 20 | ✅ | ✅ |
| 22 | ✅ | ✅ |
| 24 | ✅ | ✅ (`minos 13.5`) |
| 26 | ✅ | ✅ (`minos 13.5`, verified 2026-10-21) |

The table is *observed*, not assumed: `ensure_runtime` reads the Mach-O deployment target
(`otool -l … minos`) of the build it just downloaded and discards it if it needs a newer
macOS than yours, then tries the next older release. `ibrew doctor` prints the same
conclusion per machine (`node.ceiling`), so you never have to guess which major “should”
be installable. (Earlier versions of this document claimed Node 25+ needed macOS 15 — the
actual `v26.12.0-darwin-x64` binary declares `minos 13.5` and runs on Ventura. Hence the
gate is measured, never hardcoded.)

## What a source can never change

* **Which bytes** are installed — mirrors are interchangeable only because of the digest.
* **The contract** — an `npm` or `conda-forge` install is labelled as such in `ibrew plan`
  and can never be reported as `same-contract`.
* **Homebrew's bookkeeping** — only `brew install` writes into `Cellar`; everything else
  goes to `~/.intelbrew/root`.
