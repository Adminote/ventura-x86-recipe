"""Integrity and provenance verification.

Levels (strongest first):
  digest      sha256/md5/sha1 published by a TLS source            (always available)
  sig-gpg     OpenPGP signature verified with `gpg`               (optional)
  sig-macho   Apple Developer ID code signature via `codesign`    (macOS, optional)

``gpg`` and ``codesign`` are *optional*: we use them when installed, and we
say so in the report when they were skipped. We never claim "verified up to X"
when only the digest was checked.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from typing import Optional

from .util import run


@dataclass
class VerifyReport:
    integrity: str = "none"          # "sha256" | "md5" | ...
    provenance: str = "none"         # "gpg:<key>" | "codesign:<team>" | "none"
    level: str = "digest-only"       # "verified" | "digest-only" | "unverified"
    notes: list = None

    def __post_init__(self):
        self.notes = self.notes or []

    def summary(self) -> str:
        extra = f" ({'; '.join(self.notes)})" if self.notes else ""
        return f"{self.level}: {self.integrity}{'+' + self.provenance if self.provenance != 'none' else ''}{extra}"


def gpg_available() -> bool:
    return shutil.which("gpg") is not None


def gpg_verify(data_file: str, sig_file: Optional[str] = None,
               auto: bool = False, keyring: Optional[str] = None) -> Optional[str]:
    """Verify a detached/auto signature; return the signer fingerprint or None."""
    if not gpg_available():
        return None
    args = ["gpg", "--batch", "--status-fd", "1"]
    if keyring:
        args += ["--no-default-keyring", "--keyring", keyring]
    if sig_file:
        args += ["--verify", sig_file, data_file]
    else:
        args += ["--verify", data_file]
    r = run(args, timeout=120)
    if not r.ok:
        return None
    m = re.search(r"\[GNUPG:\] VALIDSIG ([0-9A-Fa-f]+)", r.stdout)
    return m.group(1).upper() if m else None


def import_keys(key_url: str, token: str = "", keyring: Optional[str] = None) -> bool:
    from . import net

    r = net.request(key_url, headers={"Authorization": f"Bearer {token}"} if token else None,
                    timeout=60)
    if not r.ok:
        return False
    args = ["gpg", "--batch", "--import"]
    if keyring:
        args = ["gpg", "--batch", "--no-default-keyring", "--keyring", keyring, "--import"]
    import subprocess

    p = subprocess.run(args, input=r.body, capture_output=True, timeout=120)
    return p.returncode == 0


def codesign_team(path: str) -> Optional[str]:
    """Return the TeamIdentifier of a Mach-O binary, or None."""
    if not shutil.which("codesign"):
        return None
    r = run(["codesign", "-dv", path], timeout=60)
    m = re.search(r"TeamIdentifier=(\S+)", r.stderr + r.stdout)
    return m.group(1) if m else None


def macho_ok(path: str) -> bool:
    if not shutil.which("codesign"):
        return True
    r = run(["codesign", "--verify", "--strict", path], timeout=60)
    return r.ok


def looks_like_elf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def looks_like_macho(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return False
    return magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


def ensure_trusted_level(integrity: str, provenance: str, notes: list) -> str:
    if not integrity or integrity == "none":
        return "unverified"
    if provenance and provenance != "none":
        return "verified"
    return "digest-only"


def first_binary(directory: str) -> Optional[str]:
    """First executable Mach-O directly inside ``directory`` (for signature checks)."""
    try:
        for name in sorted(os.listdir(directory)):
            p = os.path.join(directory, name)
            if os.path.isfile(p) and looks_like_macho(p) and os.access(p, os.X_OK):
                return p
    except OSError:
        pass
    return None
