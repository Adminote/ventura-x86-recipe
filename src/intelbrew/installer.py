"""Step execution: download → verify → extract (or copy a raw binary) → install → verify.

Safety rules
------------
* Only the *official* `brew install` is written into ``$(brew --prefix)/Cellar``;
  everything from other sources goes into ``~/.intelbrew/root`` (prefix layout,
  like Homebrew) and is linked into ``~/.local/bin``. That keeps Homebrew's
  bookkeeping intact and makes rollback a single rm.
* Every step must prove its digest; a step without one is refused unless
  ``IBREW_ALLOW_UNVERIFIED=1`` (and it is loudly reported as unverified).
* Steps that declare ``requires_macos`` are refused on older systems.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field

from . import archive, brew, net, verify
from .util import PlatformInfo, info, run, warn


def root() -> str:
    return os.path.join(net.home(), "root")


def bin_dir() -> str:
    return os.environ.get("IBREW_BIN_DIR") or os.path.join(net.home(), "bin")


def state_dir() -> str:
    p = os.path.join(net.home(), "state")
    os.makedirs(p, exist_ok=True)
    return p


def _read_json(path: str, default):
    """Read JSON, returning ``default`` when the file is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)


@dataclass
class Outcome:
    formula: str
    step: dict
    installed: bool = False
    prefix: str = ""
    bins: list[str] = field(default_factory=list)
    report: str = ""
    error: str = ""
    backup_keys: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _looks_like_prefix(src: str, formula: str) -> bool:
    """Heuristic: a relocatable prefix has bin/ plus a real payload, not just one file."""
    b = os.path.join(src, "bin")
    if not os.path.isdir(b):
        return False
    files = [f for f in os.listdir(b) if os.path.isfile(os.path.join(b, f))]
    return len(files) > 1 or (os.path.basename(b).startswith("bin") and len(files) == 1 \
        and formula in " ".join(files))


def _macho_head_ok(head: bytes, pf: PlatformInfo) -> bool | None:
    """Arch verdict from the first bytes of a file; ``None`` when it is not Mach-O."""
    magic = head[:4]
    if magic not in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        return None  # not Mach-O (script, data, universal handled below)
    # single-arch thin x86_64 has cputype 0x01000007 at offset 4 (LE)
    if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        cpu = int.from_bytes(head[4:8], "little")
        if cpu == 0x01000007 and pf.arch != "x86_64":
            return False
        if cpu == 0x0100000C and pf.arch != "arm64":
            return False
    return True


def _macho_arch_ok(path: str, pf: PlatformInfo) -> bool:
    """Reject Mach-O binaries that cannot run on this arch (universal is fine)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return True
    return _macho_head_ok(head, pf) is not False


def _bottle_arch_ok(path: str, pf: PlatformInfo) -> tuple[bool, str]:
    """Pre-flight the bottle tarball: is any ``bin``/``sbin`` payload the wrong CPU?"""
    import tarfile

    wrong = "arm64" if pf.arch == "x86_64" else "x86_64"
    try:
        with tarfile.open(path, "r:gz") as tf:
            for m in tf:
                parts = m.name.split("/")
                if not m.isfile() or len(parts) < 3 or parts[-2] not in ("bin", "sbin"):
                    continue
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                if _macho_head_ok(fh.read(4096), pf) is False:
                    return False, f"{m.name} is built for {wrong}"
    except Exception:
        return True, ""  # a damaged tarball fails in brew with a better message
    return True, ""


def _keg_arch_note(kdir: str, formula: str, pf: PlatformInfo) -> str:
    """Post-install sanity check; never rolls anything back, only reports."""
    wrong = "arm64" if pf.arch == "x86_64" else "x86_64"
    for name, p in _formula_commands(formula, kdir):
        if not _macho_arch_ok(p, pf):
            return f"warning: {kdir}/bin/{name} looks like a {wrong} binary"
    return ""


def _min_macos(path: str) -> int | None:
    otool = shutil.which("otool") or "/usr/bin/otool"
    if not os.path.exists(otool):
        return None
    r = run([otool, "-l", path], timeout=60)
    m = re.search(r"minos\s+(\d+)", r.stdout)
    return int(m.group(1)) if m else None


def _which_all(name: str) -> list[str]:
    out = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# links + backups
# --------------------------------------------------------------------------- #

def _backup(path: str) -> bool:
    """Move an existing real file out of the way; symlinks are simply removed."""
    if not os.path.lexists(path):
        return False
    if os.path.islink(path):
        target = os.readlink(path)
        if target.startswith(root()) or "/Cellar/" in target:
            os.unlink(path)
            return False
        os.unlink(path)
        return False
    # a real file/dir on PATH (e.g. /usr/local/bin/node installed manually)
    bdir = os.path.join(net.home(), "backups")
    os.makedirs(bdir, exist_ok=True)
    dest = os.path.join(bdir, f"{os.path.basename(path)}.{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.move(path, dest)
    warn(f"existing {path} moved to {dest}")
    _record_backup(path, dest)
    return True


def _record_backup(orig: str, backup: str) -> None:
    p = os.path.join(state_dir(), "backups.json")
    data = _read_json(p, [])
    data.append({"path": orig, "backup": backup, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _write_json(p, data)


def backups() -> list[dict]:
    return _read_json(os.path.join(state_dir(), "backups.json"), [])


def restore_backup(path: str) -> bool:
    data = backups()
    mine = [b for b in data if b["path"] == path]
    if not mine:
        return False
    b = mine[-1]
    if os.path.lexists(path):
        os.unlink(path)
    shutil.move(b["backup"], path)
    rest = [x for x in data if x is not b]
    _write_json(os.path.join(state_dir(), "backups.json"), rest)
    return True


def make_links(names: dict[str, str], *, overwrite_toolchain: bool = True) -> list[str]:
    """``{command: target}`` → symlinks in :func:`bin_dir`. Returns created commands."""
    os.makedirs(bin_dir(), exist_ok=True)
    made = []
    for cmd, target in names.items():
        link = os.path.join(bin_dir(), cmd)
        _backup(link)
        os.symlink(target, link)
        made.append(cmd)
    return made


# --------------------------------------------------------------------------- #
# the installers
# --------------------------------------------------------------------------- #

def _install_archive_dir(extracted: str, formula: str, step: dict, pf: PlatformInfo,
                         stage: str) -> tuple:
    """Layout the extracted tree into ``stage``. Returns (bins {cmd: rel}, mode)."""
    strip = int(step.get("strip", 1))
    src = extracted
    for _ in range(strip):
        src = archive.flatten_root(src)
    sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(src) for f in fs
               if os.path.exists(os.path.join(dp, f)))
    single_bin = step.get("bin")
    explicit = step.get("install", {}).get("bin") if step.get("install") else None
    if explicit:
        explicit = [explicit] if isinstance(explicit, str) else list(explicit)
    if explicit:
        os.makedirs(os.path.join(stage, "bin"), exist_ok=True)
        bins = {}
        for rel in explicit:
            p = os.path.join(src, rel)
            if not os.path.exists(p):
                raise FileNotFoundError(f"{formula}: declared binary {rel!r} missing in archive")
            dest = os.path.join(stage, "bin", os.path.basename(rel))
            shutil.copy2(p, dest)
            os.chmod(dest, os.stat(dest).st_mode | 0o755)
            bins[os.path.basename(rel)] = os.path.join("bin", os.path.basename(rel))
        return bins, "bin"
    has_bin = os.path.isdir(os.path.join(src, "bin"))
    conda_pkg = os.path.isdir(os.path.join(src, "info")) and os.path.isdir(os.path.join(src, "lib"))
    has_dirs = any(os.path.isdir(os.path.join(src, d)) for d in ("lib", "share", "libexec", "include"))
    if has_bin and (conda_pkg or has_dirs or _looks_like_prefix(src, formula)):
        shutil.copytree(src, stage, symlinks=True, dirs_exist_ok=True)
        bins = {f: os.path.join("bin", f) for f in os.listdir(os.path.join(stage, "bin"))}
        return bins, "prefix"
    # single-binary archive: the payload is one executable
    if single_bin:
        names = [single_bin] if isinstance(single_bin, str) else list(single_bin)
    else:
        cands = [f for f in sorted(os.listdir(src)) if os.path.isfile(os.path.join(src, f))
                 and os.access(os.path.join(src, f), os.X_OK)
                 and not f.endswith((".txt", ".md", ".json", ".sha256", ".asc", ".sig"))]
        if not cands:
            raise ValueError(f"{formula}: no executable in the archive; pass --bin")
        # multi-binary releases (uv+uvx, bat+, …) are common: install them all,
        # the formula name identifies the primary one for shell completion checks
        names = cands
    os.makedirs(os.path.join(stage, "bin"), exist_ok=True)
    bins = {}
    for name in names:
        p = os.path.join(src, name)
        if not os.path.isfile(p):
            raise FileNotFoundError(f"{formula}: {name} not found in archive")
        dest = os.path.join(stage, "bin", os.path.basename(name))
        shutil.copy2(p, dest)
        archive.ensure_executable(dest)
        bins[os.path.basename(name)] = os.path.join("bin", os.path.basename(name))
    return bins, "bin"


def install_archive(step: dict, formula: str, pf: PlatformInfo, *, allow_unverified: bool) -> Outcome:
    out = Outcome(formula=formula, step=step)
    url = step.get("url", "")
    digest = step.get("sha256", "")
    if not digest and not allow_unverified:
        out.error = "step has no sha256; refusing (set IBREW_ALLOW_UNVERIFIED=1 to override)"
        return out
    minmac = step.get("requires_macos")
    if minmac and pf.macos_version and pf.macos_version[0] < int(minmac):
        out.error = f"this build requires macOS >= {minmac}; machine is {pf.pretty_name}"
        return out

    fn = step.get("filename") or os.path.basename(url.split("?")[0]) or f"{formula}.bin"
    dl = os.path.join(net.cache_dir("downloads"), fn)
    urls = net.dedupe([url, *list(step.get("fallback_urls") or [])])
    r, _races = net.download_race(urls, dl, digest=digest, algo="sha256")
    if not r.ok:
        out.error = f"download failed on every mirror ({r.error})"
        return out

    rep = verify.VerifyReport(integrity="sha256" if digest else "none")
    rep.level = "digest-only" if digest else "unverified"

    # optional provenance
    gpg = step.get("gpg")
    if gpg:
        if not verify.gpg_available():
            rep.notes.append("gpg not installed; signature not checked")
        else:
            keyring = gpg.get("keyring")
            if keyring and gpg.get("keys_url") and not os.path.exists(keyring):
                os.makedirs(os.path.dirname(keyring), exist_ok=True)
                verify.import_keys(gpg["keys_url"], gpg.get("keys_token", ""), keyring)
            fp = verify.gpg_verify(dl, gpg.get("sig_url") and _fetch_sig(gpg["sig_url"], dl),
                                   keyring=keyring)
            if fp and (not gpg.get("fingerprint") or fp.endswith(gpg["fingerprint"].replace(" ", "")[-16:])):
                rep.provenance = f"gpg:{fp[-16:]}"
            elif fp:
                warn(f"{formula}: signature by unexpected key {fp}")
            else:
                out.error = "GPG signature verification failed"
                return out
    team = step.get("codesign_team")

    work = os.path.join(net.home(), "tmp", f"{formula}-{os.getpid()}")
    shutil.rmtree(work, ignore_errors=True)
    ex = os.path.join(work, "x")
    stage = os.path.join(work, "stage")
    os.makedirs(ex, exist_ok=True)
    os.makedirs(stage, exist_ok=True)
    raw = step.get("fmt") == "raw"
    if raw:
        # A release asset that *is* the binary (jq-macos-amd64, go tools, …):
        # no archive to unpack, the command name comes from the recipe or the formula.
        names = step.get("bin") or [formula]
        if isinstance(names, str):
            names = [names]
        os.makedirs(os.path.join(stage, "bin"), exist_ok=True)
        for n in names:
            dest_bin = os.path.join(stage, "bin", os.path.basename(n))
            shutil.copy2(dl, dest_bin)
            archive.ensure_executable(dest_bin)
        info(f"installing single binary {os.path.basename(dl)} → {', '.join(names)}")
    else:
        info(f"extracting {os.path.basename(dl)}")
        archive.extract(dl, ex)
    if team and not raw:
        b = verify.first_binary(os.path.join(ex, (step.get("strip") and archive.flatten_root(ex)) or ex, "bin")) \
            or verify.first_binary(archive.flatten_root(ex))
        found = verify.codesign_team(b) if b else None
        if found == team:
            rep.provenance = f"codesign:{team}"
        elif found:
            warn(f"{formula}: signed by unexpected team {found} (expected {team})")
        else:
            rep.notes.append("codesign: no Apple signature (fine for most OSS tools)")
    if raw:
        declared = step.get("bin") or [formula]
        if isinstance(declared, str):
            declared = [declared]
        bins = {n: os.path.join("bin", os.path.basename(n)) for n in declared}
        mode = "bin"
    else:
        bins, mode = _install_archive_dir(ex, formula, step, pf, stage)

    # Pre-flight on the staged tree, before the working install is touched:
    # a rejected replacement (wrong CPU, unlaunchable) must leave the old one intact.
    for rel in bins.values():
        p = os.path.join(stage, rel)
        if not _macho_arch_ok(p, pf):
            out.error = f"{os.path.basename(p)} is built for another CPU architecture"
            shutil.rmtree(work, ignore_errors=True)
            return out
    main = next(iter(bins.values()), None)
    if main:
        minos = _min_macos(os.path.join(stage, main))
        if minos and pf.macos_version and minos > pf.macos_version[0]:
            warn(f"{formula}: binary declares minimum macOS {minos}, machine has "
                 f"{pf.macos_version[0]}; it may refuse to launch")

    archives_dir = os.path.join(root(), "opt")
    dest = os.path.join(archives_dir, formula)
    old = os.path.join(work, "old-root")
    moved_old = os.path.exists(dest)
    if moved_old:
        shutil.move(dest, old)
    os.makedirs(archives_dir, exist_ok=True)
    try:
        shutil.move(stage, dest)
        archive.strip_quarantine(dest)
        made = make_links({cmd: os.path.join(dest, rel) for cmd, rel in bins.items()})
    except BaseException:
        # put the previous working install back; its links resolve to the same paths
        shutil.rmtree(dest, ignore_errors=True)
        if moved_old:
            shutil.move(old, dest)
        shutil.rmtree(work, ignore_errors=True)
        raise
    rep.level = verify.ensure_trusted_level(rep.integrity, rep.provenance, rep.notes)
    out.installed = True
    out.prefix = dest
    out.bins = made
    out.report = rep.summary() + f" [{mode}]"
    shutil.rmtree(work, ignore_errors=True)
    _record_install(formula, step, out)
    return out


def _fetch_sig(url: str, data_path: str) -> str | None:
    dest = data_path + ".asc"
    r = net.download(url, dest, show=False)
    return dest if r.ok else None


def install_npm(step: dict, formula: str, pf: PlatformInfo) -> Outcome:
    out = Outcome(formula=formula, step=step)
    if not shutil.which("npm") or not shutil.which("node"):
        out.error = "npm/node not found on PATH (install Node first: `ibrew setup --apply`)"
        return out
    pkg = step.get("package", formula)
    version = step.get("version") or "latest"
    tgz = os.path.join(net.cache_dir("npm"), f"{pkg.replace('/', '-')}-{version}.tgz")
    meta_url = f"{_npm_registry()}/{pkg}/{version}"
    meta = net.request(meta_url, timeout=30)
    if not meta.ok:
        out.error = f"npm registry: cannot resolve {pkg}@{version}"
        return out
    d = meta.json()
    if "security" in str(d.get("version", "")) or (d.get("deprecated") and "security" in str(d.get("deprecated", ""))):
        out.error = f"'{pkg}' on npm is a squatted security placeholder, not the real tool; refusing"
        return out
    tarball = d.get("dist", {}).get("tarball", "")
    r = net.download(tarball, tgz, digest=d.get("dist", {}).get("shasum", ""), algo="sha1")
    if not r.ok:
        out.error = f"npm tarball download failed: {r.error}"
        return out
    prefix = os.path.join(root(), "npm")
    os.makedirs(prefix, exist_ok=True)
    r = run(["npm", "install", "-g", "--prefix", prefix, tgz, "--no-audit", "--no-fund"],
            timeout=900, capture=False)
    if not r.ok and r.returncode != 0:
        # capture=False → stdout/stderr are None; rerun with capture for the message
        r2 = run(["npm", "install", "-g", "--prefix", prefix, tgz], timeout=900)
        out.error = (r2.stderr or r2.stdout)[-600:]
        return out
    nbin = os.path.join(prefix, "bin")
    bins = {}
    if os.path.isdir(nbin):
        for f in os.listdir(nbin):
            bins[f] = os.path.join(nbin, f)
    made = make_links(bins)
    out.installed = True
    out.prefix = prefix
    out.bins = made
    out.report = f"npm install -g ({d.get('dist', {}).get('shasum', '')[:12]}…, sha1 from registry)"
    _record_install(formula, step, out)
    return out


def _npm_registry() -> str:
    r = run(["npm", "config", "get", "registry"], timeout=30)
    return (r.out or "https://registry.npmjs.org").rstrip("/")


_GHCR_ACCEPT = ("application/vnd.oci.image.index.v1+json, "
                "application/vnd.oci.image.manifest.v1+json")


class _BottleRejected(RuntimeError):
    """This bottle build cannot be used here; the next candidate may still work."""


class _BrewEnvironment(RuntimeError):
    """Homebrew itself cannot install here, so no other bottle candidate will help."""


def _real_brew() -> str:
    """Homebrew itself — never our shim, which would reroute us back into here."""
    from . import brewshim

    try:
        return brewshim.real_brew()
    except Exception:
        return shutil.which("brew") or "brew"


def _prefix_write_blocker() -> str:
    """Explain a prefix-permission refusal from ``brew install``, if that is what happened.

    Homebrew checks its prefix directories *before* it reads a formula from a bottle,
    so one root-owned directory makes every pour fail with a message about the prefix.
    The directory list is Homebrew's own, so it cannot drift from what it checks.
    """
    r = run([_real_brew(), "ruby", "-e",
             "puts Keg.must_be_writable_directories.select { |d| d.exist? && !d.writable? }"],
            timeout=120)
    bad = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip().startswith("/")]
    if not bad:
        return ""
    who = os.environ.get("USER") or "you"
    return (f"brew cannot install anything into its prefix: not writable by {who} "
            f"({', '.join(bad)}). This is an environment problem, not a bad bottle — "
            f"fix it with: sudo chown -R {who} {' '.join(bad)}")


def _ghcr_get(url: str, tok: str, *, key: str, ttl: int,
              accept: str = _GHCR_ACCEPT) -> dict:
    """GET a ghcr manifest, cached on disk (indexes 24 h, platform manifests 30 d)."""
    cache = os.path.join(net.cache_dir("ghcr"), re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")
    if net._ttl_fresh(cache, ttl):
        data = _read_json(cache, None)
        if isinstance(data, dict):
            return data
    r = net.request(url, headers={"Authorization": f"Bearer {tok}", "Accept": accept},
                    timeout=90)
    if not r.ok:
        raise RuntimeError(f"ghcr {url.rsplit('/', 1)[-1][:24]}: {r.error or r.status}")
    try:
        data = r.json()
    except Exception:
        raise RuntimeError("ghcr returned a non-JSON manifest") from None
    if not isinstance(data, dict):
        raise RuntimeError("ghcr returned an unexpected manifest")
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass
    return data


def _split_ref(ref: str) -> tuple[str, str]:
    """``1.8.2.sonoma`` → ("1.8.2", "sonoma"); a trailing rebuild is dropped too."""
    parts = [p for p in (ref or "").split(".") if p]
    if len(parts) >= 3 and parts[-1].isdigit():
        parts.pop()                              # ghcr writes `<version>.<tag>.<rebuild>`
    if len(parts) >= 2 and brew.macos_of_tag(parts[-1]) is not None:
        return ".".join(parts[:-1]), parts[-1]
    return ref or "", ""


def _entry_major(m: dict) -> int | None:
    """macOS major of one index entry, from its bottle tag or its platform field."""
    ref = (m.get("annotations") or {}).get("org.opencontainers.image.ref.name", "")
    major = brew.macos_of_tag(_split_ref(ref)[1])
    if major is not None:
        return major
    mm = re.match(r"\D*(\d+)", (m.get("platform") or {}).get("os.version", "") or "")
    return int(mm.group(1)) if mm else None


def _ghcr_resolve(formula: str, oci_tag: str, pf: PlatformInfo, want_tag: str = "") -> dict:
    """Pick this machine's build out of a ghcr OCI index.

    The CPU comes from ``pf.arch``, never from the tag string: ``ventura`` and
    ``sonoma`` are Intel tags, ``arm64_sonoma`` is not.
    """
    repo = brew.ghcr_repo(formula)
    tok = brew.ghcr_token(repo)
    if not tok:
        raise _BottleRejected(f"ghcr token denied for {repo}")
    base = f"https://ghcr.io/v2/homebrew/core/{repo}"
    idx = _ghcr_get(f"{base}/manifests/{oci_tag}", tok, key=f"{repo}-{oci_tag}-index",
                    ttl=24 * 3600)
    want_arch = "amd64" if pf.arch == "x86_64" else "arm64"
    mine = pf.macos_version[0] if pf.macos_version else 0
    want_major = brew.macos_of_tag(want_tag) if want_tag else None

    # ghcr answers with a bare manifest when a tag has a single platform build.
    entries = idx.get("manifests") or ([idx] if idx.get("layers") else [])
    best, best_rank = None, None
    for m in entries or []:
        p = m.get("platform") or {}
        if p.get("os") not in (None, "", "darwin"):
            continue                                    # linux bottles ride in the same index
        if p.get("architecture") not in (None, "", want_arch):
            continue
        major = _entry_major(m)
        if major is None or (mine and major > mine) or not m.get("digest"):
            continue
        rank = (1 if want_major and major == want_major else 0, major)
        if best_rank is None or rank > best_rank:
            best, best_rank = m, rank
    if not best:
        raise _BottleRejected(f"no {want_arch} darwin bottle for {formula} {oci_tag} "
                              f"usable on {pf.pretty_name}")

    digest = best["digest"]
    man = _ghcr_get(f"{base}/manifests/{digest}", tok, key=f"{repo}-{digest.split(':')[-1]}",
                    ttl=30 * 86400,
                    accept="application/vnd.oci.image.manifest.v1+json")
    ann = dict(best.get("annotations") or {})
    ann.update(man.get("annotations") or {})            # manifest annotations win

    layers = man.get("layers") or []
    layer = next((lay for lay in layers
                  if re.search(r"bottle(?:\.\d+)?\.tar\.gz$",
                               (lay.get("annotations") or {})
                               .get("org.opencontainers.image.title", ""))), None)
    if not layer:
        raise _BottleRejected(f"{formula} {oci_tag}: ghcr manifest carries no bottle layer")

    ref = ann.get("org.opencontainers.image.ref.name", "")
    version, ref_tag = _split_ref(ref)
    if not version:
        version, _ = brew.parse_oci_tag(oci_tag)
    platform = best.get("platform") or {}
    return {
        "layer": layer["digest"],
        "digest": ann.get("sh.brew.bottle.digest") or layer["digest"].split(":")[-1],
        "oci_tag": oci_tag,
        "version": version,
        "rebuild": brew.parse_oci_tag(oci_tag)[1],
        "tag": ref_tag or brew.tag_for(pf) or "",
        "os_version": platform.get("os.version", ""),
        "os_major": _entry_major(best),
        "tab": ann.get("sh.brew.tab") or "",
    }


def _brew_pin(formula: str, version: str) -> bool:
    """Pin an older-than-current bottle so ``brew upgrade`` cannot force a source build."""
    info_ = brew.bottle_info(formula)
    if not info_ or not version:
        return False
    if brew.version_key(version) >= brew.version_key(info_.version):
        return False
    if run([_real_brew(), "pin", formula], timeout=120).ok:
        warn(f"{formula} {version} predates {info_.version}; pinned so `brew upgrade` keeps it")
        return True
    return False


def _install_native_bottle(step: dict, formula: str, pf: PlatformInfo, out: Outcome) -> Outcome:
    """A bottle for this exact platform exists: let Homebrew do the whole job."""
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1")
    info(f"brew install {formula}   (native bottle for {brew.tag_for(pf)})")
    r = run([_real_brew(), "install", formula], timeout=3600, capture=False, env=env)
    if r.returncode != 0:
        out.error = f"brew install {formula} failed (exit {r.returncode})"
        return out
    version = _installed_version(formula) or step.get("version", "")
    kdir = brew.keg(formula, version)
    out.installed = True
    out.prefix = kdir
    out.bins = [c for c, _ in _formula_commands(formula, kdir)]
    out.report = f"brew bottle {brew.tag_for(pf)} (native install by Homebrew)"
    note = _keg_arch_note(kdir, formula, pf) if os.path.isdir(kdir) else ""
    if note:
        warn(note)
        out.report += f"; {note}"
    _record_install(formula, {**step, "version": version}, out)
    return out


def _pour_bottle(step: dict, formula: str, pf: PlatformInfo, out: Outcome, meta: dict) -> Outcome:
    """Download one verified bottle build, name it the way Homebrew expects, pour it."""
    repo = brew.ghcr_repo(formula)
    tok = brew.ghcr_token(repo)
    if not tok:
        raise _BottleRejected(f"ghcr token denied for {repo}")
    base = f"https://ghcr.io/v2/homebrew/core/{repo}"
    auth = {"Authorization": f"Bearer {tok}", "Accept": "application/octet-stream"}
    cache = net.cache_dir("bottles")
    layer = meta["layer"]
    layer_local = os.path.join(cache, os.path.basename(layer) + ".tar.gz")
    r = net.download(base + "/blobs/" + layer, layer_local, digest=layer.split(":")[1],
                     show=True, headers=auth)
    if not r.ok:
        raise RuntimeError(f"bottle download failed: {r.error}")

    ok_arch, why = _bottle_arch_ok(layer_local, pf)
    if not ok_arch:
        raise _BottleRejected(f"{formula} {meta['oci_tag']}: {why}")

    version = meta.get("version") or step.get("version") or ""
    tag_name = meta.get("tag") or step.get("tag") or ""
    if not version or not tag_name:
        raise _BottleRejected(f"{formula}: ghcr manifest does not name a version/tag")
    fn = f"{formula.replace('/', '-')}--{version}.{tag_name}.bottle"
    if meta.get("rebuild"):
        fn += f".{meta['rebuild']}"
    final = os.path.join(cache, fn + ".tar.gz")
    # Homebrew identifies a local bottle by this exact file name
    # (HOMEBREW_BOTTLES_EXTNAME_REGEX) and reads the pour metadata from
    # ``<name minus .tar.gz>.json`` — the shape `brew bottle` writes.
    if os.path.abspath(final) != os.path.abspath(layer_local):
        if os.path.exists(final):
            os.unlink(final)
        try:
            os.link(layer_local, final)
        except OSError:
            shutil.copyfile(layer_local, final)
    sidecar = os.path.join(cache, fn + ".json")
    tab = meta.get("tab")
    if isinstance(tab, str) and tab:
        try:
            meta["tab"] = tab = json.loads(tab)
        except ValueError:
            meta["tab"] = tab = None
    if isinstance(tab, dict):
        with open(sidecar, "w", encoding="utf-8") as fh:
            json.dump({formula: {"bottle": {"tags": {tag_name: {"tab": tab}}}}}, fh)
    elif os.path.exists(sidecar):
        os.unlink(sidecar)

    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1",
               HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK="1")
    info(f"brew install <{fn}>   (bottle {tag_name}, sha256 verified)")
    r = run([_real_brew(), "install", final], timeout=3600, capture=False, env=env)
    if r.returncode != 0:
        blocker = _prefix_write_blocker()
        if blocker:
            raise _BrewEnvironment(blocker)
        tail = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1]
        out.error = ("brew install of the verified bottle failed"
                     + (f": {tail}" if tail else " (its output is above; nothing was captured)"))
        raise _BottleRejected(out.error)

    kdir = brew.keg(formula, version)
    if not os.path.isdir(kdir):
        iv = _installed_version(formula)
        if iv:
            version, kdir = iv, brew.keg(formula, iv)
    out.installed = True
    out.prefix = kdir
    out.bins = [c for c, _ in _formula_commands(formula, kdir)]
    pinned = _brew_pin(formula, version)
    out.report = (f"brew bottle {tag_name} {version} (sha256 verified, Homebrew relocation applied"
                  + ("; pinned" if pinned else "") + ")")
    note = _keg_arch_note(kdir, formula, pf) if os.path.isdir(kdir) else ""
    if note:
        warn(note)
        out.report += f"; {note}"
    _record_install(formula, {**step, "version": version}, out)
    return out


def install_bottle(step: dict, formula: str, pf: PlatformInfo) -> Outcome:
    """Install a Homebrew *bottle* ourselves (brew can't: not a platform tag / old version).

    Candidates are tried newest-first: the current version, then older releases
    that still have a build this machine can run. The bottle's OCI config blob
    carries the Homebrew relocation info, which we stage next to the tarball
    before handing over to ``brew install <local bottle>`` so Homebrew still
    does linking, dependency checks and caveats.
    """
    out = Outcome(formula=formula, step=step)
    if not shutil.which("brew"):
        out.error = "brew not found"
        return out
    if step.get("native"):
        return _install_native_bottle(step, formula, pf, out)

    cands = list(step.get("candidates") or [])
    if not cands:
        cands = [{"oci_tag": step.get("version", ""), "version": step.get("version", ""),
                  "tag": step.get("tag", "")}]
    errors: list[str] = []
    for cand in cands:
        oci = str(cand.get("oci_tag") or cand.get("version") or "")
        if not oci:
            continue
        tag = cand.get("tag") or step.get("tag") or brew.tag_for(pf) or ""
        try:
            meta = _ghcr_resolve(formula, oci, pf, tag)
        except _BottleRejected as e:
            errors.append(str(e))
            continue
        try:
            return _pour_bottle(step, formula, pf, out, meta)
        except _BottleRejected as e:
            errors.append(str(e))
            continue
        except _BrewEnvironment as e:
            # Every candidate would fail the same way; stop instead of downloading more.
            out.error = str(e)
            return out
    if not errors:
        out.error = f"no bottle candidates for {formula}"
        return out
    out.error = "; ".join(errors[-3:])
    return out


def _formula_commands(formula: str, kdir: str) -> list[tuple]:
    out = []
    for sub in ("bin", "sbin"):
        d = os.path.join(kdir, sub)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                out.append((f, os.path.join(d, f)))
    return out


def _installed_version(formula: str) -> str:
    names = brew.installed_formulae()
    return names.get(formula, "")


def _record_install(formula: str, step: dict, out: Outcome) -> None:
    p = os.path.join(state_dir(), "installed.json")
    data = _read_json(p, {})
    data[formula] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "prefix": out.prefix,
                     "bins": out.bins, "source": step.get("source", step.get("kind")),
                     "version": step.get("version", ""), "report": out.report}
    _write_json(p, data)


def installed_state() -> dict[str, dict]:
    return _read_json(os.path.join(state_dir(), "installed.json"), {})


# --------------------------------------------------------------------------- #
# PATH profile (so `node`, `brew`, `npm` resolve to the right binaries)
# --------------------------------------------------------------------------- #

PROFILE_MARK = "# >>> intelbrew >>>"


PROFILE_START = "# >>> intelbrew >>>"
PROFILE_END = "# <<< intelbrew <<<"


def profile_candidates() -> list[str]:
    """Shell startup files to consider, most specific first for this $SHELL."""
    shell = os.path.basename(os.environ.get("SHELL", ""))
    home = os.path.expanduser("~")
    if shell == "zsh":
        # Interactive zsh reads ~/.zshrc; login shells read ~/.zprofile first.
        return [os.path.join(home, ".zshrc"), os.path.join(home, ".zprofile"),
                os.path.join(home, ".zshenv")]
    if shell == "bash":
        return [os.path.join(home, ".bash_profile"), os.path.join(home, ".bashrc"),
                os.path.join(home, ".profile")]
    return [os.path.join(home, ".profile"), os.path.join(home, ".zshrc")]


def profile_path() -> str:
    """The file to write: the first candidate that already exists, else the first."""
    for cand in profile_candidates():
        if os.path.exists(cand):
            return cand
    return profile_candidates()[0]


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def ensure_profile(write: bool = True) -> bool:
    """Add (or refresh) the PATH block in the user's shell startup file."""
    prof = profile_path()
    text = _read(prof)
    block = (f"{PROFILE_START}\n"
             f"# intelbrew binaries first, then the brew shim, then Homebrew.\n"
             f"# Remove this block to undo (`ibrew setup --uninstall` drops it too).\n"
             f'export PATH="{bin_dir()}:{_brew_bin_link_dir()}:$PATH"\n'
             f"{PROFILE_END}\n")
    if PROFILE_START in text:
        # replace a stale block (a path from another install would shadow the new one)
        kept, skipping = [], False
        for line in text.splitlines(keepends=True):
            if line.strip() == PROFILE_START:
                skipping = True
                continue
            if skipping and (line.strip() == PROFILE_END or line.strip().startswith(PROFILE_START)):
                skipping = False
                continue
            if not skipping:
                kept.append(line)
        text = "".join(kept).rstrip("\n") + "\n\n" + block
        if not write:
            return True
        with open(prof, "w", encoding="utf-8") as fh:
            fh.write(text)
        return True
    if not write:
        return False
    with open(prof, "a", encoding="utf-8") as fh:
        fh.write(("\n" if text and not text.endswith("\n") else "") + block)
    return True


def drop_profile() -> bool:
    """Remove the PATH block; returns True when something was removed."""
    removed = False
    for prof in profile_candidates():
        text = _read(prof)
        if PROFILE_START not in text:
            continue
        kept, skipping = [], False
        for line in text.splitlines(keepends=True):
            if line.strip() == PROFILE_START:
                skipping = True
                removed = True
                continue
            if skipping and (line.strip() == PROFILE_END
                             or line.strip().startswith(PROFILE_START)):
                skipping = False
                continue
            if not skipping:
                kept.append(line)
        with open(prof, "w", encoding="utf-8") as fh:
            fh.write("".join(kept))
    return removed


def _brew_bin_link_dir() -> str:
    return os.path.join(net.home(), "brew-bin")
