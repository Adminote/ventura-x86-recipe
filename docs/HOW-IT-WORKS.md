# How it works (methodology)

**English** · [简体中文](HOW-IT-WORKS.zh-CN.md)

This document is the reusable part: the reasoning that works on *any* Intel Mac and any
macOS version, not just the one it was developed on. Everything below is implemented in
`src/intelbrew/`; the numbers come from `ibrew census` / `ibrew doctor`.

---

## 1. Determine the last macOS that still gets Intel binaries

Do not hardcode "Ventura is dead". Ask, from data:

```bash
# bottle tags a formula actually publishes, right now
curl -s https://formulae.brew.sh/api/formula/node.json |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(d["bottle"]["stable"]["files"]))'
# → ['arm64_golden_gate', 'arm64_linux', 'arm64_sequoia', 'arm64_tahoe', ..., 'x86_64_linux']
```

Rules to apply to the tag list:

| Observation | Meaning |
|---|---|
| no `<mymacos>` tag in any formula | Homebrew stopped publishing for this OS ⇒ everything compiles |
| tag exists only for arm64 (`arm64_<x>`) | Apple Silicon only |
| `x86_64_linux` only | Intel macOS is not supported at all any more |
| newest Intel tag is `sonoma` while you run `ventura` | a *one-version-behind* bottle may still work (see §3) |

`brew.rb` keeps this logic in `src/intelbrew/brew.py`: `TAG_MACOS` (tag ↔ macOS major),
`INTEL_SUPPORTED_MAX_MACOS`, and `tag_for(platform)`. When Homebrew changes policy, update
that table — nothing else.

**Also check Homebrew's own version.** Homebrew maps `:ventura` → `ventura` and newer
hosts → `arm64_*`; a very new macOS may not even be known to an old Homebrew, in which
case `brew ruby -e 'puts Homebrew::SimulateSystem.current_tag'` disagrees with what you
expect. `ibrew doctor` compares the two and warns.

## 2. Classify every formula: binary, probably-binary, or compile-only

`ibrew census` produces the three buckets. The decision for a single formula is:

```
tag = tag_for(this machine)
if tag in bottle_tags(formula):            → native        (leave `brew install` alone)
elif any tag with macos_major <= mine:     → older-bottle  (reroute, warn, verify at launch)
else:                                      → compile-only  (find an upstream source, or refuse)
```

Never route to a bottle whose macOS is *newer* than the machine — it links against
symbols that do not exist. That is the difference between "probable" and "wishful".

## 3. Older bottles: what actually breaks

A bottle is a relocatable prefix plus Homebrew's bookkeeping:

```
node/23.11.0/
  .brew/node.rb                 ← formula used at build time
  INSTALL_RECEIPT.json          ← prefix, tab, dependencies
  bin/node include/ lib/ share/
```

Homebrew relocates a bottle using the OCI **config blob** (`sh.brew.bottle.digest`,
`org.opencontainers.image.version`, prefix info) that lives next to the layer on
`ghcr.io`, then verifies each file against the bottle's checksum list. Two consequences:

* `tar xzf bottle.tar.gz` alone gives a *broken* install (no receipts ⇒ `brew list` blind,
  no relocation ⇒ wrong paths inside `.pc`/`.dylib`).
* Installing a bottle built for another macOS is **not** just a tag change: Homebrew refuses
  it unless the bottle's platform matches its own `SimulateSystem` tag.

The correct approach (what `installer.install_bottle` does): pull the OCI index → find the
darwin variant for the wanted macOS → download the layer **and** config blob → verify the
layer's sha256 → write them back as a local bottle (`.incomplete` + `.manifest.json`) →
`brew install <that file>`. Homebrew then does relocation, linking, receipts and caveats
itself. If your route to `ghcr.io` is slow (it often is from CN), this path is slow but
correct; `ibrew install` prefers upstream builds precisely to avoid it.

**Launch check.** Before accepting an older bottle, read the Mach-O deployment target:

```bash
otool -l bin/node | grep -A2 LC_BUILD_VERSION   # minos 13.5 → needs macOS 13.5+
```

`installer._min_macos` does this and warns when `minos > machine macOS`.

## 4. Official upstream builds are the real answer

For most developer tools the upstream project publishes its own Intel macOS binary, which
is *closer to the source of truth* than Homebrew's build and does not need relocation.

The reusable classification:

| Category | Example | Where the binary is | Contract |
|---|---|---|---|
| Runtimes | Node.js | `nodejs.org/dist/vX/node-vX-darwin-x64.tar.xz` | same |
| Interpreters | Python | `python-build-standalone` releases | same-ish (no brew formula) |
| JDKs | openjdk | Eclipse Temurin `x64/mac` | same |
| Rust/Go CLIs | uv, ripgrep, fd, gh, jq | GitHub Releases, `*darwin*amd64|x86_64*` asset | same |
| Node CLIs | typescript, pnpm | npm registry | same CLI, different install root |
| Data/media tools | ffmpeg, imagemagick | conda-forge `osx-64` | different runtime |
| Python CLIs | poetry, pre-commit | PyPI wheel / zipapp | same CLI |

Rules that keep this honest:

* **Asset filtering is stricter than it looks.** Accept `darwin|macos|osx` + the exact arch
  token (`amd64`/`x86_64`/`x64` for Intel) and reject anything carrying the *other* arch
  token. `.pkg`, `.dmg`, `.msi` are refused (they are installers, not archives).
* **Digests come from the API, not from the file name.** GitHub exposes
  `asset.digest = "sha256:…"` since 2025; otherwise read the sibling `*.sha256` or
  `SHA256SUMS`. No digest ⇒ no install (unless explicitly overridden).
* **Requirements are documented, not assumed.** `engines.node` / `engines.macos` and
  `requires_macos` in recipes are enforced; a tool that needs Node 22 is not installed on
  Node 18 “because it usually works”.
* **npm is only used for packages known to be JS tools.** npm contains squatted
  placeholders (`uvicorn@0.0.1-security`) — mapping arbitrary brew names to npm would
  install the wrong software. `planner.NPM_TOOLS` is an allowlist.

## 5. Mirror strategy that survives a bad network

Three independent problems, three mechanisms:

1. **Metadata** (a few KB–MB): `net.request` with retries; base URLs come from
   `IBREW_*_MIRRORS` env vars, defaulting to upstream + TUNA/USTC/aliyun/npmmirror.
2. **Artifacts** (MB–GB): `net.download_race` starts the same URL on *all* mirrors at once
   with `curl`, kills the losers, verifies the winner. Falls back to `urllib` when `curl`
   is absent. Python's `urllib` was measured at ~27 KB/s where `curl` got 12 MB/s on the
   same host — use `curl`.
3. **GitHub** (API and release assets): `gh-proxy.com` / `ghfast.top` prefixing, plus
   `GITHUB_TOKEN`/`gh auth token` for API rate limits.

Every mirror serves identical bytes; verification is what lets us treat them as
interchangeable. A mirror can never change *what* is installed, only how fast.

## 6. Safety model

Five layers, each independently useful:

| Layer | Mechanism | Failure mode it prevents |
|---|---|---|
| Path safety | `archive.py` rejects `..`, absolute paths, escaping symlinks, hardlinks outside, device nodes | malicious / malformed archives writing outside the prefix |
| Integrity | sha256 (or sha1 for npm's registry) checked before extraction | corrupted or swapped download |
| Provenance | GPG signature (optional) or Apple `codesign` team check | wrong publisher |
| Compatibility | Mach-O arch + `minos` check before linking | “installs fine, crashes with dyld error” |
| Reversibility | install into `~/.intelbrew`, backups of shadowed files in `~/.intelbrew/backups` | losing an existing working tool |

Homebrew-owned installs are only created by Homebrew itself (`brew install <bottle file>`),
so `brew doctor`, `brew list` and uninstall keep working.

## 7. Transparent shims (why they are not wrappers)

* `~/.intelbrew/bin/node` is a **symlink** to the official binary; `npm`/`npx` are 3-line
  `exec` scripts. No interpreter in the hot path, no `node` bootstrapping `node`.
* `~/.intelbrew/brew-bin/brew` is a shim that classifies the subcommand: install-like
  verbs get rerouted, **everything else `exec`s the real brew**. There is no second
  package manager to learn.
* PATH order is the whole trick: `~/.intelbrew/bin:~/.intelbrew/brew-bin:$PATH`. Undo =
  remove one line from the shell profile.

## 8. Validating a recipe (the part most projects skip)

A recipe is a claim: “this URL, these bytes, this archive layout, this binary name, works
on this platform”. `ibrew doctor <formula>` tests the claim:

1. requirement check (documented minimum macOS/Node),
2. download to a throw-away `INTELBREW_HOME`,
3. digest verification,
4. extraction (path safety included),
5. installation into a temporary prefix,
6. **execution** of every declared binary (`--version`, then `-version`),
7. on success, writes `verified: {by, at, machine}` back into the recipe.

Recipes without a fresh `verified` block are treated as `heuristic` by reviewers, not as
truth. CI runs this on macOS runners for the Intel matrix.

## 9. Reproducing this on another Intel Mac

```bash
git clone https://github.com/intelbrew/intelbrew && cd intelbrew
./install.sh --prefix ~/.local
ibrew doctor                 # environment + network + recipes
ibrew census                 # what would compile here
ibrew setup --apply          # node/npm/npx + brew shim + PATH
brew install node            # rerouted, no compiler
```

If `ibrew doctor` reports `brew.platform` disagreeing with the bottle data, run
`brew update`; if `node.ceiling` says the newest official build needs a newer macOS, the
tool already picked the newest one that runs here — measured from the build's `minos`, not
from a hardcoded list.
