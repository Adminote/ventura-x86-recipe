"""Homebrew introspection: platform, bottle availability, installed formulas.

Everything is read from Homebrew *data* (JSON APIs / `brew` output), never from
Homebrew Ruby internals, so it keeps working across Homebrew 4 and 5+.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import net

if TYPE_CHECKING:        # annotations only: importing sources here would be circular
    from . import sources
from .util import PlatformInfo, run, warn

# Current homebrew-core bottle tags (Homebrew 4/5, tested 2026):
#   {arch}_{macos}, osx->golden_gate(10.16), ventura(13), sonoma(14), sequoia(15), tahoe(26),
#   plus arch64_linux / x86_64_linux.
TAG_MACOS = {10: "golden_gate", 11: "big_sur", 12: "monterey", 13: "ventura",
             14: "sonoma", 15: "sequoia", 26: "tahoe"}
TAG_TO_MACOS = {v: k for k, v in TAG_MACOS.items()}
TAG_TO_MACOS["osx"] = 10

# Last non-EOL Intel macOS for which homebrew-core publishes x86_64 bottles.
# Update this constant if Homebrew changes policy (see docs/methodology.md §1).
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
        r = net.request(f"{base}/formula.json", timeout=120, show_progress=True)
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
        cands = [t for t in self.tags if t.startswith("arm64_") == (pf.arch == "arm64")]
        cands = [t for t in cands if t in TAG_TO_MACOS or t.endswith("_linux")]
        mac = [t for t in cands if t in TAG_TO_MACOS]
        if not mac:
            return None
        mac.sort(key=lambda t: TAG_TO_MACOS[t], reverse=True)
        mine = pf.macos_version[0] if pf.macos_version else 0
        older = [t for t in mac if TAG_TO_MACOS[t] <= mine]
        return older[0] if older else None


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
    compile: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)

    @property
    def binary_pct(self) -> float:
        return 100.0 * len(self.binary) / max(self.total, 1)


def census(pf: PlatformInfo, fetch: bool = True) -> Census:
    tag = tag_for(pf)
    idx = formula_index(fetch=fetch)
    c = Census(pf=pf.to_dict(), tag=tag, total=len(idx))
    for f in idx:
        tags = set(((f.get("bottle") or {}).get("stable") or {}).get("files") or {})
        if f.get("disabled"):
            c.disabled.append(f["name"])
        elif tag in tags:
            c.binary.append(f["name"])
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


def ghcr_token(repo: str) -> str | None:
    import json as _json

    r = net.request(f"https://ghcr.io/token?scope=repository:homebrew/core/{repo}:pull",
                    timeout=30)
    if not r.ok:
        return None
    try:
        return _json.loads(r.text()).get("token")
    except Exception:
        return None


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
