"""``ibrew doctor``: validate a recipe (or the whole registry) on *this* machine.

A recipe is only trusted once it has been downloaded, verified, extracted and
its binaries have been executed here. ``--apply`` installs into a throw-away
prefix so a running machine is never touched.

Exit code: 0 = all R0 fixes OK, 10 = some R0 fixes need a human step.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass

from . import brew, net, registry, sources
from .util import PlatformInfo, run


@dataclass
class Check:
    name: str
    status: str          # ok | warn | fail | skipped
    detail: str = ""
    fix: str = ""

    def flag(self) -> str:
        return {"ok": "✓", "warn": "!", "fail": "✗", "skipped": "-"}[self.status]


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #

def check_platform(pf: PlatformInfo) -> Check:
    if pf.system == "Darwin":
        if pf.arch in ("x86_64", "arm64"):
            return Check("platform", "ok", f"{pf.pretty_name} {pf.machine} ({pf.arch})")
        return Check("platform", "fail", pf.arch)
    return Check("platform", "warn", f"{pf.pretty_name} ({pf.arch}): only brew shims make sense here")


def check_brew(pf: PlatformInfo) -> list[Check]:
    out: list[Check] = []
    if not shutil.which("brew"):
        return [Check("brew", "skipped", "brew not installed")]
    ver = brew.brew_version()
    prefix = brew.brew_prefix()
    out.append(Check("brew.version", "ok", ver))
    native = brew.tag_for(pf)
    out.append(Check("brew.prefix", "ok", prefix))
    if native:
        # Is brew's own platform tag in this Homebrew version? Ask brew's Rubo.
        probe = run(["brew", "ruby", "-e",
                     "puts Homebrew::SimulateSystem.current_tag.to_s"], timeout=60)
        tag_now = probe.out.splitlines()[-1] if probe.ok and probe.out.strip() else "?"
        if probe.ok and tag_now != native:
            out.append(Check("brew.platform", "warn",
                             f"brew resolves platform to '{tag_now}', intelbrew expects '{native}'",
                             fix="`brew update` (Homebrew may not know this macOS yet) or continue: "
                                 "intelbrew asks for the bottle directly"))
        else:
            out.append(Check("brew.platform", "ok", f"platform tag {tag_now}"))
    info_ = brew.bottle_info("node")
    if info_:
        ok_native = info_.has(native)
        out.append(Check("brew.bottle-coverage", "ok" if ok_native else "warn",
                         f"node {info_.version}: {native} bottle "
                         f"{'available' if ok_native else 'NOT available (brew would compile)'}",
                         fix="" if ok_native else "`ibrew node install` / `ibrew setup --apply`"))
    return out


def check_install_path() -> Check:
    """Is the `ibrew` we are running reachable from a plain shell?"""
    found = shutil.which("ibrew")
    this = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if found:
        return Check("install.on-path", "ok", found)
    return Check("install.on-path", "fail", f"`ibrew` not found on PATH (running {this or '?'})",
                 fix='run `ibrew setup --apply`, then `source '
                     + os.path.expanduser("~/.zshrc") + '` (or open a new terminal)')


def check_cellar_writable() -> Check:
    cellar = brew.brew_cellar()
    p = os.path.join(cellar, ".intelbrew-write-test")
    try:
        os.makedirs(cellar, exist_ok=True)
        open(p, "w").close()
        os.unlink(p)
        return Check("cellar.writable", "ok", cellar)
    except OSError as e:
        return Check("cellar.writable", "warn", f"{cellar}: {e}",
                     fix="brew bottles need a writable Cellar; other sources go to ~/.intelbrew/root")


def check_toolchain() -> list[Check]:
    out: list[Check] = []
    for tool, probe in (("python3", ["-V"]), ("git", ["--version"]), ("curl", ["--version"]),
                        ("node", ["-v"]), ("npm", ["-v"]), ("gpg", ["--version"]),
                        ("codesign", ["-v"]), ("otool", ["-help"])):
        p = shutil.which(tool)
        if not p:
            out.append(Check(f"tool.{tool}", "warn", "missing",
                             fix={"gpg": "optional: enables GPG signature checks",
                                  "node": "`ibrew node install`",
                                  "npm": "`ibrew node install`"}.get(tool, "")))
            continue
        r = run([p, *probe], timeout=20)
        lines = [ln for ln in (r.stdout or r.stderr).splitlines() if ln.strip()]
        line = lines[0][:60] if lines else ""
        good = r.returncode == 0 or tool in ("codesign", "otool")   # they print usage, exit 1
        out.append(Check(f"tool.{tool}", "ok" if good else "warn", line))
    return out


def check_network() -> list[Check]:
    out: list[Check] = []
    probes = [
        ("formulae.brew.sh", "https://formulae.brew.sh/api/formula/node.json"),
        ("ghcr.io", "https://ghcr.io/v2/"),
        ("nodejs.org", "https://nodejs.org/dist/index.json"),
        ("npmmirror", "https://npmmirror.com/mirrors/node/index.json"),
        ("pypi", "https://pypi.org/pypi/pip/json"),
        ("npm", "https://registry.npmjs.org/npm/latest"),
        ("github-api", "https://api.github.com/rate_limit"),
    ]
    for name, url in probes:
        r = net.request(url, timeout=15, retries=0)
        # 401/403 count as "reachable"
        good = r.ok or r.status in (401, 403)
        out.append(Check(f"net.{name}", "ok" if good else "warn",
                         r.error or f"HTTP {r.status}" if not good else "reachable"))
    return out


def check_proxy() -> list[Check]:
    out: list[Check] = []
    gitcfg = run(["git", "config", "--get", "http.https://github.com.proxy"], timeout=20)
    if gitcfg.ok and gitcfg.out:
        host = gitcfg.out.strip().split("//")[-1].rstrip("/")
        import socket

        h, _, port = host.partition(":")
        try:
            with socket.create_connection((h, int(port or 80)), timeout=3):
                out.append(Check("proxy.git", "ok", f"git proxy {gitcfg.out.strip()} is up"))
        except OSError:
            out.append(Check("proxy.git", "warn",
                             f"git proxy {gitcfg.out.strip()} (from ~/.gitconfig) is NOT reachable",
                             fix="start the proxy or `git config --global --unset "
                                 "http.https://github.com.proxy`"))
    return out


def check_node_ceiling(pf: PlatformInfo) -> list[Check]:
    out = []
    idx = sources.node_index()
    usable = [e for e in idx if sources.node_supported_arch(pf, e)]
    if not usable:
        out.append(Check("node.ceiling", "fail", "no official Node build for this platform"))
        return out
    usable.sort(key=lambda e: tuple(int(x) for x in re.findall(r"\d+", e["version"])[:3]),
                reverse=True)
    top = usable[0]["version"].lstrip("v")
    major = top.split(".")[0]
    newer = [e for e in idx if int(e["version"].lstrip("v").split(".")[0]) > int(major)
             and "darwin-x64" in " ".join(e.get("files") or [])]
    out.append(Check("node.ceiling", "ok",
                     f"newest official build runnable on {pf.pretty_name}: {top}"
                     + (f"; {newer[0]['version']} exists but requires a newer macOS" if newer else "")))
    return out


# --------------------------------------------------------------------------- #
# recipe validation
# --------------------------------------------------------------------------- #

def validate_recipe(name: str, pf: PlatformInfo, *, apply: bool = True) -> list[Check]:
    out: list[Check] = []
    r = registry.load(name)
    if not r:
        return [Check("recipe", "fail", f"no recipe for {name} (a recipe is not needed when "
                                        f"the formula has a native bottle)")]
    out.append(Check("recipe", "ok", f"{r.name} {r.version} ({len(r.steps)} step(s), {r.origin})"))
    for i, step in enumerate(r.steps):
        tag = f"step{i + 1}"
        if step.get("kind") == "npm-global":
            out.append(Check(tag, "warn", "npm step: validated via `npm view` only"))
            continue
        url = step.get("url", "")
        digest = step.get("sha256", "")
        if not digest:
            out.append(Check(tag, "fail", "missing sha256"))
            continue
        minmac = step.get("requires_macos")
        if minmac and pf.macos_version and pf.macos_version[0] < int(minmac):
            out.append(Check(tag, "skipped", f"needs macOS >= {minmac}"))
            continue
        if not apply:
            out.append(Check(tag, "ok", f"declared: {url[:70]} (not downloaded: --no-apply)"))
            continue
        work = tempfile.mkdtemp(prefix="ibrew-doctor-")
        old_home = os.environ.get("INTELBREW_HOME")
        os.environ["INTELBREW_HOME"] = work
        try:
            from . import installer as inst

            oc = inst.install_archive(dict(step, strip=step.get("strip", 1)), r.name, pf,
                                      allow_unverified=False)
            if not oc.installed:
                out.append(Check(tag, "fail", oc.error))
                continue
            checks = []
            for cmd in oc.bins:
                exe = os.path.join(oc.prefix, "bin", cmd)
                res = run([exe, "--version"], timeout=30)
                if not res.ok:
                    res = run([exe, "-version"], timeout=30)
                checks.append(f"{cmd}: {(res.stdout or res.stderr).strip().splitlines()[0][:40] if (res.stdout or res.stderr).strip() else 'ran'}"
                              if res.ok or res.returncode not in (126, 127)
                              else f"{cmd}: FAILED ({res.stderr.strip()[:60]})")
            bad = [c for c in checks if "FAILED" in c]
            out.append(Check(tag, "fail" if bad else "ok",
                             "; ".join(checks) + f"  [{oc.report}]"))
            if not bad:
                r = registry.mark_verified(r, pf)
                registry.save(r)
        finally:
            os.environ["INTELBREW_HOME"] = old_home if old_home is not None else ""
            if old_home is None:
                os.environ.pop("INTELBREW_HOME", None)
            shutil.rmtree(work, ignore_errors=True)
    return out
