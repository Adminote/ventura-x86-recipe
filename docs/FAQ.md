# FAQ

**English** · [简体中文](FAQ.zh-CN.md)

**Is this another Homebrew fork?**
No. There is no package universe, no formula DSL, no bottle server. It routes each install
to an official binary that already exists, and the `brew` shim always calls the real
`brew` for everything it does not reroute.

**Why not just `brew install --force-bottle`?**
Because Homebrew asks its own platform tag "is there a bottle for me?", and that tag never
points at a platform Homebrew stopped publishing for. No flag changes that. Forcing a
different tag is rejected ("bottle for a different macOS version"), and copying a bottle
out of the OCI registry by hand produces a broken install without Homebrew's relocation
metadata.

**Why not just use `nvm` / `fnm` for Node?**
You should — `ibrew node` does the same thing (official builds, mirrors, shims) and is
provably SHA256/签名 verified. It additionally reroutes `brew install node` so the rest of
your brew workflow does not compile node from source. Use whichever you prefer; they
coexist.

**Will `brew upgrade` break my setup?**
`ibrew` never modifies Homebrew's database. `brew upgrade` may reinstall a formula from
source (that is Homebrew's choice); the shim reroutes install-like verbs, so
`brew install`/`reinstall`/`upgrade <name>` go through the planner. If in doubt, run
`ibrew status`.

**Does it work on Apple Silicon / newer macOS?**
Yes, but it is rarely needed: Homebrew still publishes arm64 bottles. `ibrew census` will
tell you whether anything would compile on your machine.

**How do I undo it?**
Remove the `intelbrew` block from your shell profile, then delete `~/.intelbrew`. Files
that were shadowed are in `~/.intelbrew/backups` and are restored with
`ibrew` internal restore (or `mv` them back by hand). Homebrew installs are removed with
`brew uninstall`.

**Are the binaries safe?**
They are official upstream builds, checked against the digest published by the same
project, optionally against its GPG/Apple signature. Mirrors can only change the *speed*,
never the bytes. See [LIMITATIONS](LIMITATIONS.md) §4 for the trust model's holes.

**Why does `ibrew doctor` say the newest Node cannot run here?**
Because that Intel build declares a higher minimum macOS (`otool -l … minos`). `ibrew node`
installs the newest *runnable* one — it measures the deployment target of the build it just
downloaded, so the ceiling is per machine and never hardcoded.

**My corporate proxy needs authentication.**
Set `http_proxy`/`https_proxy` (curl and urllib honour them). Mirrors that require a
different route can be overridden with `IBREW_NODE_MIRRORS`, `IBREW_PYPI_MIRRORS`,
`IBREW_NPM_MIRRORS`, `IBREW_CONDA_MIRRORS`, `IBREW_BREW_API_MIRRORS`.

**A formula I need has no recipe and no upstream binary. What then?**
`ibrew install <name>` will refuse and point at the playbook. Options: an older version,
a container, compiling manually, or adding a recipe for a vendor binary you trust
(`ibrew add`, requires `--sha256`).

**Can I pin versions?**
Yes: recipes carry `version` and `sha256`; `ibrew plan <formula>@<version>` requests a
specific upstream tag when the source supports tags (GitHub releases, Node majors).

**Does it touch `/usr/local`?**
Only `brew install` writes into Homebrew's Cellar. Everything from other sources lives in
`~/.intelbrew`. `~/.intelbrew/bin` is prepended to `PATH`, so it shadows but never
overwrites Homebrew's links.
