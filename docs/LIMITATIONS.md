# Limitations — what is supported and what is not

**English** · [简体中文](LIMITATIONS.zh-CN.md)

This list is intentional. Every row describes behaviour you will actually observe, not a
wish. If a case is missing here, treat it as unsupported until `ibrew doctor` proves it.

## 1. Not supported, by design

| Case | Behaviour | Why |
|---|---|---|
| Compiling from source | never happens in `ibrew install`; the `brew` shim only compiles when *you* pass `--build-from-source` | compiling is the problem, not the fallback |
| `.pkg` / `.dmg` / `.msi` payloads | refused | installers need interaction and system-wide writes; not idempotent |
| macOS GUI applications | refused | use `brew install --cask`; apps are not prefix-installable |
| Unsigned downloads without a digest | refused unless `IBREW_ALLOW_UNVERIFIED=1` | unverifiable bytes are not an install |
| Arbitrary brew name → npm | refused | npm hosts squatted placeholders (`uvicorn@0.0.1-security`) |
| Linux-only projects | refused with a pointer to the playbook | no macOS binary exists |
| Replacing Homebrew's dependency graph | not attempted | dependencies come from the same source as the package: a bottle brings its own, upstream builds are self-contained |
| Private/proprietary binaries | not resolved automatically | you can add a recipe with `ibrew add` |

## 2. Supported with caveats

| Case | Caveat |
|---|---|
| Bottle built for an **older** macOS | may use symbols missing on your system; `ibrew install` reads the Mach-O `minos` and warns, but cannot prove runtime correctness. Status is `probable`, not `verified` |
| Bottle built for a **newer** macOS | rejected outright — this is the one case where guessing is always wrong |
| `python@3.x` | routed to upstream *standalone* Python builds. `pyenv`/`uv` interoperate; `brew`'s `python3` symlinks and site-packages are **not** recreated |
| `ffmpeg`, `imagemagick`, `redis`, … | conda-forge builds run with their own library set. They do **not** link against Homebrew's `libvpx`/`openssl`/`jpeg`; this is a feature (no compile, no dependency explosion) and a limitation (no `brew` formula parity) |
| A Node major whose x64 build needs a newer macOS | `ibrew node` reads `minos` from the downloaded build, discards it and tries the next older release; the ceiling is measured per machine, never assumed |
| Node 18 + npm 11 | npm ≥ 11 declares `engines.node >= 20`; the shim falls back to the system npm when available, instead of hiding the conflict |
| GPG verification | needs `gpg` installed and a keyring; without it the report says `digest-only`, never `verified` |
| `codesign` checks | prove *who* signed, not that the code is safe; most OSS tools ship ad-hoc signatures |
| Apple Silicon | supported but mostly unnecessary (Homebrew already ships bottles); the tool prefers arm64 assets automatically |
| Renewed install after `brew update` | a formula may gain a native bottle again (or lose one); re-run `ibrew census` — decisions are recomputed from live data, never cached as policy |

## 3. Things that are out of scope for *any* tool

* **A network with no route to any mirror.** Mirror racing picks the fastest working one; it
  cannot invent one. `ibrew doctor` tells you which endpoints are dead.
* **A project that stopped publishing Intel binaries.** Nothing can conjure a binary; the
  honest options are: an older version, a container, or compile.
* **Kernel/driver-level software** (kexts, system extensions, virtualization drivers) —
  SIP and notarization decide, not a package manager.
* **Bit-for-bit parity with a Homebrew build.** Different build flags, different OpenSSL,
  different paths. For reproducibility, pin the recipe's `sha256`.

## 4. Security model and its holes

* Digests prove *the bytes you got are the bytes the source published*; they do not prove
  the publisher is benevolent.
* TLS to `nodejs.org`, `github.com`, `ghcr.io`, `pypi.org` is the trust anchor. A
  compromised mirror that also compromises the metadata endpoint wins — this is why the
  metadata and the artifact are fetched from *different* hosts when possible.
* `IBREW_ALLOW_UNVERIFIED=1` exists for emergencies; it prints `unverified` loudly and
  refuses to record `verified` in a recipe.
* GitHub proxies (`gh-proxy.com`, `ghfast.top`) are third parties that see your request
  path. They cannot change bytes without failing the digest, but they do see *what* you
  download. Disable with `IBREW_GITHUB_MIRRORS=` (empty).
* Archives are extracted with a hardened extractor, but a malicious *binary* still runs.
  Verification narrows who could have authored it; it does not sandbox it.

## 5. Known rough edges (tracked, not hidden)

* `ghcr.io` downloads are slow on some CN routes; the Homebrew-bottle route is therefore
  the slowest path and is chosen last.
* `conda-forge` route uses `current_repodata.json` (small). Package sets that only appear
  in full repodata are not resolved.
* `pypi` zipapp route depends on the project shipping a `.app`/wheel; otherwise it falls
  back to GitHub releases.
* `ibrew doctor` on a cold cache can take a minute (formula index is ~30 MB).
* Recipe coverage is small on purpose: a wrong recipe is worse than no recipe.
