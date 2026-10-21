# Source playbook — when there is no binary

**English** · [简体中文](SOURCE-PLAYBOOK.zh-CN.md)

`ibrew install <name>` refusing is the start of a 5-minute job, not the end. Work down
this list until one row answers your case, then either install it manually or add a recipe.

## 0. First, check whether you are even in the bad case

```bash
ibrew census                 # % of formulas that would compile here
ibrew plan <name> -v         # what intelbrew would do today
```

If `plan` already shows a source, you do not need this page.

## 1. Does the *upstream project* publish an Intel macOS build?

Look at the project's releases page and match the asset name to **both** `darwin|macos|osx`
and an Intel token (`x86_64`, `amd64`, `x64`, `intel`).

| Asset name pattern | Verdict |
|---|---|
| `foo-1.2-x86_64-apple-darwin.tar.gz` | ✅ use it |
| `foo-1.2-darwin-amd64.zip` | ✅ use it |
| `foo-1.2-universal2.dmg` | ⚠️ universal is fine, `.dmg` is not an archive — mount it manually |
| `foo-1.2-aarch64-apple-darwin.tar.gz` | ❌ Apple Silicon only |
| `foo-1.2-x86_64-unknown-linux-gnu.tar.gz` | ❌ Linux only |
| `foo-1.2.pkg` | ❌ installer; extract with `pkgutil --expand-full` if you really need it |

Then:

```bash
ibrew add foo "<url>" --sha256 <hex from the release's .sha256 / SHA256SUMS> --bin foo --apply
```

## 2. Is it a Node.js, Python, Java or Rust project?

| Ecosystem | Use | Why it works |
|---|---|---|
| Node.js | `ibrew node install <major>` | official darwin-x64 builds, mirror-raced |
| Node CLI | `npm i -g <pkg>` on the managed runtime | pure JS + prebuilt native addons |
| Python | `uv python install 3.12`, then `uv tool install <pkg>` | standalone builds / wheels, no brew |
| Java | Temurin (`ibrew install openjdk@21`) | x64 macOS builds published by Adoptium |
| Rust CLI | `cargo install --locked <crate>` **if** a compiler exists; otherwise the release binary | compiling here is the thing we are avoiding |

## 3. Is it a media/data tool with many native dependencies?

(`ffmpeg`, `imagemagick`, `graphviz`, `pandoc`, `redis`, …)

```bash
conda create -n tools -c conda-forge ffmpeg
# or
ibrew install ffmpeg        # routes to conda-forge when available
```

Trade-off: conda-forge ships its own library set. It runs, but it is **not** linked against
Homebrew's libs — do not mix `brew`'s `ffmpeg` plugins with conda's binary.

## 4. Is the project dead or Linux-only?

Then there is no binary and no honest workaround in this tool:

* use an older release that still shipped Intel builds (pin it in a recipe),
* use a container (`docker run …`) for CLI work,
* compile it yourself in a private prefix (`./configure --prefix=~/.local && make`).

Document which one you chose; the next person with your Mac will thank you.

## 5. Adding the recipe so nobody repeats the work

```bash
IBREW_REGISTRY_DIR=~/.intelbrew/registry \
  ibrew add foo "<url>" --sha256 <hex> --bin foo --version 1.2 --apply
```

If it validates, open a PR with `registry/foo.json` (see
[docs/RECIPES.md](RECIPES.md)). If it validates only on a newer macOS, set
`requires_macos` — and say so in the PR.

## 6. Deciding whether to trust a third-party binary

You are about to run someone's code. Minimum bar for a recipe in this repo:

1. the publisher is the project itself or a well-known vendor (Astral, Eclipse, GitHub…),
2. the download URL is HTTPS and on the publisher's own domain or their GitHub releases,
3. the digest is published by the same publisher (asset digest, `*.sha256`, `SHA256SUMS`),
4. ideally a GPG/Apple signature verified by `ibrew verify`.

If any of those is missing, it can still be installed locally with
`IBREW_ALLOW_UNVERIFIED=1`, but it will be reported as `unverified` forever and should not
be added to the bundled registry.
