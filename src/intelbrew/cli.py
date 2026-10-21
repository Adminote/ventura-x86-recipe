"""Command line: ``ibrew <command>``.

  doctor [--apply] [<formula>…]     validate environment and recipes on this machine
  status [--json]                   what would happen to `brew install <x>` here
  plan <formula>…                   dry run: which source/version/verification
  install <formula>…                install via the plan (never compiles)
  add <cmd> <url> --sha256 …        turn a verified archive (or --raw binary) into a recipe
  node install|use|list|status      official prebuilt Node (also writes node/npm/npx shims)
  npm  <args>… / npx <args>…        run npm/npx on the managed Node runtime
  env                              print shell integration
  setup [--apply]                   one-shot: shims + node + recipe stubs
  verify <file> --sha256 …          integrity report for anything you downloaded
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, brew, brewshim, doctor, installer, net, nodemgr, planner, registry
from .util import PlatformInfo, current_platform, err, info, ok, warn

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _pf(args) -> PlatformInfo:
    return current_platform()


def _table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def _print_checks(checks: list[doctor.Check]) -> int:
    bad = 0
    for c in checks:
        print(f"  {c.flag()} {c.name:28} {c.detail}")
        if c.status == "fail":
            bad += 1
        if c.fix:
            print(f"    → {c.fix}")
    return bad


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_doctor(args) -> int:
    pf = _pf(args)
    info(f"intelbrew {__version__} — validating on {pf.pretty_name} {pf.machine} ({pf.arch})")
    checks: list[doctor.Check] = [doctor.check_platform(pf)]
    checks += doctor.check_brew(pf)
    checks.append(doctor.check_install_path())
    checks.append(doctor.check_cellar_writable())
    checks += doctor.check_toolchain()
    checks += doctor.check_network()
    checks += doctor.check_proxy()
    checks += doctor.check_node_ceiling(pf)
    bad = _print_checks(checks)
    if args.formula:
        for f in args.formula:
            info(f"recipe: {f}")
            rchecks = doctor.validate_recipe(f, pf, apply=not args.no_apply)
            bad += _print_checks(rchecks)
    else:
        recipes = registry.all_recipes()
        info(f"registry: {len(recipes)} recipe(s) bundled for this platform")
        rows = [[n, r.version, r.verified.get("at", "-"), (r.steps[0].get("source", "-"))]
                for n, r in sorted(recipes.items())]
        print(_table(rows))
    if bad:
        warn(f"{bad} check(s) need attention")
        return 10
    ok("all checks passed")
    return 0


def cmd_status(args) -> int:
    pf = _pf(args)
    st = brewshim.status(pf)
    recipes = registry.all_recipes()
    inst = installer.installed_state()
    data = {
        "platform": pf.to_dict(),
        "brew": {"version": brew.brew_version(), "prefix": brew.brew_prefix(),
                 "cellar": brew.brew_cellar(), "native_tag": st["native_tag"]},
        "shim": {"active": st["on_path"], "shadow": st["shadow_dir"], "real_brew": st["real_brew"]},
        "node": {"runtimes": nodemgr.majors_installed(), "shims": nodemgr.shim_status()},
        "recipes": len(recipes),
        "installed": {k: v.get("version", "") for k, v in inst.items()},
        "brew_would_compile": {
            "node": not st["native_bottle_for_node"],
        },
    }
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    print(f"intelbrew {__version__}   {pf.pretty_name} {pf.machine} ({pf.arch})")
    print(f"  brew          {brew.brew_version()} @ {brew.brew_prefix()}   platform tag: {st['native_tag']}")
    print(f"  brew shim     {'ACTIVE ' + st['shadow_dir'] if st['on_path'] else 'not on PATH'}")
    print(f"  real brew     {st['real_brew'] or '(not found)'}")
    print(f"  node runtimes {', '.join(nodemgr.majors_installed()) or '(none)'}")
    print(f"  node shims    {nodemgr.shim_status()}")
    print(f"  recipes       {len(recipes)}")
    print(f"  installed     {', '.join(sorted(inst)) or '(none)'}")
    print(f"  brew compile  node: {'NO (rerouted)' if not st['native_bottle_for_node'] else 'not needed'}")
    return 0


def cmd_plan(args) -> int:
    pf = _pf(args)
    res = planner.resolve_many(args.formula, pf)
    for r in res:
        print(r.line())
        if args.verbose and (r.url or r.detail):
            src = r.steps[0] if r.steps else {}
            if r.url:
                print(f"    url      {r.url}")
            digest = src.get("sha256", "")
            print(f"    verify   {('sha256:' + digest[:16] + '…') if digest else 'none'}")
            if r.detail:
                print(f"    note     {r.detail}")
    return 0 if all(r.ok for r in res) else 3


def cmd_install(args) -> int:
    pf = _pf(args)
    res = planner.resolve_many(args.formula, pf)
    failed = 0
    for r in res:
        if not r.ok:
            err(f"{r.formula}: {r.blocked}")
            failed += 1
            continue
        for step in r.steps:
            kind = step.get("kind")
            if args.dry_run:
                print(f"{r.formula}: would run {kind} step {step.get('url', step.get('package', ''))}")
                continue
            if kind == "archive":
                oc = installer.install_archive(step, r.formula, pf,
                                               allow_unverified=args.allow_unverified)
            elif kind == "npm-global":
                oc = installer.install_npm(step, r.formula, pf)
            elif kind == "bottle":
                oc = installer.install_bottle(step, r.formula, pf)
            else:
                err(f"{r.formula}: unknown step kind {kind}")
                failed += 1
                continue
            if oc.installed:
                ok(f"{r.formula} {step.get('version', '')} — {oc.report}")
                print(f"    installed in {oc.prefix}")
                print(f"    commands:   {', '.join(oc.bins) or '-'}")
            else:
                err(f"{r.formula}: {oc.error}")
                failed += 1
    if not args.dry_run and failed == 0:
        ok(f"done. binaries: {installer.bin_dir()}")
        if not _dir_on_path(installer.bin_dir()):
            print(f"    not on PATH yet: run `ibrew setup --apply` to add it to "
                  f"{installer.profile_path()}, or use `eval \"$(ibrew env)\"`")
    return 1 if failed else 0


def _dir_on_path(directory: str) -> bool:
    real = os.path.realpath(directory)
    return any(os.path.realpath(p) == real for p in os.environ.get("PATH", "").split(os.pathsep))


def cmd_add(args) -> int:
    """Turn an official archive — or a bare binary with --raw — into a recipe."""
    pf = _pf(args)
    digest = args.sha256 or ""
    if not digest and not args.from_file:
        err("--sha256 is required (or --from-file to compute it from a local copy)")
        return 2
    name = args.formula
    steps = []
    if args.from_file:
        digest = net.file_digest(args.from_file, "sha256")
    raw = bool(args.raw)
    step = {
        "kind": "archive",
        "source": "github-release" if "github.com" in args.url else "manual",
        "url": args.url,
        "sha256": digest,
    }
    if "github.com" in args.url:
        # same bytes through the CN proxies; the digest keeps them interchangeable
        step["fallback_urls"] = [p + args.url for p in net.GITHUB_PROXIES]
    if raw:
        step["fmt"] = "raw"
        step["bin"] = args.bin or [name]
    else:
        step["strip"] = 0 if args.no_strip else 1
        step["install"] = {"bin": args.bin or []}
    if args.requires_macos:
        step["requires_macos"] = args.requires_macos
    if args.codesign_team:
        step["codesign_team"] = args.codesign_team
    steps.append(step)
    r = registry.Recipe(name=name, version=args.version or "", steps=steps,
                        requires={"macos": f">={args.requires_macos}"} if args.requires_macos else {})
    p = registry.save(r)
    ok(f"recipe written: {p}")
    if args.apply:
        checks = doctor.validate_recipe(name, pf, apply=True)
        _print_checks(checks)
        return 0 if all(c.status in ("ok", "skipped") for c in checks) else 10
    return 0


def cmd_node(args) -> int:
    pf = _pf(args)
    if args.action == "list":
        print("\n".join(nodemgr.majors_installed()) or "(no managed runtimes)")
        return 0
    if args.action == "status":
        print(json.dumps({"runtimes": nodemgr.majors_installed(),
                          "shims": nodemgr.shim_status(),
                          "global_prefix": os.path.join(installer.root(), "npm")}, indent=2))
        return 0
    major = args.major
    if args.action == "use":
        if not major:
            err("usage: ibrew node use <major>")
            return 2
        nodemgr.write_shims("shim", major, pf)
        return 0
    nodemgr.write_shims("shim", major, pf)     # install/default/update share this
    return 0


def cmd_npm(args) -> int:
    return nodemgr.run_runtime("npm", os.environ.get("IBREW_MAJOR"), list(args.args))


def cmd_npx(args) -> int:
    return nodemgr.run_runtime("npx", os.environ.get("IBREW_MAJOR"), list(args.args))


def cmd_env(args) -> int:
    print(f'export PATH="{installer.bin_dir()}:{brewshim.shadow_dir()}:$PATH"')
    return 0


def cmd_setup(args) -> int:
    pf = _pf(args)
    info(f"intelbrew setup on {pf.pretty_name} {pf.machine} ({pf.arch})")
    # 1. Node runtime + node/npm/npx shims
    if not args.skip_node:
        try:
            nodemgr.write_shims("shim", args.node, pf)
        except Exception as e:
            err(f"node runtime: {e}")
    # 2. brew shim
    if not args.skip_brew:
        try:
            ibrew = os.path.join(installer.bin_dir(), "ibrew")
            os.makedirs(installer.bin_dir(), exist_ok=True)
            _wrap_ibrew(ibrew)
            brewshim.install_shadow()
            ok(f"brew shim ready: {os.path.join(brewshim.shadow_dir(), 'brew')}")
        except Exception as e:
            err(f"brew shim: {e}")
    # 3. profile
    if args.uninstall_profile:
        ok("removed the intelbrew PATH block" if installer.drop_profile()
           else "no intelbrew block found in your shell profile")
        return 0
    if args.apply:
        installer.ensure_profile()
        prof = installer.profile_path()
        ok(f"PATH block written to {prof}")
        if _dir_on_path(installer.bin_dir()):
            print(f"    {installer.bin_dir()} is already active in this shell")
        else:
            print(f"    activate it now:  source {prof}   (or open a new terminal)")
            print("    then verify:      ibrew doctor")
    else:
        print(f"    dry run: add --apply to write the PATH block into {installer.profile_path()}")
    # 4. census line
    try:
        c = brew.census(pf)
        print(f"    Homebrew here: {len(c.binary)}/{c.total} formulas have a native bottle "
              f"({c.binary_pct:.1f}%); the rest would compile — routes: `ibrew plan <name>`")
    except Exception:
        pass
    return 0


def _wrap_ibrew(path: str) -> None:
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "intelbrew")
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n")
        fh.write(f'export PYTHONPATH="{os.path.dirname(src)}:${{PYTHONPATH:-}}"\n')
        fh.write(f'exec "{sys.executable}" -m intelbrew.cli "$@"\n')
    os.chmod(path, 0o755)


def cmd_verify(args) -> int:
    from . import verify as vf

    if not args.sha256 and not args.gpg_sig:
        err("nothing to verify: pass --sha256 <hex> and/or --gpg-sig <file>")
        return 2
    rep = vf.VerifyReport()
    if args.sha256:
        got = net.file_digest(args.file, "sha256")
        if got != args.sha256.lower():
            err(f"sha256 mismatch: {got} != {args.sha256}")
            return 1
        rep.integrity = "sha256"
    if args.gpg_sig:
        fp = vf.gpg_verify(args.file, args.gpg_sig)
        rep.provenance = f"gpg:{fp[-16:]}" if fp else "none"
    rep.level = vf.ensure_trusted_level(rep.integrity, rep.provenance, rep.notes)
    ok(rep.summary())
    return 0


def cmd_census(args) -> int:
    pf = _pf(args)
    c = brew.census(pf)
    if args.json:
        print(json.dumps({"tag": c.tag, "total": c.total, "binary": len(c.binary),
                          "compile": len(c.compile)}, indent=2))
        return 0
    print(f"{pf.pretty_name} {pf.arch}: bottle tag '{c.tag}'")
    print(f"  native bottles  {len(c.binary):5d} / {c.total}  ({c.binary_pct:.1f}%)")
    print(f"  would compile   {len(c.compile):5d}")
    if args.list_compile:
        print("\n".join(c.compile))
    return 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ibrew", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"intelbrew {__version__}")
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("doctor", help="validate environment/recipes on this machine")
    d.add_argument("formula", nargs="*")
    d.add_argument("--no-apply", action="store_true",
                   help="only check declared data (no download, no execution)")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("status", help="what happens here for brew/node/npm")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    pl = sub.add_parser("plan", help="dry run: source, version and verification for each formula")
    pl.add_argument("formula", nargs="+")
    pl.add_argument("-v", "--verbose", action="store_true")
    pl.set_defaults(func=cmd_plan)

    i = sub.add_parser("install", help="install formulas from binary sources (never compiles)")
    i.add_argument("formula", nargs="+")
    i.add_argument("-n", "--dry-run", action="store_true")
    i.add_argument("--allow-unverified", action="store_true")
    i.set_defaults(func=cmd_install)

    a = sub.add_parser("add", help="create a recipe from a verified archive or single binary")
    a.add_argument("formula")
    a.add_argument("url")
    a.add_argument("--sha256", default="")
    a.add_argument("--from-file", default="")
    a.add_argument("--version", default="")
    a.add_argument("--bin", action="append",
                   help="path inside the archive, or the command name with --raw (repeatable)")
    a.add_argument("--raw", action="store_true",
                   help="the URL is the binary itself, not an archive (e.g. jq-macos-amd64)")
    a.add_argument("--no-strip", action="store_true", help="archive has no top-level directory")
    a.add_argument("--requires-macos", type=int, default=0)
    a.add_argument("--codesign-team", default="")
    a.add_argument("--apply", action="store_true", help="validate immediately")
    a.set_defaults(func=cmd_add)

    n = sub.add_parser("node", help="manage the official prebuilt Node runtime")
    n.add_argument("action", choices=["install", "use", "list", "status", "update"])
    n.add_argument("major", nargs="?")
    n.set_defaults(func=cmd_node)

    nm = sub.add_parser("npm", help="run npm with the managed runtime")
    nm.add_argument("args", nargs=argparse.REMAINDER)
    nm.set_defaults(func=cmd_npm)

    nx = sub.add_parser("npx", help="run npx with the managed runtime")
    nx.add_argument("args", nargs=argparse.REMAINDER)
    nx.set_defaults(func=cmd_npx)

    e = sub.add_parser("env", help="print shell exports")
    e.set_defaults(func=cmd_env)

    st = sub.add_parser("setup", help="one-shot: node + npm/npx shims, brew shim, profile")
    st.add_argument("--apply", action="store_true")
    st.add_argument("--node", default=None)
    st.add_argument("--skip-node", action="store_true")
    st.add_argument("--skip-brew", action="store_true")
    st.add_argument("--uninstall-profile", action="store_true",
                    help="remove the intelbrew PATH block from your shell profile")
    st.set_defaults(func=cmd_setup)

    v = sub.add_parser("verify", help="integrity report for a local file")
    v.add_argument("file")
    v.add_argument("--sha256", default="")
    v.add_argument("--gpg-sig", default="")
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("census", help="how many brew formulas are binaries on this machine")
    c.add_argument("--json", action="store_true")
    c.add_argument("--list-compile", action="store_true")
    c.set_defaults(func=cmd_census)

    # internal: invoked by the node/npm/npx shims
    rt = sub.add_parser("runtime", help=argparse.SUPPRESS)
    rt.add_argument("action", choices=["run"])
    rt.add_argument("kind", choices=["node", "npm", "npx"])
    rt.add_argument("major", nargs="?")
    rt.add_argument("args", nargs=argparse.REMAINDER)
    rt.set_defaults(func=_cmd_runtime)
    return p


def _cmd_runtime(args) -> int:
    rest = list(args.args)
    if rest and rest[0] == "--":
        rest = rest[1:]
    return nodemgr.run_runtime(args.kind, args.major or None, rest)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `npm`/`npx` pass their own flags through untouched (`-v`, `--version`, …):
    # argparse would reject them, so hand off before parsing.
    if argv and argv[0] in ("npm", "npx"):
        return nodemgr.run_runtime(argv[0], os.environ.get("IBREW_MAJOR"), argv[1:])
    # `ibrew brew <args>` and `ibrew <brew-verb> <args>` both drive the brew shim
    if argv and argv[0] == "brew":
        argv = argv[1:]
    elif argv and argv[0] in ("list", "info", "search", "uninstall", "remove", "upgrade",
                              "update", "outdated", "cleanup", "services", "tap", "fetch",
                              "deps", "uses", "link", "autoremove"):
        return brewshim.main(argv)
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        err("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
