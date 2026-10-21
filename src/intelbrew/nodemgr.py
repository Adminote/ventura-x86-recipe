"""Transparent ``node`` / ``npm`` / ``npx``: official builds, mirrored, cached.

* ``ensure_runtime()`` picks the newest release of the requested major that the
  *official* dist publishes for this architecture, preferring ``nodejs.org`` and
  falling back to the China mirrors (npmmirror / TUNA) for the same bytes.
* Every download is SHA256-checked against the release ``SHASUMS256.txt``.
* ``activate(kind)`` writes ``~/.intelbrew/bin/{node,npm,npx}`` statically, so
  the shims never need a JS runtime themselves.

Nothing here touches Homebrew. That is what makes ``node``/``npm``/``npx``
keep working after Homebrew drops Intel bottles.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil

from . import archive, net, sources, verify
from .util import PlatformInfo, current_platform, err, info, ok, run, warn


def runtime_root() -> str:
    return os.path.join(net.home(), "node")


def runtime_dir(ver: str) -> str:
    return os.path.join(runtime_root(), ver)


def _arch(pf: PlatformInfo) -> str:
    return "arm64" if pf.arch == "arm64" else "x64"


def current_exe() -> str:
    """Preferred existing node executable (our runtime > PATH), or ''."""
    for v in sorted(os.listdir(runtime_root()) if os.path.isdir(runtime_root()) else [], reverse=True):
        p = os.path.join(runtime_dir(v), "bin", "node")
        if os.path.isfile(p):
            return p
    w = shutil.which("node")
    return w or ""


def majors_installed() -> list[str]:
    d = runtime_root()
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def candidates(pf: PlatformInfo, ceiling: int | None = None,
               exact: int | None = None) -> list[dict]:
    """Official releases with a darwin-<arch> build, newest first.

    ``ceiling``  — never pick a newer major (used for "newest runnable")
    ``exact``    — only this major (used when the user asked for node@22)
    """
    idx = sources.node_index()
    out = []
    for e in idx:
        if sources.node_supported_arch(pf, e) is None:
            continue
        major = int(e["version"].lstrip("v").split(".")[0])
        if ceiling and major > ceiling:
            continue
        if exact and major != exact:
            continue
        out.append(e)
    out.sort(key=lambda e: tuple(int(x) for x in re.findall(r"\d+", e["version"])[:3]),
             reverse=True)
    return out


def _minos(path: str) -> int | None:
    """Minimum macOS major declared by a Mach-O binary (None if unknown)."""
    otool = shutil.which("otool") or "/usr/bin/otool"
    if not os.path.exists(otool):
        return None
    r = run([otool, "-l", path], timeout=60)
    m = re.search(r"minos\s+(\d+)", r.stdout)
    return int(m.group(1)) if m else None


def ensure_runtime(major: str | None, pf: PlatformInfo | None = None,
                   force: bool = False) -> str:
    """Install (if needed) and return the node binary of ``major``.

    ``major=None`` means "newest release that this macOS can actually run": the
    Mach-O deployment target is read from the downloaded build and a build that
    needs a newer macOS is discarded, then the next candidate is tried. The
    ceiling is upstream's, discovered here instead of hardcoded.
    """
    pf = pf or current_platform()
    wanted = int(major) if major else None
    available = candidates(pf)
    majors = sorted({int(e["version"].lstrip("v").split(".")[0]) for e in available},
                    reverse=True)
    majors = [m for m in majors if m >= 10][:8] or majors[:8]

    if wanted is not None and wanted not in majors:
        # Asking for a major that upstream never built must not silently install
        # a different one: say what exists and let the user decide.
        raise NodeUnavailable(
            f"Node {wanted} has no official {pf.arch} macOS build. "
            f"Available majors: {', '.join(map(str, majors)) or 'none'}",
            hint=f"ibrew node use {majors[0]}" if majors else "")

    pool = candidates(pf, exact=wanted) if wanted else available
    too_new = []
    for cand in pool:
        try:
            return _install_candidate(cand, pf, force)
        except _TooNew as e:
            too_new.append(f"{cand['version']} ({e})")
            warn(f"node {cand['version']}: {e}; trying the next older release")
            continue
    if too_new:
        raise NodeUnavailable(
            f"every Node {wanted or ''} build needs a newer macOS than {pf.pretty_name}",
            hint="run `ibrew node install` without a major to get the newest runnable one")
    raise NodeUnavailable(f"no official {pf.arch} macOS build of Node found for {pf.pretty_name}")


class _TooNew(Exception):
    """The downloaded build declares a newer minimum macOS than this machine."""


class NodeUnavailable(RuntimeError):
    """No build can be installed here, with a user-actionable hint."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def _install_candidate(chosen: dict, pf: PlatformInfo, force: bool) -> str:
    ver = chosen["version"]
    dest = runtime_dir(ver)
    node = os.path.join(dest, "bin", "node")
    if os.path.isfile(node) and not force:
        return node
    fn = f"node-{ver}-{sources.node_supported_arch(pf, chosen)}.tar.xz"
    digest = ""
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
        raise RuntimeError(f"SHASUMS256.txt for {ver} has no {fn}")
    local = os.path.join(net.cache_dir("node"), fn)
    urls = [f"{b}/{ver}/{fn}" for b in net.NODE_MIRRORS()]
    r, races = net.download_race(urls, local, digest=digest, stall_timeout=30)
    if not r.ok:
        raise RuntimeError(f"could not download {fn} from any Node mirror ({r.error})")
    for u, okk, secs in races:
        if okk:
            info(f"node {ver} verified from {u.split('/')[2]} ({secs:.1f}s)")
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    archive.extract(local, tmp)
    inner = archive.flatten_root(tmp)
    if not os.path.isfile(os.path.join(inner, "bin", "node")):
        raise RuntimeError(f"{fn}: unexpected layout (no bin/node)")
    minos = _minos(os.path.join(inner, "bin", "node"))
    mine = pf.macos_version[0] if pf.macos_version else 0
    if minos and mine and minos > mine:
        shutil.rmtree(tmp, ignore_errors=True)
        os.unlink(local)
        raise _TooNew(f"build requires macOS {minos}+ (this machine: {pf.pretty_name})")
    shutil.rmtree(dest, ignore_errors=True)
    shutil.move(inner, dest)
    shutil.rmtree(tmp, ignore_errors=True)
    archive.strip_quarantine(dest)
    team = verify.codesign_team(node)
    if team == "HX7739G8FX":
        info(f"node {ver} signature: Node.js Foundation (HX7739G8FX)")
    elif team:
        warn(f"node {ver} signed by unexpected team {team}")
    return node


# --------------------------------------------------------------------------- #
# shims
# --------------------------------------------------------------------------- #

NODE_SHIM = """#!/bin/sh
# intelbrew shim (generated; do not edit) — the managed Node runtime
NODE="{node}"
if [ ! -x "$NODE" ]; then
  # Without this guard a *missing* target makes the shell continue searching
  # PATH and silently run a different node (wrong version, wrong npm).
  echo "intelbrew: the managed Node runtime is missing:" >&2
  echo "  $NODE" >&2
  echo "  fix: ibrew node use {major_hint}   (or: ibrew node install)" >&2
  exit 127
fi
exec "$NODE" "$@"
"""

NPM_SHIM = """#!/bin/sh
# intelbrew shim (generated; do not edit) — runs npm/npx on the managed Node runtime
NODE="{node}"
CLI="{cli}"
if [ ! -x "$NODE" ]; then
  echo "intelbrew: the managed Node runtime for {name} is missing:" >&2
  echo "  $NODE" >&2
  echo "  fix: ibrew node use {major_hint}   (or: ibrew node install)" >&2
  exit 127
fi
if [ ! -f "$CLI" ]; then
  echo "intelbrew: {name} is not shipped with this Node build ($CLI)" >&2
  echo "  fix: ibrew node use {major_hint}" >&2
  exit 127
fi
exec "$NODE" "$CLI" "$@"
"""


def write_shims(kind: str = "shim", major: str | None = None,
                pf: PlatformInfo | None = None) -> list[str]:
    """Write ``node`` (+ npm/npx) into ``~/.intelbrew/bin``.

    ``node`` is a symlink to the official binary; ``npm``/``npx`` are 3-line
    shell shims that exec the bundded CLI with that binary. No python, no
    runtime dispatch: zero startup cost and nothing to break.
    """
    pf = pf or current_platform()
    node = ensure_runtime(major, pf)
    prefix = os.path.dirname(os.path.dirname(node))
    ver = os.path.basename(prefix)
    bdir = os.path.join(net.home(), "bin")
    os.makedirs(bdir, exist_ok=True)
    made = []

    link = os.path.join(bdir, "node")
    if os.path.lexists(link):
        os.unlink(link)
    with open(link, "w") as fh:
        fh.write(NODE_SHIM.format(node=node, major_hint=ver.split(".")[0].lstrip("v")))
    os.chmod(link, 0o755)
    made.append(link)

    want = ({"npm", "npx", "corepack"} if kind in ("shim", "npm", "install", "use", "update")
            else {kind})
    missing = []
    for name in sorted(want):
        cli = os.path.join(prefix, "lib", "node_modules", "npm", "bin", f"{name}-cli.js")
        if name == "corepack":
            cli = os.path.join(prefix, "lib", "node_modules", "corepack", "dist", "corepack.js")
        if not os.path.isfile(cli):
            missing.append(name)
            continue
        p = os.path.join(bdir, name)
        with open(p, "w") as fh:
            fh.write(NPM_SHIM.format(node=node, cli=cli, name=name,
                                     major_hint=ver.split(".")[0].lstrip("v")))
        os.chmod(p, 0o755)
        made.append(p)
    if missing:
        warn(f"this Node build does not ship: {', '.join(missing)} "
             f"(other runtimes still work; `ibrew node use <major>` switches)")
    ok(f"node {ver} activated in {bdir}: {', '.join(os.path.basename(m) for m in made)}")
    return made


# --------------------------------------------------------------------------- #
# runtime executors (invoked by the shims)
# --------------------------------------------------------------------------- #

CLI_NAME = {"npm": "npm-cli.js", "npx": "npx-cli.js"}
LEGACY_MAJOR = 18          # last Node.js line whose installer we still trust on old macOS


def _npm_cli_prefix(node: str) -> tuple[str, str, str]:
    prefix = os.path.dirname(os.path.dirname(node))
    pkg = os.path.join(prefix, "lib", "node_modules", "npm", "package.json")
    ver = "0.0.0"
    with contextlib.suppress(OSError, ValueError), open(pkg, encoding="utf-8") as fh:
        ver = json.load(fh).get("version", ver)
    return prefix, os.path.join(prefix, "lib", "node_modules", "npm", "bin"), ver


def run_runtime(kind: str, major: str | None, args: list[str]) -> int:
    pf = None
    from .util import current_platform

    pf = current_platform()
    try:
        node = ensure_runtime(major, pf)
    except Exception as e:
        err(f"node runtime unavailable: {e}")
        return 1
    if kind == "node":
        os.execv(node, [node, *args])
    # explicit `ibrew npm …` on hosts where the shims are not on PATH yet
    prefix, bindir, npm_ver = _npm_cli_prefix(node)
    major = int(os.path.basename(prefix).split(".")[0].lstrip("v"))
    env = dict(os.environ)
    env["PATH"] = os.path.join(prefix, "bin") + os.pathsep + env.get("PATH", "")
    # npm >= 11 refuses to run on node@18 once its own engines stop matching.
    if kind == "npm" and major <= 18 and _ver(npm_ver) >= (11, 0):
        fallback = _ver(run(["npm", "-v"], timeout=30).out)
        if fallback and fallback < (11, 0):
            env["PATH"] = os.path.dirname(shutil.which("npm") or "") + os.pathsep + env["PATH"]
    cli = os.path.join(bindir, CLI_NAME[kind])
    if not os.path.isfile(cli):
        err(f"{kind} is missing from this node build; run `ibrew setup --apply` to reinstall")
        return 1
    os.execve(node, [node, cli, *args], env)
    return 0  # unreachable


def _ver(v: str) -> tuple[int, int]:
    m = re.match(r"(\d+)\.(\d+)", (v or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def shim_status() -> dict[str, str]:
    bdir = os.path.join(net.home(), "bin")
    out = {}
    for name in ("node", "npm", "npx"):
        p = os.path.join(bdir, name)
        out[name] = ("ours" if os.path.exists(p) else
                     "shadowed by " + (shutil.which(name) or "?"))
    return out


def runtime_health() -> list[dict]:
    """Per-shim state, so a missing/renamed runtime is visible before it bites."""
    out = []
    for name in ("node", "npm", "npx"):
        p = os.path.join(net.home(), "bin", name)
        if not os.path.lexists(p):
            out.append({"shim": name, "state": "absent", "target": ""})
            continue
        if os.path.islink(p):
            target = os.readlink(p)
            state = "ok" if os.access(target, os.X_OK) else "broken"
        else:
            target = p
            with open(p, encoding="utf-8", errors="replace") as fh:
                head = fh.read()
            m = re.search(r'^NODE="([^"]+)"', head, re.M)
            target = m.group(1) if m else ""
            state = "ok" if target and os.access(target, os.X_OK) else "broken"
        out.append({"shim": name, "state": state, "target": target})
    return out
