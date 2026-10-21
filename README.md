# intelbrew

**English** · [简体中文](README.zh-CN.md)

> Keep `brew`, `node`, `npm` and `npx` working on Intel Macs whose macOS is newer than
> the last macOS Homebrew still ships Intel bottles for — by switching each install to an
> **official binary build from another source**, and verifying it on arrival.

Homebrew 4/5+ ships prebuilt bottles only for Apple Silicon and for the newest Intel
macOS. On an older Intel Mac (e.g. Ventura 13 on a 2015–2018 MacBook Pro) almost every
`brew install` falls back to **compiling from source** — hours per tool, and frequent
build failures. intelbrew does not fork Homebrew and does not need a compiler: it plans a
**binary route** for each package, verifies the bytes, and installs them in a way that
keeps the usual commands working.

```
brew install node      →  Node.js official darwin-x64 build   (signed, SHA256-checked)
ibrew install ripgrep  →  upstream GitHub release asset       (SHA256-checked)
ibrew install uv       →  Astral's official macOS build        (SHA256-checked)
ibrew install ffmpeg   →  conda-forge osx-64 package           (SHA256-checked)
```

---

## 1. The problem (measured, not guessed)

`ibrew census` asks the Homebrew API which formulas have a bottle for **this exact
machine**:

```console
$ ibrew census
macOS 13.7.8 x86_64: bottle tag 'ventura'
  native bottles   1234 / 7358  (16.8%)
  would compile    6124
```

So on Ventura x86_64 **~83 % of `brew install` invocations compile**. Same for the
popular ones: `node`, `python@3.12/3.13`, `ffmpeg`, `go`, `git`, `postgresql`, `redis`,
`imagemagick`, `nginx`, `rust` — no `ventura` bottle. Only a handful of stale versioned
formulas (`node@18`, `icu4c@76`) still carry one.

Two things make this worse and are handled here:

* `brew` decides "bottle" from its own platform tag, which never targets a platform it no
  longer publishes for — so **no flag** brings a bottle back.
* Old bottles that *do* exist on `ghcr.io` are useless without Homebrew's relocation
  metadata (`.brew/` receipts); a bare `tar xzf` of a bottle leaves a broken install.

## 2. What intelbrew does

| Command | Behaviour |
|---|---|
| `ibrew doctor` | Verifies the machine, Homebrew, network, mirrors, proxies and every bundled recipe (downloads, extracts, executes, re-signs the recipe) |
| `ibrew census` | How many formulas are binaries vs. "would compile" on this machine |
| `ibrew plan <pkg>…` | Dry run: which source, which version, which verification, which contract |
| `ibrew install <pkg>…` | Installs from the plan; **never compiles**; digest-verified |
| `ibrew node install/use <major>` | Official Node.js build (mirror-raced), plus `node`/`npm`/`npx` shims |
| `ibrew add <cmd> <url> --sha256 …` | Turns a verified archive (or a bare binary with `--raw`) into a recipe, validated immediately |
| `ibrew setup --apply` | One-shot: Node runtime + shims + `brew` shim + shell profile |
| `ibrew status` / `ibrew env` / `ibrew verify` | Inspect, shell integration, standalone integrity report |

The `brew` shim keeps Homebrew's interface and reroutes **only** the installs Homebrew
would compile:

```console
$ brew install node            # goes through ~/.intelbrew/brew-bin/brew
==> node: Homebrew has no ventura bottle — using node-dist (same-contract, verified)
✓ node v24.22.0 — digest-only: sha256 [prefix]
```

`brew list`, `brew info`, casks, taps, `--build-from-source` and every other subcommand
are passed through to the real `brew` untouched.

## 3. Install

```bash
curl -fsSL https://raw.githubusercontent.com/Adminote/ventura-x86-recipe/main/install.sh | sh
# or from a checkout:
./install.sh --prefix ~/.local

# the installer writes a PATH block into your shell profile; load it in *this* shell
source ~/.zshrc            # zsh; use ~/.bash_profile on bash
ibrew doctor               # read this before installing anything
ibrew setup --apply        # Node runtime, node/npm/npx shims, brew shim
```

If `ibrew: command not found` after installing, the PATH block is in the profile but
this shell has not read it yet — `source` it, or open a new terminal:

```bash
grep -n intelbrew ~/.zshrc ~/.zprofile   # find where the block went
ibrew setup --apply                      # (re)write it; prints the exact file
ibrew setup --uninstall-profile          # remove it again
```

Requirements: macOS 11+ on Intel (Apple Silicon works too, but needs it less),
`python3` 3.9+ (system Python is fine), and Homebrew only if you want the `brew` shim.

## 4. How it decides where to get a binary

For every requested formula, in order, first hit wins:

1. **Curated recipe** — `registry/<name>.json`, validated on this machine by `ibrew doctor`.
2. **Homebrew bottle for this platform**, from the closest tag `≤ machine macOS`
   (whitelisted tags: `golden_gate`, `big_sur`, `monterey`, `ventura`, `sonoma`, `sequoia`, `tahoe`, `*_linux`).
   Anything newer than the machine is rejected, never guessed.
3. **Upstream shortcut** — the tool's own official build (`node-dist`, `github-release`,
   `adoptium-jdk`, `pypi`, `conda-forge`, `npm`).
4. **Refuse** — and print the playbook page that explains how to add a source.

Every plan carries two orthogonal facts that are never mixed up:

* **contract** — `same-contract` (same interface as the formula) or `npm`/`shortcut`
  (same purpose, different interface; e.g. `uv` installs Python but is not `python`).
* **confidence** — `verified` (recipe or documented upstream mapping), `probable`
  (bottle built for an older macOS), `heuristic` (guessed repo — needs review).

Verification is reported honestly, in three levels: `unverified` (refused by default),
`digest-only`, `verified` (digest **and** GPG/Apple signature).

## 5. Boundaries — what this does **not** do

* **Not a Homebrew fork or a package universe.** It routes to *official upstream builds*
  and to Homebrew bottles; it does not build a private bottle server.
* **Does not resurrect Linux-only or discontinued software.** If upstream publishes no
  Intel macOS binary, intelbrew refuses and says so; it never compiles silently.
* **Does not patch other tools.** `brew install python@3.12` is routed to recipe `python@3.12`
  only when a recipe exists; `pip install numpy` still builds or downloads *its* wheels —
  that is pip's job, not this tool's.
* **Does not support `.pkg`/`.dmg` installers** (they need an interactive installer and
  system-wide writes), macOS GUI apps (use `brew --cask`), or unsigned/unverifiable
  downloads unless you pass `IBREW_ALLOW_UNVERIFIED=1`.
* **Does not guarantee every bottle works.** A bottle built on Sonoma may use symbols
  missing on Ventura; `ibrew install` checks the Mach-O deployment target and warns.
* **Does not fix a broken network.** It has mirror lists, GitHub proxies and parallel
  racing, but if you have no route to any mirror, nothing here helps (see `ibrew doctor`).

Details and edge cases: [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## 6. Coverage matrix

See [docs/SOURCES.md](docs/SOURCES.md) for the full matrix (source × contract ×
verification × tested/untested). Quick version:

| Case | Supported | How |
|---|---|---|
| Node/npm/npx on Intel macOS 12–15 | ✅ tested | official `nodejs.org` / TUNA / npmmirror builds, mirror racing |
| Single-binary CLI tools (`ripgrep`, `fd`, `gh`, `uv`, …) | ✅ tested | official GitHub release asset + `sha256` from GitHub's asset digest |
| Tools that ship a bare binary, no archive (`jq`) | ✅ tested | `"fmt": "raw"` recipe: the asset *is* the executable |
| Java (`openjdk`) | ✅ | Eclipse Temurin x64 builds |
| Tools built on Homebrew-only libs (`ffmpeg`, `imagemagick`) | ⚠️ partial | conda-forge `osx-64` packages (separate runtime, not brew-linked) |
| Python (`python@3.x`) | ⚠️ partial | upstream standalone builds; not a brew formula replacement |
| macOS GUI apps | ❌ | use `brew install --cask` |
| Anything with no Intel binary upstream | ❌ by design | refuses, prints the playbook |

## 7. Repo layout

```
src/intelbrew/        the tool (stdlib only, no dependencies)
  sources.py          where binaries come from (one function per source)
  planner.py          formula → ordered steps, contract + confidence
  installer.py        download → verify → extract → install → link
  brewshim.py         the `brew` rerouter (always calls the real brew)
  nodemgr.py          official Node runtime + node/npm/npx shims
  archive.py          safe extraction (zip-slip, symlink escape, hardlink checks)
  verify.py           SHA256 / GPG / codesign reporting
  registry.py         curated recipes
  doctor.py           "does this actually work here?"
registry/             curated recipes (JSON, machine-validated)
docs/                 methodology, sources, limitations, recipes, FAQ
tests/                offline unit tests (no network needed)
```

## 8. Development

```bash
python3 -m pytest tests/ -q          # offline, no network
PYTHONPATH=src python3 -m intelbrew.cli doctor --no-apply
```

Contributions are most useful as **recipes**: one verified JSON file per formula, with the
evidence generated by `ibrew doctor <formula>`. See [docs/RECIPES.md](docs/RECIPES.md).

## 9. Status

| Area | State |
|---|---|
| Node runtime, shims, mirror racing | working, exercised end-to-end |
| GitHub-release / official-dist route | working, exercised end-to-end |
| Recipes, doctor, census, plan | working |
| Homebrew bottle route (ghcr, relocation) | implemented; download speed depends on your route to `ghcr.io` |
| conda-forge route | implemented, partially exercised |
| GPG verification | implemented, requires `gpg` + a keyring |

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Homebrew, the Node.js Foundation, or any
project it installs from.
