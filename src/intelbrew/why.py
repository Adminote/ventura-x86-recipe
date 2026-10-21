"""``ibrew why <name>``: explain where a command/package could come from.

Read-only. It answers the question people actually ask after a failure:

    "npx foo" failed / "brew install foo" started compiling — why, and what are
    my options on *this* machine?

It looks at, in order: what is already on PATH, the Homebrew formula (and
whether a bottle exists for this platform), the recipe registry, what the planner
would do, and finally what the npm registry says about the package (pure JS,
prebuilt native, or needs to compile).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any

from . import brew, net, planner, registry
from .util import PlatformInfo, current_platform, run

# --------------------------------------------------------------------------- #
# data gathering
# --------------------------------------------------------------------------- #


def _quoted(name: str) -> str:
    return name.replace("/", "%2f")


def npm_meta(name: str) -> dict[str, Any]:
    """Latest-version document for an npm package, from any mirror."""
    for base in net.NPM_MIRRORS():
        r = net.request(f"{base}/{_quoted(name)}/latest", timeout=20)
        if r.ok:
            try:
                return r.json()
            except ValueError:
                continue
        if r.status in (404, 400):
            return {"__missing__": True}
    return {}


def _npm_native(meta: dict) -> tuple[str, str]:
    """Classify an npm package: (kind, evidence).

    kind: "js"        | pure JavaScript, npx just works
          "prebuilt"  | ships/downloads platform binaries (works offline of a compiler)
          "build"     | runs node-gyp on install; needs a toolchain and system libs
    """
    scripts = meta.get("scripts") or {}
    deps = {**(meta.get("dependencies") or {}), **(meta.get("devDependencies") or {})}
    optional = meta.get("optionalDependencies") or {}
    hooks = [str(scripts[k]) for k in ("preinstall", "install", "postinstall") if scripts.get(k)]
    install = " ".join(hooks).strip()
    if "node-gyp" in install or meta.get("gypfile"):
        return "build", f"install script: {install[:80] or 'gypfile: true'}"
    if "prebuild-install" in deps or "node-pre-gyp" in deps or "node-gyp-build" in deps:
        return "prebuilt", "uses prebuild-install / node-pre-gyp / node-gyp-build"
    plat = [k for k in optional if re.search(r"(darwin|linux|win32|arm64|x64)", k)]
    if plat:
        return "prebuilt", f"platform packages: {', '.join(sorted(plat)[:3])}"
    if install:
        return "build", f"install script: {install[:80]}"
    return "js", "no install script, no native markers"


def _npm_platform(meta: dict, pf: PlatformInfo) -> str:
    """'' when the package runs here, else a reason."""
    os_ok = meta.get("os") or []
    cpu_ok = meta.get("cpu") or []
    want_os = {"darwin": "darwin", "linux": "linux"}.get(pf.system.lower(), pf.system.lower())
    if os_ok and want_os not in os_ok and f"!{want_os}" in os_ok:
        return f"package excludes {want_os} (os: {os_ok})"
    if os_ok and want_os not in os_ok and any(not str(o).startswith("!") for o in os_ok):
        return f"package is for {os_ok}, not {want_os}"
    if cpu_ok and pf.arch not in cpu_ok and any(not str(c).startswith("!") for c in cpu_ok):
        return f"package is for cpu {cpu_ok}, not {pf.arch}"
    return ""


def _node_major() -> int:
    r = run(["node", "-v"], timeout=15)
    m = re.match(r"v(\d+)", r.out)
    return int(m.group(1)) if m else 0


def _engines_problem(meta: dict) -> str:
    engines = meta.get("engines") or {}
    need = str(engines.get("node") or "")
    m = re.search(r">=\s*v?(\d+)", need)
    have = _node_major()
    if m and have and have < int(m.group(1)):
        return f"needs node {need} (you have v{have}.x)"
    return ""


def analyze(name: str, pf: PlatformInfo | None = None) -> dict[str, Any]:
    pf = pf or current_platform()
    out: dict[str, Any] = {"query": name, "platform": pf.pretty_name + f" {pf.arch}"}

    cmd = shutil.which(name)
    managed_root = os.path.join(net.home())
    out["on_path"] = {
        "path": cmd or "",
        "managed": bool(cmd and os.path.realpath(cmd).startswith(os.path.realpath(managed_root))),
        "version": run([cmd, "--version"], timeout=15).out.splitlines()[:1][0]
        if cmd else "",
    }

    info = brew.bottle_info(name)
    out["brew"] = {
        "exists": bool(info),
        "version": info.version if info else "",
        "native_bottle": bool(info and info.has(brew.tag_for(pf))),
        "tag": brew.tag_for(pf),
        "deprecated": bool(info and info.deprecated),
        "disabled": bool(info and info.disabled),
    }

    recipe = registry.load(name)
    out["recipe"] = {
        "exists": bool(recipe),
        "version": recipe.version if recipe else "",
        "source": (recipe.steps[0].get("source", "") if recipe and recipe.steps else ""),
        "verified": (recipe.verified.get("at", "") if recipe else ""),
    }

    plan = planner.resolve(name, pf)
    out["plan"] = {
        "how": plan.how,
        "version": plan.version,
        "contract": plan.contract,
        "confidence": plan.confidence,
        "blocked": plan.blocked,
        "ok": plan.ok,
    }

    meta = npm_meta(name)
    if meta.get("__missing__"):
        out["npm"] = {"exists": False}
    elif meta:
        kind, evidence = _npm_native(meta)
        out["npm"] = {
            "exists": True,
            "version": meta.get("version", ""),
            "kind": kind,
            "evidence": evidence,
            "platform_problem": _npm_platform(meta, pf),
            "engines_problem": _engines_problem(meta),
            "deprecated": meta.get("deprecated") or "",
            "bin": sorted(meta.get("bin") or {}) if isinstance(meta.get("bin"), dict)
            else ([meta["bin"]] if meta.get("bin") else []),
            "description": (meta.get("description") or "")[:80],
        }
    else:
        out["npm"] = {"exists": None}      # registry unreachable

    out["verdict"] = verdict(out)
    return out


# --------------------------------------------------------------------------- #
# verdict text
# --------------------------------------------------------------------------- #

def verdict(d: dict[str, Any]) -> list[str]:
    """Short, ordered lines: what works here, and what will fail."""
    lines: list[str] = []
    npm = d.get("npm") or {}
    brew_ = d.get("brew") or {}
    plan = d.get("plan") or {}

    if d["on_path"]["path"]:
        lines.append(f"already runnable: {d['on_path']['path']}"
                     + (" (managed by intelbrew)" if d["on_path"]["managed"] else ""))

    if plan.get("ok") and plan.get("how") not in ("none", "brew-bottle"):
        lines.append(f"binary route: `ibrew install {d['query']}` → {plan['how']} "
                     f"{plan['version']} ({plan['contract']}, {plan['confidence']})")
    elif plan.get("ok") and plan.get("how") == "brew-bottle":
        lines.append(f"Homebrew bottle: {brew_.get('version', '?')} built for "
                     f"{plan.get('version', '?')} (no compile, but check it launches)")
    if brew_.get("native_bottle"):
        lines.append(f"native bottle exists: `brew install {d['query']}` needs no compile")
    if brew_.get("disabled"):
        lines.append("this formula is disabled in Homebrew (do not use it)")
    elif brew_.get("deprecated"):
        lines.append("this formula is deprecated in Homebrew")

    if npm.get("exists"):
        exposes = d["query"] in (npm.get("bin") or [])
        if npm.get("platform_problem"):
            lines.append(f"npm: {npm['version']} cannot run here — {npm['platform_problem']}")
        elif npm.get("engines_problem"):
            lines.append(f"npm: {npm['version']} — {npm['engines_problem']}; `npx` will warn or fail")
        elif not exposes and npm.get("bin"):
            lines.append(f"npm: the package named '{d['query']}' exposes "
                         f"{', '.join(npm['bin'][:3])} — not '{d['query']}'; probably unrelated")
        elif not exposes and not npm.get("bin"):
            # npm names are first-come-first-served: "htop" on npm is
            # 'handle-to-promise', not htop. Do not send people to it.
            desc = npm.get("description") or "no description"
            lines.append(f"npm: the package named '{d['query']}' exposes no command "
                         f"({desc}) — most likely unrelated; do not rely on "
                         f"`npx {d['query']}`")
        elif npm["kind"] == "js":
            lines.append(f"npm: {npm['version']} is pure JavaScript → `npx {d['query']}` works")
        elif npm["kind"] == "prebuilt":
            lines.append(f"npm: {npm['version']} ships platform binaries ({npm['evidence']}) "
                         f"→ `npx` should work without a compiler")
        else:
            lines.append(f"npm: {npm['version']} compiles on install ({npm['evidence']}) "
                         f"→ `npx` needs a toolchain; intelbrew cannot supply build headers")
        if npm.get("deprecated"):
            lines.append(f"npm: deprecated — {str(npm['deprecated'])[:100]}")
    elif npm.get("exists") is False:
        lines.append(f"npm: no package named '{d['query']}' (registry returned 404)")
    else:
        lines.append("npm: registry unreachable, could not check")

    if not any(x.startswith(("binary route", "native bottle", "npm:", "already runnable"))
               for x in lines):
        lines.append("nothing on Homebrew, in the recipe registry, or on npm — "
                     "see docs/SOURCE-PLAYBOOK.md")
    return lines


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def render(d: dict[str, Any], as_json: bool = False) -> str:
    if as_json:
        return json.dumps(d, indent=2, ensure_ascii=False)
    out = [f"{d['query']}  on {d['platform']}"]
    out.append("")
    out.extend(f"  • {line}" for line in d["verdict"])
    out.append("")
    w = d.get("brew") or {}
    out.append(f"  brew    formula={'yes' if w.get('exists') else 'no'}"
               f"  version={w.get('version') or '-'}"
               f"  bottle({w.get('tag')})={'yes' if w.get('native_bottle') else 'no'}")
    r = d.get("recipe") or {}
    out.append(f"  recipe  {'yes' if r.get('exists') else 'no'}"
               f"  version={r.get('version') or '-'}"
               f"  source={r.get('source') or '-'}")
    n = d.get("npm") or {}
    out.append(f"  npm     {'yes' if n.get('exists') else 'no' if n.get('exists') is False else '?'}"
               f"  version={n.get('version') or '-'}"
               f"  kind={n.get('kind') or '-'}")
    return "\n".join(out)
