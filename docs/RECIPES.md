# Recipes

**English** · [简体中文](RECIPES.zh-CN.md)

A recipe is a machine-validated claim about how to install one formula from a binary. It
is the only part of intelbrew that grows over time, and the only part a contributor needs
to touch.

## Where recipes live

* `registry/` (in the repo) — bundled, reviewed, shipped with the tool.
* `~/.intelbrew/registry/` — yours; same format, wins over bundled ones.

`ibrew install <name>` checks the registry first; a recipe short-circuits all heuristics.

## Schema

```json
{
  "name": "ripgrep",
  "version": "15.2.0",
  "requires": { "macos": ">=11" },
  "verified": { "by": "ibrew doctor", "at": "2026-10-21", "machine": "macOS 13.7.8 x86_64" },
  "steps": [
    {
      "kind": "archive",
      "source": "github-release",
      "url": "https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-apple-darwin.tar.gz",
      "sha256": "af7825fcc69a2afc7a7aea55fc9af90e26421d8f20fe59df32e233c0b8a231c1",
      "fallback_urls": ["https://gh-proxy.com/https://github.com/…"],
      "strip": 1,
      "install": { "bin": ["rg"] }
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `kind` | `archive` (verified tarball/zip), `npm-global`, or `bottle` (brew ghcr) |
| `url` / `fallback_urls` | primary and interchangeable mirrors (identical bytes) |
| `sha256` | **required** for `archive`; the install is refused without it |
| `fmt` | `"raw"` when the URL *is* the binary (no archive); omit for tarballs/zips |
| `strip` | number of leading directories to drop (1 = classic release tarball) |
| `install.bin` | paths inside the archive that become commands; omit to auto-detect |
| `requires_macos` | hard floor; the step is refused below it |
| `gpg` | `{sig_url, keys_url, fingerprint}` — verified when `gpg` is installed |
| `codesign_team` | e.g. `HX7739G8FX` (Node.js Foundation); reported, not enforced |
| `verified` | written by `ibrew doctor`; reviewed, not hand-edited |

## Creating one

```bash
# 1. find the official Intel macOS build for the tool
#    (GitHub release asset, upstream dist, or vendor download page)

# 2. create + validate in one go
ibrew add ripgrep \
  "https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-apple-darwin.tar.gz" \
  --sha256 af7825fc… --bin rg --version 15.2.0 --apply
```

`--apply` runs the full doctor pipeline (download → verify → extract → install → execute)
in a throw-away `INTELBREW_HOME`. Success writes `verified` into the recipe and prints the
exact file to commit.

## Review checklist (for PRs)

1. **Intel build exists** — `x86_64`/`amd64`, not a Rosetta-only universal binary.
2. **Digest is upstream's** — from the release API, `*.sha256`, or `SHA256SUMS`; never
   computed from your own download without cross-checking.
3. **Contract is honest** — if the binary is not the same tool as the brew formula
   (different runtime, different CLI), say so in the PR and prefer not adding it.
4. **`requires_macos` is documented** — from the project's README or `minos`.
5. **`verified` block is fresh** — from `ibrew doctor` on a real Intel Mac, with the macOS
   version in `machine`.
6. **Fallbacks are byte-identical** — mirrors must serve the same `sha256`.

## Recipes we want first

`node@20`, `node@22`, `uv`, `jq`, `gh`, `yq`, `fzf`, `fd`, `bat`, `ruby` (ruby-build),
`openjdk@21`. Tools where upstream Intel support is uncertain (`ffmpeg`, `python@3.x`)
need a note explaining the trade-off, not a silent recipe.
