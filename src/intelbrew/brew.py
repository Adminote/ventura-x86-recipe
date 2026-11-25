"""Homebrew introspection: platform, bottle availability, installed formulas.

Everything is read from Homebrew *data* (JSON APIs / `brew` output), never from
Homebrew Ruby internals, so it keeps working across Homebrew 4 and 5+.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import net

if TYPE_CHECKING:        # annotations only: importing sources here would be circular
    from . import sources
from .util import PlatformInfo, activity, run, warn

# Current homebrew-core bottle tags (Homebrew 4/5, tested 2026):
#   {arch}_{macos}, osx->golden_gate(10.16), ventura(13), sonoma(14), sequoia(15), tahoe(26),
#   plus arch64_linux / x86_64_linux.
TAG_MACOS = {10: "golden_gate", 11: "big_sur", 12: "monterey", 13: "ventura",
             14: "sonoma", 15: "sequoia", 26: "tahoe"}
TAG_TO_MACOS = {v: k for k, v in TAG_MACOS.items()}
TAG_TO_MACOS["osx"] = 10

# Last non-EOL Intel macOS for which homebrew-core publishes x86_64 bottles.
# Update this constant if Homebrew changes policy (see docs/HOW-IT-WORKS.md).
INTEL_SUPPORTED_MAX_MACOS = 15


def tag_for(pf: PlatformInfo) -> str | None:
    """Bottle tag this machine would look for, e.g. ``ventura`` (Intel) or ``arm64_sequoia``."""
    if pf.system != "Darwin" or not pf.macos_version:
        return None
    major = pf.macos_version[0]
    name = TAG_MACOS.get(major)
    if not name:
        return None
    return f"arm64_{name}" if pf.arch == "arm64" else name


def macos_of_tag(tag: str | None) -> int | None:
    """macOS major encoded in a bottle tag, arm64 prefix included (``arm64_sonoma`` → 14)."""
    if not tag:
        return None
    base = tag[6:] if tag.startswith("arm64_") else tag
    return TAG_TO_MACOS.get(base)


def arch_of_tag(tag: str) -> str:
    return "arm64" if tag.startswith("arm64_") else "x86_64"


def platform_tags(tags, pf: PlatformInfo) -> list[str]:
    """Bottle tags for this machine's CPU that macOS *could* run (any age)."""
    out = []
    for t in tags:
        if "linux" in t:
            continue
        if (arch_of_tag(t) == "arm64") != (pf.arch == "arm64"):
            continue
        if macos_of_tag(t) is None:
            continue
        out.append(t)
    return out


def best_bottle_tag(tags, pf: PlatformInfo) -> tuple[str | None, bool]:
    """Newest usable bottle tag for this machine, and whether it is the native one."""
    mine = pf.macos_version[0] if pf.macos_version else 0
    usable = [t for t in platform_tags(tags, pf) if macos_of_tag(t) <= mine]
    if not usable:
        return None, False
    best = max(usable, key=lambda t: macos_of_tag(t))
    return best, best == tag_for(pf)


def version_key(version: str) -> tuple[int, ...]:
    """Coarse ordering for Homebrew versions (``1.2.3_1`` → ``(1, 2, 3, 1)``)."""
    nums = tuple(int(x) for x in re.findall(r"\d+", version or ""))
    return nums or (0,)


def parse_oci_tag(tag: str) -> tuple[str, int | None]:
    """``1.25.0-2`` → ("1.25.0", 2); ``7.1.2-30-1`` → ("7.1.2-30", 1)."""
    m = re.match(r"^(.*?)(?:-(\d+))?$", tag or "")
    if not m:
        return tag or "", None
    return m.group(1) or "", int(m.group(2)) if m.group(2) else None


def formula_tag_set(tags) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for t in tags:
        out[t] = True
    return out


# --------------------------------------------------------------------------- #
# JSON APIs (formulae.brew.sh, with mirror fallback)
# --------------------------------------------------------------------------- #

def api_bases() -> list[str]:
    env = os.environ.get("IBREW_BREW_API_MIRRORS")
    if env:
        return [v.strip().rstrip("/") for v in env.split(",") if v.strip()]
    return [
        "https://formulae.brew.sh/api",
        "https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles/api",
        "https://mirrors.ustc.edu.cn/homebrew-bottles/api",
    ]


def formula_json(name: str, ttl_days: int = 1) -> dict | None:
    """``/api/formula/<name>.json``; also follows ``aliases``/``oldnames`` renames."""
    cache = os.path.join(net.cache_dir("brew"), re.sub(r"[^A-Za-z0-9_.@-]", "_", name) + ".json")
    if net._ttl_fresh(cache, ttl_days * 86400):
        try:
            with open(cache, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    for base in api_bases():
        with activity(f"looking up {name} in the Homebrew API ({base.split('/')[2]})"):
            r = net.request(f"{base}/formula/{name}.json", timeout=30)
        if not r.ok:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        if isinstance(data, dict) and "versions" in data:
            try:
                with open(cache, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
            except Exception:
                pass
            return data
    return None


def formula_index(fetch: bool = True) -> list[dict]:
    """Full core formula list (used once, for the platform-support census).

    ``fetch=False`` only reads the cache: the index is ~32 MB and must never be
    downloaded implicitly by a command the user thought was local (e.g. `setup`).
    """
    cache = os.path.join(net.cache_dir("brew"), "formula-index.json")
    if net._ttl_fresh(cache, 3 * 86400):
        try:
            with open(cache, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    if not fetch:
        return []
    for base in api_bases():
        host = base.split("/")[2]
        with activity(f"fetching the Homebrew formula index (~30 MB) from {host}"):
            r = net.request(f"{base}/formula.json", timeout=180, show_progress=True)
        if not r.ok:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        if isinstance(data, list) and data and "versions" in data[0]:
            try:
                with open(cache, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
            except Exception:
                pass
            return data
    warn("could not fetch Homebrew formula index from any mirror")
    return []


@dataclass
class BottleInfo:
    name: str
    version: str
    tags: list[str]
    deprecated: bool = False
    disabled: bool = False

    def has(self, tag: str | None) -> bool:
        return bool(tag) and tag in self.tags

    def nearest_tag(self, pf: PlatformInfo) -> str | None:
        """Best *other* bottle tag for this machine (for 'bottle found, 1 version behind')."""
        tag, native = best_bottle_tag(self.tags, pf)
        return None if native else tag


def bottle_info(name: str) -> BottleInfo | None:
    d = formula_json(name)
    if not d:
        return None
    files = ((d.get("bottle") or {}).get("stable") or {}).get("files") or {}
    return BottleInfo(
        name=d.get("name") or name,
        version=(d.get("versions") or {}).get("stable", "?"),
        tags=sorted(files),
        deprecated=bool(d.get("deprecated")),
        disabled=bool(d.get("disabled")),
    )


# --------------------------------------------------------------------------- #
# Installed state
# --------------------------------------------------------------------------- #

def installed_formulae() -> dict[str, str]:
    r = run(["brew", "list", "--formula", "--versions"], timeout=120)
    out: dict[str, str] = {}
    if not r.ok:
        return out
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[-1]
    return out


def brew_prefix() -> str:
    r = run(["brew", "--prefix"], timeout=30)
    return r.out or "/usr/local"


def brew_cellar() -> str:
    r = run(["brew", "--cellar"], timeout=30)
    return r.out or os.path.join(brew_prefix(), "Cellar")


def brew_version() -> str:
    r = run(["brew", "--version"], timeout=30)
    m = re.search(r"Homebrew\s+([0-9.]+)", r.stdout)
    return m.group(1) if m else "?"


def keg(name: str, version: str) -> str:
    return os.path.join(brew_cellar(), name, version)


# --------------------------------------------------------------------------- #
# Census: which formulas are native-binary vs compile-on-this-machine
# --------------------------------------------------------------------------- #

@dataclass
class Census:
    pf: dict
    tag: str | None
    total: int
    binary: list[str] = field(default_factory=list)
    older: list[str] = field(default_factory=list)
    maybe: list[str] = field(default_factory=list)
    compile: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)

    @property
    def binary_pct(self) -> float:
        return 100.0 * len(self.binary) / max(self.total, 1)

    @property
    def usable_pct(self) -> float:
        return 100.0 * (len(self.binary) + len(self.older)) / max(self.total, 1)


def census(pf: PlatformInfo, fetch: bool = True) -> Census:
    """Four tiers: native / older (per the API) / maybe (only ghcr can tell) / compile."""
    tag = tag_for(pf)
    idx = formula_index(fetch=fetch)
    c = Census(pf=pf.to_dict(), tag=tag, total=len(idx))
    for f in idx:
        tags = set(((f.get("bottle") or {}).get("stable") or {}).get("files") or {})
        if f.get("disabled"):
            c.disabled.append(f["name"])
        elif tag in tags:
            c.binary.append(f["name"])
        elif best_bottle_tag(tags, pf)[0]:
            c.older.append(f["name"])
        elif any(macos_of_tag(t) is not None for t in tags):
            # The API lists only the tags Homebrew still supports; a bottle built for
            # an older macOS can outlive that list on ghcr (e.g. wget 1.25.0 keeps an
            # amd64 sonoma layer after its `sonoma` tag disappeared from the API).
            c.maybe.append(f["name"])
        else:
            c.compile.append(f["name"])
    return c


# --------------------------------------------------------------------------- #
# ghcr.io (where homebrew-core bottles actually live)
# --------------------------------------------------------------------------- #

def ghcr_repo(formula: str) -> str:
    """homebrew/core/<name>[/<major>] as used by Homebrew on ghcr.io."""
    m = re.match(r"^(.+)@(\d+)$", formula)
    return f"{m.group(1)}/{m.group(2)}" if m else formula


_GHCR_TOKENS: dict[str, tuple[str, float]] = {}


def ghcr_token(repo: str) -> str | None:
    hit = _GHCR_TOKENS.get(repo)
    if hit and time.time() - hit[1] < 240:
        return hit[0]
    r = net.request(f"https://ghcr.io/token?scope=repository:homebrew/core/{repo}:pull",
                    timeout=30)
    if not r.ok:
        return None
    try:
        tok = json.loads(r.text()).get("token")
    except Exception:
        return None
    if tok:
        _GHCR_TOKENS[repo] = (tok, time.time())
    return tok


def ghcr_tags(repo: str, ttl: int = 86400) -> list[str]:
    """All OCI tags published for ``homebrew/core/<repo>``, newest first (cached)."""
    if not re.fullmatch(r"[A-Za-z0-9_.@/-]+", repo) or ".." in repo:
        return []
    cache = os.path.join(net.cache_dir("ghcr"),
                         re.sub(r"[^A-Za-z0-9_.-]", "_", repo) + ".tags.json")
    if net._ttl_fresh(cache, ttl):
        try:
            with open(cache, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, list):
                return loaded
        except Exception:
            pass
    tok = ghcr_token(repo)
    if not tok:
        return []
    base = f"https://ghcr.io/v2/homebrew/core/{repo}/tags/list"
    tags: list[str] = []
    last = ""
    # net.request has no header access, so paginate with ?last= instead of the Link header.
    for _ in range(30):
        url = base + "?n=1000" + (f"&last={urllib.parse.quote(last)}" if last else "")
        r = net.request(url, headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        if not r.ok:
            break
        try:
            page = r.json().get("tags") or []
        except Exception:
            break
        if not page:
            break
        tags.extend(page)
        if len(page) < 1000:
            break
        last = page[-1]
    out, seen = [], set()
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    if out:
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(out, fh)
        except Exception:
            pass
    return out


def walk_back() -> int:
    raw = os.environ.get("IBREW_BOTTLE_WALK_BACK", "")
    try:
        n = int(raw) if raw else 8
    except ValueError:
        n = 8
    return max(0, min(30, n))


def bottle_candidates(name: str, pf: PlatformInfo) -> list[dict]:
    """Bottle builds usable on this machine, best first.

    Each entry: ``oci_tag`` (the ghcr tag to pull), ``version``, ``tag`` (the
    bottle platform tag it was built for) and ``rebuild``.  The first entry is
    the current version when it has any usable bottle; otherwise the current
    version is still tried first (ghcr may hold a rebuild the API index has not
    caught up with), followed by older versions that do have bottles.
    """
    info_ = bottle_info(name)
    if not info_ or info_.disabled:
        return []
    cur = info_.version
    best, native = best_bottle_tag(info_.tags, pf)
    if native:
        oci = f"{cur}"
        return [{"oci_tag": oci, "version": cur, "tag": best, "rebuild": None}]

    hint = best or ""
    n = walk_back()
    tags = ghcr_tags(ghcr_repo(name)) if n > 0 else []
    by_ver: dict[str, tuple[int, str, int | None]] = {}
    for t in tags:
        ver, rebuild = parse_oci_tag(t)
        if not ver:
            continue
        rank = rebuild if rebuild is not None else -1
        prev = by_ver.get(ver)
        if prev is None or rank > prev[0]:
            by_ver[ver] = (rank, t, rebuild)

    # A bottle rebuild (``1.25.0-2``) is the same version with a fixed build script,
    # so the current version's best rebuild must be tried before walking back.
    cur_oci, cur_rebuild = cur, None
    if cur in by_ver:
        _, cur_oci, cur_rebuild = by_ver[cur]
    out = [{"oci_tag": cur_oci, "version": cur, "tag": hint, "rebuild": cur_rebuild}]
    if not by_ver:
        return out

    cur_key = version_key(cur)
    older = sorted((v for v in by_ver if version_key(v) < cur_key),
                   key=version_key, reverse=True)[:n]
    for v in older:
        _, oci, rebuild = by_ver[v]
        out.append({"oci_tag": oci, "version": v, "tag": hint, "rebuild": rebuild})
    return out


def tap_for(formula: str) -> str | None:
    if "/" in formula:
        return formula.rsplit("/", 1)[0]
    return None


def brew_bottle(formula: str, pf: PlatformInfo) -> sources.Candidate | None:
    """Thin re-export so callers can stay in the ``brew`` namespace."""
    from . import sources

    return sources.brew_bottle(formula, pf)


def native_bottle_exists(formula: str, pf: PlatformInfo) -> bool:
    info_ = bottle_info(formula)
    return bool(info_ and info_.has(tag_for(pf)))
