"""A transparent ``brew``: every subcommand Homebrew gets *after* the reroute.

The problem it solves: Homebrew decides "bottle or compile" from its own
``SimulateSystem`` platform, which never targets a platform Homebrew no longer
publishes for. On such a machine ``brew install node`` compiles. This shim
keeps Homebrew's contract (options, taps, casks, output) and *reroutes* only
the formula installs whose bottle Homebrew would not use:

    brew install node         → installs a bottle "one macOS version behind"
                                (what Homebrew does automatically on supported tags)
    brew install --build-from-source  → compile, untouched

It never re-implements Homebrew: it always calls the real ``brew``.
"""

from __future__ import annotations

import os
import shutil
import sys

from . import brew, installer
from .util import PlatformInfo, current_platform, err, info, ok, run

SKIP_PREFIX = "brew-"      # explicit escape hatch: shimming.brew install brew-node


# --------------------------------------------------------------------------- #
# shadow dir: brew-shim -> real brew, everything else -> our bin dir
# --------------------------------------------------------------------------- #

def shadow_dir() -> str:
    return installer._brew_bin_link_dir()


WRAPPER = """#!/bin/sh
# intelbrew shim (generated; do not edit) — forwards to the real brew except for
# install-like verbs whose formula Homebrew would compile from source.
export PYTHONPATH="{src}:${{PYTHONPATH:-}}"
exec "{python}" -m intelbrew.brewshim "$@"
"""


def install_shadow() -> str:
    """Populate ``<home>/brew-bin/brew`` with a wrapper around this package."""
    d = shadow_dir()
    os.makedirs(d, exist_ok=True)
    real = os.environ.get("IBREW_REAL_BREW") or _find_real_brew()
    if not real:
        raise RuntimeError("real brew binary not found (set IBREW_REAL_BREW=/usr/local/bin/brew)")
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    link = os.path.join(d, "brew")
    if os.path.lexists(link):
        os.unlink(link)
    with open(link, "w") as fh:
        fh.write(WRAPPER.format(src=src, python=sys.executable))
    os.chmod(link, 0o755)
    return d


def _is_ours(path: str) -> bool:
    """True when ``path`` is this shim (symlink or wrapper) rather than Homebrew."""
    rp = os.path.realpath(path)
    sh = os.path.realpath(shadow_dir())
    pkg = os.path.dirname(os.path.abspath(__file__))
    if rp == os.path.join(sh, "brew") or rp.startswith(sh + os.sep):
        return True
    if rp.startswith(pkg + os.sep) or os.path.basename(rp) == "ibrew":
        return True
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
        return b"intelbrew" in head and b"brewshim" in head
    except OSError:
        return False


def _find_real_brew() -> str | None:
    me = os.path.realpath(sys.argv[0]) if sys.argv else ""
    for p in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(p, "brew")
        if not os.path.isfile(cand):
            continue
        rp = os.path.realpath(cand)
        if "/.intelbrew/" in rp or rp == me or _is_ours(cand):
            continue
        if os.access(cand, os.X_OK):
            return cand
    for cand in ("/usr/local/bin/brew", "/opt/homebrew/bin/brew", "/home/linuxbrew/.linuxbrew/bin/brew"):
        if os.path.isfile(cand) and "/.intelbrew/" not in cand:
            return cand
    return None


def real_brew() -> str:
    rb = os.environ.get("IBREW_REAL_BREW") or _find_real_brew()
    if not rb:
        raise RuntimeError("brew not found")
    return rb


def _env() -> dict:
    env = dict(os.environ)
    # never hand our shim to Homebrew's own subprocesses (brew shellenv, bundle…)
    env["IBREW_REAL_BREW"] = real_brew()
    return env


def passthrough(argv: list[str]) -> int:
    rb = real_brew()
    sys.stdout.flush()
    os.execv(rb, [rb, *argv])


# --------------------------------------------------------------------------- #
# install rerouting
# --------------------------------------------------------------------------- #

def _classify(formula: str, pf: PlatformInfo) -> str:
    """native | rerouted | compile-only | not-formula

    "rerouted" is anything the planner can install from a binary — a curated
    recipe, an upstream release, a bottle built for an older macOS. Only when no
    binary route exists does the formula go to Homebrew (which may compile).
    """
    if formula.startswith("brew-"):
        return "not-formula"
    from . import planner

    native_tag = brew.tag_for(pf)
    info_ = brew.bottle_info(formula)
    if info_ and info_.has(native_tag):
        return "native"
    r = planner.resolve(formula, pf)
    if r.ok and r.how != "none":
        return "rerouted"
    return "compile-only"


def install(argv: list[str], pf: PlatformInfo) -> int:
    """Handle ``brew install …`` / ``brew reinstall …`` / ``brew upgrade …`` with rerouting."""
    verb = argv[0]                       # install | reinstall | upgrade
    rest = argv[1:]
    formulas: list[str] = []
    opts: list[str] = []
    cask = False
    build_from_source = False
    want_stdout = False
    for a in rest:
        if a in ("--cask", "--casks"):
            cask = True
        elif a in ("--build-from-source", "-s", "--HEAD"):
            build_from_source = True
        elif a.startswith("-"):
            opts.append(a)
            if a.startswith("--json") or a == "--print":
                want_stdout = True
        else:
            formulas.append(a)
    if cask or build_from_source or want_stdout or not formulas:
        return passthrough(argv)

    native, rerouted, plain = [], [], []
    for f in formulas:
        kind = _classify(f, pf)
        if kind == "native":
            native.append(f)
        elif kind == "not-formula" or f.startswith(SKIP_PREFIX):
            plain.append(f[5:] if f.startswith(SKIP_PREFIX) else f)
        elif kind == "rerouted":
            rerouted.append(f)
        else:
            plain.append(f)              # compile-only: let Homebrew do its thing

    rc = 0
    if native:
        info(f"brew {verb} {' '.join(native)}  (native bottle exists for this platform)")
        r = run([real_brew(), verb, *opts, *native], timeout=7200, capture=False, env=_env())
        rc = r.returncode
    if rerouted:
        from . import planner

        for f in rerouted:
            r = planner.resolve(f, pf)
            if not r.ok:
                err(f"{f}: {r.blocked}")
                rc = rc or 1
                continue
            info(f"{f}: Homebrew has no {pf.pretty_name} bottle — using {r.how} "
                 f"({r.contract}, {r.confidence})")
            for step in r.steps:
                if step.get("kind") == "bottle":
                    st = installer.install_bottle(step, f, pf)
                elif step.get("kind") == "npm-global":
                    st = installer.install_npm(step, f, pf)
                else:
                    st = installer.install_archive(step, f, pf, allow_unverified=False)
                if st.installed:
                    ok(f"{f} {step.get('version', '')} — {st.report}")
                    print(f"    {', '.join(st.bins) or '-'}  →  {st.prefix}")
                else:
                    err(f"{f}: {st.error}")
                    rc = rc or 1
    if plain:
        r = run([real_brew(), verb, *opts, *plain], timeout=7200, capture=False, env=_env())
        rc = rc or r.returncode
    return rc


# --------------------------------------------------------------------------- #
# status / self check
# --------------------------------------------------------------------------- #

def status(pf: PlatformInfo) -> dict:
    sh = shadow_dir()
    link = os.path.join(sh, "brew")
    return {
        "on_path": shutil.which("brew") == link,
        "shadow_dir": sh,
        "brew_link": os.path.realpath(link) if os.path.lexists(link) else "",
        "real_brew": _find_real_brew() or "",
        "native_tag": brew.tag_for(pf),
        "native_bottle_for_node": bool((brew.bottle_info("node") or
                                        type("x", (), {"has": lambda *_: False})())
                                       .has(brew.tag_for(pf))),
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("IBREW_SHIM_DEPTH"):
        # already inside the shim: this must be the real brew, never call us again
        err("brew shim recursion detected; falling back to the real brew "
            "(set IBREW_REAL_BREW=/usr/local/bin/brew)")
        rb = _find_real_brew()
        if rb and not _is_ours(rb):
            os.execv(rb, [rb, *argv])
        return 127
    os.environ["IBREW_SHIM_DEPTH"] = "1"
    if not argv:
        return passthrough([])
    pf = current_platform()
    verb = argv[0]
    if verb in ("install", "reinstall", "upgrade"):
        return install(argv, pf)
    return passthrough(argv)


if __name__ == "__main__":       # `python -m intelbrew.brewshim …`
    sys.exit(main())
