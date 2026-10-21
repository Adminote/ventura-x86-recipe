"""The recipe registry: curated, machine-verified way to get each formula.

Each entry is a *recipe*: an ordered list of steps. A step is either

    {"kind": "archive", "url": "...", "sha256": "...", "fmt": "tar.xz",
     "bin": "bin/foo", "gpg": {...} | "codesign_team": "...",
     "requires_macos": 12}

    {"kind": "npm-global", "package": "...", "bin": ["foo"]}

    {"kind": "bottle", "tag": "ventura"}          # prebuilt brew bottle from ghcr

Recipes live in ``registry/<formula>.json`` inside the package, so any machine
can consume them without trusting the network for the *plan* (only for bytes).
``ibrew add`` writes new recipes, ``ibrew doctor`` re-validates them, and the
tests check every recipe's URLs — the registry stays honest.

Schema (docs/recipes.md):
    version           upstream version this recipe was validated against
    steps[]           ordered; each has source, url OR brew_ghcr_digest, sha256, install…
    install           {method: "bin"|"prefix"|"keg"|"npm", bin: [...], strip: int}
    requires          {"macos": ">=12", "node": ">=18"}  (documented minimums)
    verified          {"by": "ibrew doctor", "at": "YYYY-MM-DD", "machine": "macOS 15.7 x86_64"}
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .util import PlatformInfo, warn

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
# bundled recipes: inside the package, or next to it (repo checkout / installed lib dir)
_REG_DIRS = [
    os.path.join(_PKG_DIR, "registry"),
    os.path.join(os.path.dirname(_PKG_DIR), "registry"),
    os.path.join(os.path.dirname(os.path.dirname(_PKG_DIR)), "registry"),
]
_USER_DIR = os.path.join(os.environ.get("INTELBREW_HOME") or os.path.expanduser("~/.intelbrew"),
                         "registry")


@dataclass
class Recipe:
    name: str
    version: str = ""
    steps: List[dict] = field(default_factory=list)
    requires: dict = field(default_factory=dict)
    verified: dict = field(default_factory=dict)
    origin: str = "bundled"

    def to_json(self) -> dict:
        return {"name": self.name, "version": self.version, "steps": self.steps,
                "requires": self.requires, "verified": self.verified}

    def check_requirements(self, pf: PlatformInfo) -> str:
        """Return '' when the recipe can run here, else a reason string."""
        m = re.search(r">=\s*(\d+)", (self.requires or {}).get("macos", ""))
        if m and pf.macos_version and pf.macos_version[0] < int(m.group(1)):
            return f"needs macOS >= {m.group(1)} (have {pf.pretty_name})"
        return ""


def _dirs() -> List[str]:
    env = os.environ.get("IBREW_REGISTRY_DIR")
    cands = ([env] if env else []) + _REG_DIRS + [_USER_DIR]
    seen, out = set(), []
    for d in cands:
        if d and os.path.isdir(d) and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def load(name: str) -> Optional[Recipe]:
    for d in _dirs():
        p = os.path.join(d, f"{name}.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
                return Recipe(name=data.get("name", name), version=data.get("version", ""),
                              steps=data.get("steps", []), requires=data.get("requires", {}),
                              verified=data.get("verified", {}),
                              origin="user" if d == _USER_DIR else "bundled")
            except Exception as e:
                warn(f"bad recipe {p}: {e}")
    return None


def all_recipes() -> Dict[str, Recipe]:
    """Every known recipe; the last directory in search order wins (user > bundled)."""
    out: Dict[str, Recipe] = {}
    for d in _dirs():
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            r = load(fn[:-5])
            if r:
                out[r.name] = r
    return out


def save(recipe: Recipe, path_dir: Optional[str] = None) -> str:
    first_writable = next((d for d in _REG_DIRS if os.access(d, os.W_OK)), "")
    d = path_dir or (os.environ.get("IBREW_REGISTRY_DIR") if os.environ.get("IBREW_REGISTRY_DIR")
                     else first_writable or _USER_DIR)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{recipe.name}.json")
    data = recipe.to_json()
    data.setdefault("verified", {})
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return p


def mark_verified(recipe: Recipe, pf: PlatformInfo) -> Recipe:
    recipe.verified = {"by": "ibrew doctor", "at": time.strftime("%Y-%m-%d"),
                       "machine": f"{pf.pretty_name} {pf.arch}"}
    return recipe
