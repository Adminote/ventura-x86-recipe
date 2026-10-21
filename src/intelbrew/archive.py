"""Extraction of the realistic binary archive formats.

Security rules (enforced here, not by the caller):
  * every member path must stay inside the destination (no ``..``/absolute);
  * no device/FIFO/other special files;
  * absolute symlinks and symlinks escaping the destination are rejected;
  * hardlinks must point inside the archive.

Formats: ``.tar.gz|.tgz|.tar.bz2|.tar.xz|.txz|.tar.zst|.tar``, ``.zip`` (zip and
zip64, no ZIP64 extra-field tricks beyond what ``zipfile`` handles), ``.pkg`` /
``.dmg`` are *not* supported on purpose (see docs/limitations).
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tarfile
import zipfile

from .util import Unsupported, run, warn


class UnsafeArchive(Exception):
    pass


def kind(path: str) -> str:
    p = path.lower()
    if p.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if p.endswith((".tar.bz2", ".tbz2", ".tbz")):
        return "tar.bz2"
    if p.endswith((".tar.xz", ".txz")):
        return "tar.xz"
    if p.endswith((".tar.zst", ".tzst")):
        return "tar.zst"
    if p.endswith(".tar"):
        return "tar"
    if p.endswith(".zip"):
        return "zip"
    raise Unsupported(f"unsupported archive type: {os.path.basename(path)} "
                      f"(supported: tar.gz/tgz/tar.xz/tar.bz2/tar.zst/tar/zip)")


def _safe_rel(name: str) -> str:
    norm = os.path.normpath(name).replace("\\", "/")
    if norm.startswith(("/", "..")) or norm == ".":
        raise UnsafeArchive(f"unsafe path in archive: {name!r}")
    return norm


def _check_target(root: str, rel: str) -> str:
    target = os.path.join(root, rel)
    real_root = os.path.realpath(root)
    real_target = os.path.realpath(os.path.dirname(target))
    if not (real_target == real_root or real_target.startswith(real_root + os.sep)):
        raise UnsafeArchive(f"path escapes destination: {rel!r}")
    return target


def _extract_tar(tf: tarfile.TarFile, dest: str) -> None:
    root = os.path.realpath(dest)
    for member in tf:
        rel = _safe_rel(member.name)
        target = _check_target(root, rel)
        if member.isdir():
            os.makedirs(target, exist_ok=True)
            continue
        if member.issym():
            link = member.linkname
            if os.path.isabs(link):
                raise UnsafeArchive(f"absolute symlink: {rel} -> {link}")
            resolved = os.path.normpath(os.path.join(os.path.dirname(rel), link))
            if resolved.startswith(".."):
                raise UnsafeArchive(f"symlink escapes destination: {rel} -> {link}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.lexists(target):
                os.unlink(target)
            os.symlink(link, target)
            continue
        if member.islnk():
            src = _check_target(root, _safe_rel(member.linkname))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.link(src, target)
            continue
        if not member.isfile():
            raise UnsafeArchive(f"special file in archive: {rel}")
        os.makedirs(os.path.dirname(target) or root, exist_ok=True)
        src = tf.extractfile(member)
        if src is None:
            continue
        with src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        mode = member.mode & 0o777
        if mode:
            os.chmod(target, mode)


def _extract_zip(zf: zipfile.ZipFile, dest: str) -> None:
    root = os.path.realpath(dest)
    for info in zf.infolist():
        rel = _safe_rel(info.filename)
        target = _check_target(root, rel)
        # unix mode lives in the high 16 bits of external_attr
        mode = (info.external_attr >> 16) & 0o170777
        if stat.S_ISLNK(mode):
            link = zf.read(info).decode("utf-8")
            if os.path.isabs(link):
                raise UnsafeArchive(f"absolute symlink: {rel} -> {link}")
            resolved = os.path.normpath(os.path.join(os.path.dirname(rel), link))
            if resolved.startswith(".."):
                raise UnsafeArchive(f"symlink escapes destination: {rel} -> {link}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.lexists(target):
                os.unlink(target)
            os.symlink(link, target)
            continue
        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target) or root, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        if mode & 0o777:
            os.chmod(target, mode & 0o777)


def extract(archive: str, dest: str) -> None:
    """Extract ``archive`` into ``dest`` (which must exist)."""
    os.makedirs(dest, exist_ok=True)
    k = kind(archive)
    if k == "zip":
        with zipfile.ZipFile(archive) as zf:
            _extract_zip(zf, dest)
        return
    if k == "tar.zst":
        _extract_zstd(archive, dest)
        return
    mode = {"tar.gz": "r:gz", "tar.bz2": "r:bz2", "tar.xz": "r:xz", "tar": "r"}[k]
    with tarfile.open(archive, mode) as tf:
        _extract_tar(tf, dest)


def _extract_zstd(archive: str, dest: str) -> None:
    # Python 3.14+ has native zstd support in tarfile / compression.zstd.
    if sys.version_info >= (3, 14):
        with tarfile.open(archive, "r:zst") as tf:
            _extract_tar(tf, dest)
        return
    try:
        import zstandard  # type: ignore
    except ImportError as e:
        raise Unsupported("tar.zst needs the python package 'zstandard' "
                          "(pip install zstandard) or Python >= 3.14") from e
    import io

    with open(archive, "rb") as fh, \
            zstandard.ZstdDecompressor().stream_reader(fh) as reader, \
            tarfile.open(fileobj=io.BufferedReader(reader, 1 << 20), mode="r|") as tf:
        _extract_tar(tf, dest)


def flatten_root(path: str) -> str:
    """If an archive has exactly one top-level directory, return it (release tarballs)."""
    entries = [e for e in os.listdir(path) if not e.startswith(".")]
    if len(entries) == 1:
        cand = os.path.join(path, entries[0])
        if os.path.isdir(cand):
            return cand
    return path


def strip_quarantine(path: str) -> None:
    """Remove com.apple.quarantine xattr so Gatekeeper does not block helper binaries."""
    if shutil.which("xattr"):
        run(["xattr", "-cr", path], timeout=120)


def ensure_executable(path: str) -> None:
    try:
        st = os.stat(path)
        if not st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            os.chmod(path, st.st_mode | 0o755)
    except OSError:
        warn(f"could not chmod +x {path}")
