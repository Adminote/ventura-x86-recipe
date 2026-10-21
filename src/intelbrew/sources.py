"""Binary *sources*: where an official upstream build of a formula can come from.

A source answers one question: "give me a *binary* for <formula> that runs on this
exact machine" — an archive to unpack, or a single executable release asset —
together with the integrity data needed to prove it.

Order for a brew request (planner.py):
    1. registry (pre-registered steps written by `ibrew add`)
    2. brew bottle for this platform, from the closest tag <= machine macOS
    3. node-dist  (Node.js official darwin-x64 build)
    4. github-release (upstream official release asset, per-platform)
    5. adoptium (JDK), maven (Java tools), pypi (Python tools), conda (data tools)
    6. npm-global (npm packages with a documented official Node requirement)

Adding a source = one function returning :class:`Candidate` or None. Keep it a
*pure planner*: no downloads, no installs (those live in installer.py).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field

from . import brew, net
from .util import PlatformInfo, warn


@dataclass
class Candidate:
    source: str                 # node-dist | github-release | brew-bottle | ...
    url: str                    # primary URL (may be rewritten by installer)
    digest: str                 # sha256 hex ("" = cannot prove integrity)
    algo: str = "sha256"
    provenance: str = "none"    # gpg:<keyid> | codesign:<team> | none
    version: str = ""
    fmt: str = ""               # expected archive kind (detected from URL otherwise)
    notes: str = ""
    fallbacks: list[str] = field(default_factory=list)   # other mirrors of same bytes
    meta: dict = field(default_factory=dict)             # source-specific (filename, asset…)

    def __str__(self) -> str:
        return f"{self.source}: {os.path.basename(self.url)} ({self.version or '?'})"


# Map a formula token to the upstream tool it is really a build of.
# "shortcut mode": for developer tools we prefer the upstream official build.
SHORTCUT_MAP = {
    "node": "node", "node@24": "node", "node@22": "node", "node@20": "node", "node@18": "node",
    "python@3.13": "python", "python@3.12": "python", "python@3.11": "python", "python@3.10": "python",
    "python@3.9": "python", "python": "python",
    "go": "go", "rust": "rust", "rustup": "rustup",
    "python-uv": "uv", "uv": "uv", "uvx": "uv",
    "pyenv": "pyenv", "pyenv-virtualenv": "pyenv",
    "poetry": "poetry", "pipx": "pipx", "black": "black", "pre-commit": "pre-commit",
    "pytest": "pytest", "mypy": "mypy", "youtube-dl": "youtube-dl", "yt-dlp": "yt-dlp",
    "openjdk": "jdk", "coursier": "coursier", "sbt": "sbt",
    "dagger": "dagger", "zk": "zk",
    "stripe": "stripe", "stern": "stern",
    "redis": "redis",
    "gradle": "gradle",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _macos_major(pf: PlatformInfo) -> int:
    return pf.macos_version[0] if pf.macos_version else 0


def _go_version(v: str) -> tuple[int, ...]:
    v = v.strip().lstrip("go")
    out = []
    for part in v.split(".")[:3]:
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def sha256_file_like(d: dict) -> str:
    """Digest from a GitHub asset / PyPI file dict.

    GitHub exposes ``"digest": "sha256:<hex>"`` on release assets (2025+); some
    tools expose a bare ``sha256``. Anything else is treated as "unknown".
    """
    for k in ("digest", "sha256", "checksum"):
        v = d.get(k)
        if not isinstance(v, str):
            continue
        if v.startswith("sha256:"):
            v = v.split(":", 1)[1]
        if re.fullmatch(r"[0-9a-fA-F]{64}", v):
            return v.lower()
    return ""


# --------------------------------------------------------------------------- #
# Source: brewing a Homebrew bottle (native path, uses brew's own remote)
# --------------------------------------------------------------------------- #

def brew_bottle(name: str, pf: PlatformInfo) -> Candidate | None:
    info_ = brew.bottle_info(name)
    if not info_ or info_.disabled:
        return None
    want = brew.tag_for(pf)
    tags = [t for t in info_.tags if t.startswith("arm64_") == (pf.arch == "arm64")]
    mine = _macos_major(pf)

    def rank(t: str) -> tuple[int, int]:
        return (1 if brew.TAG_TO_MACOS.get(t, 0) <= mine else 0, brew.TAG_TO_MACOS.get(t, 0))

    tags = [t for t in tags if t in brew.TAG_TO_MACOS]
    if not tags:
        return None
    best = max(tags, key=rank)
    if best != want and rank(best)[0] == 0:
        return None  # every bottle needs a newer macOS than this machine
    status = ("native" if best == want
              else f"bottle for older macOS {best} (newest tag is {want}) — may not launch on "
                   f"{pf.pretty_name}")
    if best != want:
        warn(f"{name}: no {want} bottle; using {info_.version} bottle built on {best}. "
             f"Brew would compile from source instead.")
    return Candidate(
        source="brew-bottle",
        url=f"brew://{name}/{info_.version}/{best}",
        digest="", algo="sha256", provenance="none",
        version=info_.version, fmt="tar.gz", notes=status,
        meta={"tag": best},
    )


# --------------------------------------------------------------------------- #
# Source: Node.js official dist (darwin-x64 / darwin-arm64)
# --------------------------------------------------------------------------- #

_NODE_INDEX_CACHE: list[dict] | None = None


def node_index() -> list[dict]:
    global _NODE_INDEX_CACHE
    if _NODE_INDEX_CACHE is not None:
        return _NODE_INDEX_CACHE
    cache = os.path.join(net.cache_dir("node"), "index.json")
    if net._ttl_fresh(cache, 6 * 3600):
        try:
            with open(cache, encoding="utf-8") as fh:
                _NODE_INDEX_CACHE = json.load(fh)
                return _NODE_INDEX_CACHE
        except Exception:
            pass
    for base in net.NODE_MIRRORS():
        r = net.request(f"{base}/index.json", timeout=30)
        if r.ok:
            try:
                _NODE_INDEX_CACHE = r.json()
                with open(cache, "w", encoding="utf-8") as fh:
                    json.dump(_NODE_INDEX_CACHE, fh)
                return _NODE_INDEX_CACHE
            except Exception:
                continue
    _NODE_INDEX_CACHE = []
    return _NODE_INDEX_CACHE


def node_supported_arch(pf: PlatformInfo, entry: dict) -> str | None:
    files = entry.get("files") or []
    if pf.arch == "arm64":
        return "darwin-arm64" if "osx-arm64-tar" in files else None
    return "darwin-x64" if "osx-x64-tar" in files else None


def node_dist(series: str, pf: PlatformInfo, channel: str = "lts") -> Candidate | None:
    if pf.system != "Darwin":
        return None
    idx = node_index()
    if not idx:
        return None
    series = str(series)
    cands = []
    for e in idx:
        v = e.get("version", "").lstrip("v")
        if v.split(".")[0] != series and v != series:
            continue
        if node_supported_arch(pf, e) is None:
            continue
        cands.append(e)
    if not cands:
        return None
    # newest first
    cands.sort(key=lambda e: tuple(int(x) for x in re.findall(r"\d+", e["version"])[:3]), reverse=True)
    # Prefer an LTS patch line (security fixes) when the requested channel is lts.
    if channel == "lts":
        lts = [e for e in cands if e.get("lts")]
        chosen = (lts or cands)[0]
    else:
        chosen = cands[0]
    ver = chosen["version"]
    arch = node_supported_arch(pf, chosen)
    fn = f"node-{ver}-{arch}.tar.xz"
    digest = ""
    # SHASUMS256 index from any mirror
    for base in net.NODE_MIRRORS():
        r = net.request(f"{base}/{ver}/SHASUMS256.txt", timeout=30)
        if not r.ok:
            continue
        for line in r.text().splitlines():
            m = re.match(r"([0-9a-f]{64})\s+\*?(\S+)", line)
            if m and m.group(2) == fn:
                digest = m.group(1)
                break
        if digest:
            break
    if not digest:
        return None
    urls = [f"{b}/{ver}/{fn}" for b in net.NODE_MIRRORS()]
    return Candidate(
        source="node-dist", url=urls[0], digest=digest, provenance="none",
        version=ver, fmt="tar.xz", fallbacks=urls[1:],
        notes="official Node.js build (Nodejs.org); requires macOS 12+ for released LTS lines",
        meta={"filename": fn},
    )


def node_requirements_ok(requirements: dict, pf: PlatformInfo) -> tuple[bool, str]:
    """Check a tool's documented minimum macOS/Node version."""
    for engine in ("macos", "node", "os"):
        need = (requirements or {}).get(engine)
        if not need:
            continue
        m = re.search(r">=\s*v?(\d+)", need)
        if not m:
            continue
        major = int(m.group(1))
        if engine == "node":
            have = _installed_node_major()
            if have and have < major:
                return False, f"needs node {need} (have v{have}.x)"
        elif engine in ("macos", "os"):
            if _macos_major(pf) < major:
                return False, f"needs macOS {need} (have {pf.pretty_name})"
    return True, ""


def _installed_node_major() -> int:
    from .util import run

    r = run(["node", "-v"], timeout=20)
    m = re.match(r"v(\d+)", r.out)
    return int(m.group(1)) if m else 0


# --------------------------------------------------------------------------- #
# Source: GitHub Releases (official upstream asset, platform-filtered)
# --------------------------------------------------------------------------- #

GITHUB_REPOS = {
    "go": "golang/go",
    "uv": "astral-sh/uv",
    "pyenv": "pyenv/pyenv",
    "ruff": "astral-sh/ruff",
    "yq": "mikefarah/yq",
    "jq": "jqlang/jq",
    "ripgrep": "BurntSushi/ripgrep",
    "fd": "sharkdp/fd",
    "bat": "sharkdp/bat",
    "fzf": "junegunn/fzf",
    "dive": "wagoodman/dive",
    "zig": "zigtools/zpm",
    "lazygit": "jesseduffield/lazygit",
    "gh": "cli/cli",
    "kubectl": "kubernetes/kubernetes",
    "stern": "stern/stern",
    "zk": "mickael-menu/zk",
    "stripe": "stripe/stripe-cli",
    "dagger": "dagger/dagger",
    "coursier": "coursier/coursier",
    "sbt": "sbt/sbt",
    "poetry": "python-poetry/poetry",
    "pipx": "pypa/pipx",
    "python": "astral-sh/python-build-standalone",
}

# Glob order matters: first match wins. ``{arch}``/``{archU}`` are substituted.
ASSET_GLOBS = {
    "darwin": {
        "x86_64": ["*darwin*amd64*", "*darwin*x86_64*", "*darwin*x64*", "*macos*amd64*",
                   "*macos*x86_64*", "*macos*x64*", "*darwin*intel*", "*macos*intel*",
                   "*Darwin*x86_64*"],
        "arm64": ["*darwin*arm64*", "*macos*arm64*", "*darwin*aarch64*", "*macos*aarch64*"],
    },
}
TRUSTED_CASKS_TEAM = {"cli/cli": "W7QN8QE9Q9"}  # gh: GitHub CLI (not needed for formulas)


def asset_arch_tokens(pf: PlatformInfo) -> list[str]:
    if pf.arch == "arm64":
        return ["arm64", "aarch64"]
    return ["amd64", "x86_64", "x64", "intel", "386", "i386"]


ARCHIVE_EXTS = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar.zst",
                ".zip", ".tar")
# Files that are never the payload, even without an extension
NOT_PAYLOAD = ("sha256", "sha512", "checksums", "attestation", "sbom", "license", "readme",
               "changelog", "source", "src")


def filter_assets(assets: list[dict], pf: PlatformInfo) -> list[dict]:
    """Keep assets that are macOS + this architecture.

    Both archives and *raw* (extensionless) single binaries are accepted: many
    upstream projects publish ``tool-macos-amd64`` with no archive at all (jq,
    some Go tools). Installers/extended attributes (.pkg/.dmg/.msi/.exe) and
    metadata files (.sha256/SHA256SUMS/attestation) are never payloads.
    """
    words_os = ["darwin", "macos", "osx", "apple"]
    arch_ok = asset_arch_tokens(pf)
    out = []
    for a in assets:
        raw = a.get("name") or ""
        n = raw.lower()
        if n.endswith((".sig", ".sha256", ".sha512", ".sha256sum", ".asc", ".sbom",
                       ".intoto.jsonl", ".pem", ".pkg", ".dmg", ".msi", ".exe", ".deb",
                       ".rpm", ".apk", ".msi")):
            continue
        if any(w in n for w in NOT_PAYLOAD) and not n.endswith(ARCHIVE_EXTS):
            continue
        if not n.endswith(ARCHIVE_EXTS) and "." in os.path.basename(n):
            # unknown dotted suffix (e.g. .txt, .json, .7z) — not a supported payload
            continue
        if not any(w in n for w in words_os):
            continue
        other = ["arm64", "aarch64"] if pf.arch == "x86_64" else ["amd64", "x86_64", "x64"]
        toks = re.split(r"[^a-z0-9]+", n)
        if not any(t in toks or f"_{t}" in n or f"-{t}" in n for t in arch_ok):
            continue
        if any(o in toks for o in other):
            continue
        out.append(a)
    return out


def is_archive(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_EXTS)


def _prefer(names: list[str], patterns: list[str]) -> int | None:
    for pat in patterns:
        for i, n in enumerate(names):
            if fnmatch.fnmatch(n.lower(), pat.lower()):
                return i
    return 0 if names else None


def github_release(tool: str, pf: PlatformInfo, version: str = "", channel: str = "lts") -> Candidate | None:
    repo = GITHUB_REPOS.get(tool)
    if not repo:
        return None
    return github_release_repo(repo, pf, version)


def github_release_repo(repo: str, pf: PlatformInfo, version: str = "") -> Candidate | None:
    """Latest non-prerelease release of ``repo`` with a macOS asset for this arch."""
    try:
        if version:
            rel = net.github_json(f"repos/{repo}/releases/tags/{version}")
        else:
            rel = net.github_json(f"repos/{repo}/releases/latest")
    except net.RateLimited:
        raise
    if not rel:
        rels = net.github_json(f"repos/{repo}/releases?per_page=20") or []
        rel = next((r for r in rels if not r.get("prerelease") and not r.get("draft")), None)
    if not rel:
        return None
    assets = filter_assets(rel.get("assets") or [], pf)
    if not assets:
        return None
    names = [a["name"] for a in assets]
    # prefer real archives; fall back to a raw single binary
    archived = [n for n in names if is_archive(n)]
    idx = _prefer(archived, ASSET_GLOBS["darwin"].get(pf.arch, [])) if archived else None
    idx = names.index(archived[idx]) if idx is not None else 0
    asset = assets[idx]
    digest = sha256_file_like(asset) or _digest_from_sha_asset(rel["assets"], asset["name"])
    direct = asset["browser_download_url"]
    mirrors = [direct] + [p + direct for p in net.GITHUB_PROXIES]
    return Candidate(
        source="github-release",
        url=direct,
        fallbacks=mirrors[1:],
        digest=digest,
        provenance="none",
        version=_clean_version(rel.get("tag_name") or ""),
        fmt="" if is_archive(asset["name"]) else "raw",
        notes=f"official GitHub release asset {asset['name']}"
              + (" (single binary, no archive)" if not is_archive(asset["name"]) else ""),
        meta={"asset": asset["name"], "repo": repo,
              "release_url": rel.get("html_url", "")},
    )


def _clean_version(tag: str) -> str:
    """``v1.2.3`` → ``1.2.3``; ``jq-1.8.2`` → ``1.8.2``; ``tool-v2`` → ``2``."""
    t = tag.strip()
    m = re.match(r"^[A-Za-z][A-Za-z0-9_.]*-v?(\d.*)$", t)
    if m:
        return m.group(1)
    return t.lstrip("v")


def _digest_from_sha_asset(assets: list[dict], name: str) -> str:
    """Look for ``<asset>.sha256`` / SHA256SUMS in the same release."""
    for a in assets:
        an = a.get("name", "")
        if an in (f"{name}.sha256", f"{name}.sha256sum", f"{name}.sha256.txt"):
            r = net.request(a["browser_download_url"], timeout=30)
            if r.ok:
                m = re.search(r"([0-9a-fA-F]{64})", r.text())
                if m:
                    return m.group(1).lower()
    for a in assets:
        if a.get("name", "").lower() in ("sha256sums", "sha256sums.txt", "checksums.txt",
                                          "sha256sum.txt"):
            r = net.request(a["browser_download_url"], timeout=30)
            if r.ok:
                for line in r.text().splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and os.path.basename(parts[-1].lstrip("*")) == name:
                        mm = re.match(r"[0-9a-fA-F]{64}", parts[0])
                        if mm:
                            return mm.group(0).lower()
    return ""


# --------------------------------------------------------------------------- #
# Source: Adoptium Temurin JDK (x64/mac) — covers openjdk
# --------------------------------------------------------------------------- #

def adoptium_jdk(series: str, pf: PlatformInfo) -> Candidate | None:
    if pf.system != "Darwin":
        return None
    arch = "aarch64" if pf.arch == "arm64" else "x64"
    # Temple-style metadata API gives sha256 + size + official mirrors
    r = net.request(
        f"https://api.adoptium.net/v3/assets/latest/{series}/hotspot"
        f"?architecture={arch}&image_type=jdk&os=mac&vendor=eclipse", timeout=60)
    if r.ok:
        try:
            data = r.json()
            for pkg in data:
                b = pkg["binary"]["package"]
                if b.get("checksum"):
                    return Candidate(source="adoptium-jdk", url=b["link"],
                                     digest=b["checksum"], version=pkg["version"]["semver"],
                                     fmt="tar.gz", notes="Eclipse Temurin official build")
        except Exception:
            pass
    # Mirror path (TUNA keeps the same layout)
    for base in net.ADOPTIUM_MIRRORS():
        if "github.com" in base:
            continue
        r = net.request(f"{base}/{series}/jdk/{arch}/mac/", timeout=30)
        if not r.ok:
            continue
        names = re.findall(r'>([^<]+\.tar\.gz)<', r.text())
        if not names:
            continue
        name = sorted(names)[-1]
        sh = net.request(f"{base}/{series}/jdk/{arch}/mac/{name}.sha256.txt", timeout=30)
        if not sh.ok:
            continue
        m = re.match(r"([0-9a-f]{64})", sh.text())
        if not m:
            continue
        return Candidate(source="adoptium-jdk", url=f"{base}/{series}/jdk/{arch}/mac/{name}",
                         digest=m.group(1), version=name, fmt="tar.gz",
                         notes="Eclipse Temurin (mirror)")
    return None


# --------------------------------------------------------------------------- #
# Source: PyPI — standalone .app / .pyz zipapps for Python CLI tools
# --------------------------------------------------------------------------- #

def _pypi_files(pkg: str) -> list[dict]:
    meta = net.request(f"https://pypi.org/pypi/{pkg}/json", timeout=30)
    files: list[dict] = []
    if meta.ok:
        try:
            data = meta.json()
            files = data.get("urls") or []
            if not files:
                info = data["info"]
                files = info.get("packages", []) or []
        except Exception:
            files = []
    if not files:  # mirror JSON isn't standard; use the simple page
        for base in net.PYPI_MIRRORS()[1:]:
            r = net.request(f"{base}/{pkg}/", timeout=30)
            if r.ok:
                for m in re.finditer(r'href="([^"#]+)#sha256=([0-9a-f]{64})', r.text()):
                    files.append({"url": m.group(1), "digests": {"sha256": m.group(2)},
                                  "filename": m.group(1).rsplit("/", 1)[-1]})
                break
    return files


def pypi_tool(tool: str, pf: PlatformInfo) -> Candidate | None:
    # zipapps: a single file ending in .app / .pyz that self-extracts
    try:
        files = _pypi_files(tool)
    except Exception:
        return None
    zipapps = [f for f in files if f["filename"].endswith(".app")]
    whls = [f for f in files if f["filename"].endswith("-none-any.whl")]
    if zipapps:
        f = zipapps[0]
        return Candidate(source="pypi-zipapp", url=f["url"],
                         digest=sha256_file_like({"sha256": (f.get("digests") or {}).get("sha256")}),
                         version="", fmt="zip", notes="PyPI standalone zipapp (needs python3 at runtime)")
    if whls:
        f = whls[0]
        return Candidate(source="pypi-wheel", url=f["url"],
                         digest=sha256_file_like({"sha256": (f.get("digests") or {}).get("sha256")}),
                         version="", fmt="zip", notes="PyPI universal wheel (installed into a private venv)")
    return None


# --------------------------------------------------------------------------- #
# Source: conda-forge — osx-64 / noarch packages (data tools with native deps)
# --------------------------------------------------------------------------- #

def conda_pkg(pkg: str, pf: PlatformInfo) -> Candidate | None:
    """conda-forge package for this arch (osx-64 / osx-arm64 / noarch).

    Uses ``current_repodata.json`` only (a few MB) — the full repodata is
    hundreds of MB and would make ``ibrew plan`` feel broken.
    """
    subdir = "osx-arm64" if pf.arch == "arm64" else "osx-64"

    def pick(meta: dict, base: str, sub: str) -> Candidate | None:
        best = None
        for fn, m in (meta.get("packages.conda") or {}).items():
            if m.get("name") != pkg:
                continue
            ver = re.findall(r"\d+", m.get("version", "0"))
            key = tuple(int(x) for x in ver[:3]) if ver else (0,)
            if best is None or key > best[0]:
                best = (key, fn, m)
        if not best:
            return None
        _, fn, m = best
        return Candidate(source="conda-forge", url=f"{base}/{sub}/{fn}",
                         digest=m.get("sha256", ""), version=m.get("version", ""),
                         fmt="tar.zst", notes=f"conda-forge {sub} package")

    for base in net.CONDA_MIRRORS():
        for sub in (subdir, "noarch"):
            r = net.request(f"{base}/{sub}/current_repodata.json", timeout=45)
            if not r.ok:
                continue
            try:
                c = pick(r.json(), base, sub)
            except Exception:
                c = None
            if c:
                return c
    return None


# --------------------------------------------------------------------------- #
# Source: npm global install (fallback: the tool *is* a JS package)
# --------------------------------------------------------------------------- #

def npm_package(tool: str, pf: PlatformInfo, version: str = "") -> Candidate | None:
    for base in net.NPM_MIRRORS():
        r = net.request(f"{base}/{tool}/latest" if not version else f"{base}/{tool}/{version}",
                        timeout=30)
        if not r.ok:
            continue
        try:
            d = r.json()
        except Exception:
            continue
        req = d.get("engines") or {}
        ok, why = node_requirements_ok(req, pf)
        if not ok:
            raise ValueError(f"{tool}@{d.get('version')}: {why}")
        return Candidate(source="npm-global", url=f"npm://{tool}@{d['version']}",
                         digest="", version=d.get("version", ""), fmt="", provenance="none",
                         notes=f"npm package '{tool}' (pure JS when engines pass: {req or 'none'})")
    return None
