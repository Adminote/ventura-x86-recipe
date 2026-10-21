"""Small platform / process helpers. Nothing here imports third-party code."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Output helpers (respect NO_COLOR / non-tty)
# --------------------------------------------------------------------------- #

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


_COLOR = _supports_color()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(f"{_c('36', '==>')} {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"{_c('32', '✓')} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"{_c('33', 'warning:')} {msg}", file=sys.stderr, flush=True)


def err(msg: str) -> None:
    print(f"{_c('31', 'error:')} {msg}", file=sys.stderr, flush=True)


class Unsupported(Exception):
    """Raised when the environment cannot be supported (fails closed)."""


# --------------------------------------------------------------------------- #
# Progress / liveness
# --------------------------------------------------------------------------- #

T0 = time.monotonic()


def elapsed() -> float:
    return time.monotonic() - T0


def progress(msg: str) -> None:
    """A step header that also tells the user how long we have been at it."""
    print(f"{_c('36', '==>')} {msg}  {_c('2', f'[{elapsed():.1f}s]')}", flush=True)


class activity:
    """Show that work is happening, even when it takes a minute.

    A terminal gets a spinner with elapsed time (so "slow" never looks like
    "hung"); a pipe/CI gets one plain line on start and one on finish.

    Usage::

        with activity("fetching formula index (32 MB)"):
            data = fetch()
    """

    def __init__(self, what: str, slow_after: float = 1.0):
        self.what = what
        self.slow_after = slow_after

    def __enter__(self) -> activity:
        tty = sys.stderr.isatty() and not os.environ.get("IBREW_NO_PROGRESS")
        self._stop = False
        self._start = time.monotonic()
        if not tty:
            print(f"{_c('36', '==>')} {self.what} …", file=sys.stderr, flush=True)
            self._thread = None
            return self
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        start = self._start

        def spin() -> None:
            i = 0
            while not self._stop:
                dt = time.monotonic() - start
                if dt >= self.slow_after:
                    sys.stderr.write(f"\r  {frames[i % len(frames)]} {self.what}  {dt:.1f}s   ")
                    sys.stderr.flush()
                i += 1
                time.sleep(0.1)

        self._thread = threading.Thread(target=spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stop = True
        dt = time.monotonic() - self._start
        th = getattr(self, "_thread", None)
        if th is not None:
            th.join(timeout=0.5)
            if sys.stderr.isatty():
                sys.stderr.write("\r" + " " * 78 + "\r")
                sys.stderr.flush()
        if exc_type is None and dt >= self.slow_after:
            print(f"  {_c('32', '✓')} {self.what}  {_c('2', f'{dt:.1f}s')}",
                  file=sys.stderr, flush=True)
        return False


# --------------------------------------------------------------------------- #
# Platform facts
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PlatformInfo:
    system: str            # "Darwin" | "Linux" | ...
    arch: str              # uname -m, e.g. "x86_64", "arm64"
    release: str           # kernel release, e.g. "22.6.0"
    machine: str           # "Intel" | "Apple Silicon" | "Unknown"
    pretty_name: str       # "macOS 13.7.8" / "Ubuntu 24.04"
    macos_version: tuple | None   # (13, 7, 8) for macOS, else None

    @property
    def is_intel_mac(self) -> bool:
        return self.system == "Darwin" and self.arch == "x86_64"

    @property
    def is_apple_silicon(self) -> bool:
        return self.system == "Darwin" and self.arch == "arm64"

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["macos_version"] = list(self.macos_version) if self.macos_version else None
        d["is_intel_mac"] = self.is_intel_mac
        return d


def _sysctl(key: str) -> str | None:
    try:
        out = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _pretty_os() -> str:
    if platform.system() == "Darwin":
        name = "macOS"
        try:
            v = platform.mac_ver()[0]
        except Exception:
            v = ""
        return f"{name} {v}".strip()
    try:  # Linux
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def current_platform() -> PlatformInfo:
    system = platform.system()
    arch = platform.machine()
    release = platform.release()
    machine = "Unknown"
    macos_version = None
    if system == "Darwin":
        brand = (_sysctl("machdep.cpu.brand_string") or "")
        if "Apple" in brand:
            machine = "Apple Silicon"
        elif "Intel" in brand or arch == "x86_64":
            machine = "Intel"
        try:
            parts = tuple(int(p) for p in platform.mac_ver()[0].split(".") if p.isdigit())
            macos_version = parts or None
        except Exception:
            macos_version = None
    elif system == "Linux":
        machine = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(arch, arch)
    return PlatformInfo(system, arch, release, machine, _pretty_os(), macos_version)


# --------------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()


def run(cmd: Iterable[str], *, timeout: float = 300, capture: bool = True,
        env: dict | None = None) -> Result:
    """Run a command. Never raises for non-zero exit; raises only on spawn errors/timeouts."""
    cmd = list(cmd)
    try:
        p = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout,
                           env=env, errors="replace")
        return Result(p.returncode, p.stdout or "", p.stderr or "")
    except subprocess.TimeoutExpired:
        warn(f"timeout after {timeout:.0f}s: {' '.join(cmd[:3])} …")
        return Result(124, "", "timeout")
    except FileNotFoundError:
        return Result(127, "", f"not found: {cmd[0]}")


def which(name: str) -> str | None:
    return shutil.which(name)


def sudo_available() -> bool:
    """True when sudo can run without prompting (cached ticket or NOPASSWD)."""
    if not which("sudo"):
        return False
    return run(["sudo", "-n", "true"], timeout=10).ok
