# Contributing

**English** · [简体中文](CONTRIBUTING.zh-CN.md)

Thanks for helping keep Intel Macs usable. The most valuable contribution is a **verified
recipe**; the second most valuable is a bug report that includes `ibrew doctor` output.

## Ground rules

1. **Never add a compile path.** intelbrew installs verified binaries or refuses. A PR that
   "falls back to building from source" will be rejected.
2. **Never add an unverified download.** `sha256` is mandatory for archives; `--allow-unverified`
   must stay an explicit user override, never a default.
3. **Never guess an architecture.** If the asset name does not state Intel (`x86_64`/`amd64`)
   or `universal`, do not add it.
4. **Say what the contract is.** If the binary is not the same tool/interface as the brew
   formula, label it (`shortcut`) and explain in the PR.

## Adding a recipe

```bash
git clone https://github.com/intelbrew/intelbrew && cd intelbrew
export PYTHONPATH=src
python3 -m intelbrew.cli add <name> "<official url>" --sha256 <hex> --bin <cmd> --apply
git add registry/<name>.json
```

`--apply` downloads, verifies, extracts, installs into a temporary prefix and **runs** the
binary; on success it writes the `verified` block. Commit only recipes with a fresh
`verified` block from a real Intel Mac (state the macOS version).

## Testing

```bash
python -m pytest tests/ -q                 # offline, must pass
PYTHONPATH=src python -m intelbrew.cli doctor --no-apply
PYTHONPATH=src python -m intelbrew.cli plan <name> -v
```

For anything that touches downloads, set `INTELBREW_HOME=$(mktemp -d)` so your real
`~/.intelbrew` is untouched.

## Commit style

`<area>: <what changed>` — e.g. `sources: read sha256 from GitHub asset digests`,
`docs: clarify bottle relocation limits`, `registry: add uv 0.13.4`.

## Reporting bugs

Include: macOS version, `uname -m`, Homebrew version, `ibrew doctor` output (redact
tokens), and the exact command that failed. For "it installed but crashes", include
`otool -L <binary>` and the dyld error.
