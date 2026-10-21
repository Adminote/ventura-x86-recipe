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


def _macho_arch_ok(path: str, pf: PlatformInfo) -> bool:
    """Reject Mach-O binaries that cannot run on this arch (universal is fine)."""
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
            if magic not in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
                return True  # not Mach-O (script etc.)
            fh.seek(0)
            head = fh.read(4096)
        # single-arch thin x86_64 has cputype 0x01000007 at offset 4 (LE)
        if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
            cpu = int.from_bytes(head[4:8], "little")
            if cpu == 0x01000007 and pf.arch != "x86_64":
                return False
            if cpu == 0x0100000C and pf.arch != "arm64":
                return False
    except OSError:
        pass
    return True


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
    archives_dir = os.path.join(root(), "opt")
    dest = os.path.join(archives_dir, formula)
    if os.path.exists(dest):
        shutil.move(dest, os.path.join(work, "old-root"))
        shutil.rmtree(os.path.join(work, "old-root"), ignore_errors=True)
    os.makedirs(archives_dir, exist_ok=True)
    shutil.move(stage, dest)
    archive.strip_quarantine(dest)

    # pre-flight: does the main binary look runnable here?
    main = next(iter(bins.values()), None)
    if main:
        p = os.path.join(dest, main)
        if not _macho_arch_ok(p, pf):
            out.error = f"{os.path.basename(p)} is built for another CPU architecture"
            return out
        minos = _min_macos(p)
        if minos and pf.macos_version and minos > pf.macos_version[0]:
            warn(f"{formula}: binary declares minimum macOS {minos}, machine has "
                 f"{pf.macos_version[0]}; it may refuse to launch")

    made = make_links({cmd: os.path.join(dest, rel) for cmd, rel in bins.items()})
    if os.environ.get("IBREW_NO_PROFILE") != "1":
        ensure_profile()
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


def _ghcr_resolve(formula: str, version: str, want_tag: str) -> tuple:
    """ghcr OCI index → (config blob, layer digest) of the platform variant for ``want_tag``."""
    repo = brew.ghcr_repo(formula)
    tok = brew.ghcr_token(repo)
    if not tok:
        raise RuntimeError(f"ghcr token denied for {repo}")
    base = f"https://ghcr.io/v2/homebrew/core/{repo}"

    def get(url, accept):
        r = net.request(url, headers={"Authorization": f"Bearer {tok}", "Accept": accept},
                        timeout=90)
        if not r.ok:
            raise RuntimeError(f"{url}: {r.error}")
        return r.json()

    idx = get(f"{base}/manifests/{version}",
              "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json")
    want = {"arch": "amd64" if "x86" in want_tag else "arm64", "os": "darwin",
            "os_version": brew.TAG_TO_MACOS[want_tag]}
    cands = idx.get("manifests") or [idx]
    for m in cands:
        p = m.get("platform") or {}
        osver = p.get("os.version", "")
        major = re.match(r"\D*(\d+)", osver)
        if (p.get("os") == "darwin" and p.get("architecture") == want["arch"]
                and major and int(major.group(1)) == want["os_version"]):
            ref = m.get("annotations", {}).get("org.opencontainers.image.ref.name", "")
            tag = ref.rsplit(".", 1)[-1] if ref else ""
            man = get(f"{base}/manifests/{m['digest']}",
                      "application/vnd.oci.image.manifest.v1+json")
            layers = man.get("layers", [])
            cfg = next((lay["digest"] for lay in layers
                        if (lay.get("annotations") or {}).get("sh.brew.bottle.install")), None) \
                or (man.get("config") or {}).get("digest")
            layer = next((lay["digest"] for lay in layers
                          if ((lay.get("annotations") or {})
                              .get("org.opencontainers.image.title") or "")
                          .endswith("bottle.tar.gz")), None)
            if cfg and layer:
                return cfg, layer, tag
    raise RuntimeError(f"no darwin/{want['arch']} macOS {want['os_version']} bottle in "
                       f"{formula} {version} on ghcr")


def install_bottle(step: dict, formula: str, pf: PlatformInfo) -> Outcome:
    """Install a Homebrew *bottle* ourselves (brew can't: not a platform tag / old version).

    The bottle's OCI config blob carries the Homebrew relocation info (cellar
    prefix + install receipts), which we stage before handing over to
    ``brew install <local bottle>`` so Homebrew still does the linking checks,
    dependency checks and caveats.
    """
    out = Outcome(formula=formula, step=step)
    if not shutil.which("brew"):
        out.error = "brew not found"
        return out
    version = step.get("version") or brew.bottle_info(formula).version
    want_tag = step.get("tag") or brew.tag_for(pf)
    try:
        cfg_digest, layer_digest, real_tag = _ghcr_resolve(formula, version, want_tag)
    except Exception as e:
        out.error = str(e)
        return out
    repo = brew.ghcr_repo(formula)
    tok = brew.ghcr_token(repo)
    base = f"https://ghcr.io/v2/homebrew/core/{repo}"
    cache = net.cache_dir("bottles")
    layer_local = os.path.join(cache, os.path.basename(layer_digest) + ".tar.gz")
    # ghcr blob URLs are digest-addressed; fetch with the token, verify the sha256
    r = net.download(base + "/blobs/" + layer_digest, layer_local,
                     digest=layer_digest.split(":")[1], show=True,
                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/octet-stream"})
    if not r.ok:
        out.error = f"bottle download failed: {r.error}"
        return out
    cfg_local = os.path.join(cache, os.path.basename(cfg_digest) + ".json")
    r = net.download(base + "/blobs/" + cfg_digest, cfg_local, digest=cfg_digest.split(":")[1],
                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/octet-stream"})
    if not r.ok:
        out.error = f"bottle config download failed: {r.error}"
        return out
    cfg = _read_json(cfg_local, {})
    if not cfg:
        out.error = "bottle config blob is not valid JSON"
        return out
    b = (cfg.get("config") or {}).get("Labels") or {}
    bottle_hash = b.get("sh.brew.bottle.digest") or ""
    want_ver = b.get("org.opencontainers.image.version") or version
    tag_name = b.get("sh.brew.bottle.digest.arch") or want_tag
    if b.get("sh.brew.bottle.digest"):
        cfg_bytes = json.dumps(cfg, indent=None, separators=(",", ":")).encode()
        with open(cfg_local, "wb") as fh:
            fh.write(cfg_bytes)
    # Local install of a bottle: Homebrew reads ``<file>.manifest.json`` next to
    # the wine, plus the bottle marker files inside the tarball.
    fn = f"{formula.replace('/', '-')}--{want_ver}.{tag_name}.bottle.tar.gz"
    final = os.path.join(cache, fn)
    sidecar = {
        "schemaVersion": 2,
        "manifests": [{
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + net.file_digest(cfg_local, "sha256"),
            "size": os.path.getsize(cfg_local),
            "platform": {"architecture": "amd64" if pf.arch == "x86_64" else "arm64",
                         "os": "darwin", "os.version": f"macOS {brew.TAG_TO_MACOS[want_tag]}"},
            "annotations": {"sh.brew.bottle.digest": bottle_hash or layer_digest.split(":")[1]},
        }],
    }
    local = os.path.join(cache, f"{os.path.basename(final)}.incomplete")
    shutil.copyfile(layer_local, local)
    with open(os.path.join(cache, f"{os.path.basename(local)}.manifest.json"), "w") as fh:
        json.dump(sidecar, fh)
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1",
               HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK="1")
    info(f"brew install <{os.path.basename(local)}>   (bottle {tag_name}, sha256 verified)")
    r = run(["brew", "install", local], timeout=3600, capture=False, env=env)
    if r.returncode != 0:
        r2 = run(["brew", "install", local], timeout=3600)
        out.error = ("brew install of the verified bottle failed: "
                     + ((r2.stderr or r2.stdout or "").strip().splitlines() or ["?"])[-1])
        return out
    kdir = brew.keg(formula, want_ver)
    out.installed = True
    out.prefix = kdir
    out.bins = [c for c, _ in _formula_commands(formula, kdir)]
    out.report = (f"brew bottle {tag_name} (sha256 verified, Homebrew relocation applied"
                  + ("; platform tag differs from Homebrew's own" if real_tag != brew.tag_for(pf) else "")
                  + ")")
    _record_install(formula, step, out)
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


def profile_path() -> str:
    shell = os.environ.get("SHELL", "")
    return os.path.expanduser("~/.zprofile" if shell.endswith("zsh") else "~/.profile")


def ensure_profile(write: bool = True) -> bool:
    prof = profile_path()
    if not write:
        return os.path.exists(prof) and PROFILE_MARK in open(prof, encoding="utf-8").read()
    text = ""
    if os.path.exists(prof):
        with open(prof, encoding="utf-8") as fh:
            text = fh.read()
    if PROFILE_MARK in text:
        return False
    block = f"""
{PROFILE_MARK}
# brew-first PATH order + intelbrew binaries in front of Homebrew links.
# `eval "$(ibrew env --"$SHELL")"` if you prefer generated settings.
export PATH="{bin_dir()}:{_brew_bin_link_dir()}:$PATH"
{PROFILE_MARK} <<<
"""
    with open(prof, "a", encoding="utf-8") as fh:
        fh.write(block)
    return True


def _brew_bin_link_dir() -> str:
    return os.path.join(net.home(), "brew-bin")
