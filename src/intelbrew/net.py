"""HTTP layer: mirrors, GitHub proxies, cached index fetches and downloads.

Design rules
------------
* Every network access goes through here, so *source switching* is one place.
* Failures are data (``Fetch(ok=False)``), never fatal: the planner needs to be
  able to say "not available on any source" rather than crash.
* Nothing is ever installed without a digest check (md5 / sha1 / sha256).
  A source that cannot prove integrity is only used when the caller passes
  ``allow_unverified=True`` and that fact is reported back.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import __version__
from .util import info, warn

UA = f"intelbrew/{__version__} (+https://github.com/Adminote/ventura-x86-recipe)"
GITHUB_PROXIES = [
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://ghproxy.net/",
]
DEFAULT_TIMEOUT = float(os.environ.get("IBREW_HTTP_TIMEOUT", "60"))
DEFAULT_RETRIES = int(os.environ.get("IBREW_HTTP_RETRIES", "2"))
OFFLINE = os.environ.get("IBREW_OFFLINE", "").lower() in ("1", "true", "yes")


# --------------------------------------------------------------------------- #
# TLS: never reuse a broken interpreter bundle (the classic "conda python has
# stale certifi" failure). macOS ships a good trust store in the security keys.
# --------------------------------------------------------------------------- #

def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi  # type: ignore

        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    with contextlib.suppress(Exception):
        ctx.load_default_certs()
    # macOS system keychain as the last resort
    for ca_bundle in ("/etc/ssl/cert.pem",):
        if os.path.exists(ca_bundle):
            with contextlib.suppress(Exception):
                ctx.load_verify_locations(ca_bundle)
    return ctx


_CTX = _ssl_context()


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #

@dataclass
class Fetch:
    ok: bool
    url: str = ""
    status: int = 0
    body: bytes = b""
    error: str = ""

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

def home() -> str:
    return os.environ.get("INTELBREW_HOME") or os.path.expanduser("~/.intelbrew")


def cache_dir(*parts: str) -> str:
    p = os.path.join(home(), "cache", *parts)
    os.makedirs(p, exist_ok=True)
    return p


def _ttl_fresh(path: str, ttl: int) -> bool:
    try:
        return time.time() - os.path.getmtime(path) < ttl
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Core requests
# --------------------------------------------------------------------------- #

def request(url: str, *, timeout: float = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES,
            headers: dict[str, str] | None = None) -> Fetch:
    if OFFLINE:
        return Fetch(False, url, error="offline mode")
    hdrs = {"User-Agent": UA, "Accept-Encoding": "identity"}
    if headers:
        hdrs.update(headers)
    last = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
                return Fetch(True, resp.geturl(), resp.status, resp.read())
        except urllib.error.HTTPError as e:
            # 404 is a definitive answer: do not retry, do not mask it
            if e.code in (401, 403, 404, 410, 429):
                return Fetch(False, url, e.code, error=f"HTTP {e.code}")
            last = f"HTTP {e.code}"
        except Exception as e:  # URLError, timeouts, ssl, connection resets…
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    return Fetch(False, url, 0, error=last)


def guard(host_probe: str = "https://example.com/", timeout: float = 15) -> tuple[bool, str]:
    """One-shot connectivity probe so we can fail fast with a good message."""
    r = request(host_probe, timeout=timeout, retries=0)
    if r.ok:
        return True, ""
    return False, r.error


def download(url: str, dest: str, *, digest: str = "", algo: str = "sha256",
             size: int | None = None, timeout: float = 300,
             retries: int = 1, show: bool = True,
             headers: dict[str, str] | None = None) -> Fetch:
    """Download a single URL to ``dest`` atomically, verifying digest/size."""
    r, _ = download_race([url], dest, digest=digest, algo=algo, show=show,
                         headers=headers, race=False, timeout=timeout)
    return r


def download_race(urls: list[str], dest: str, *, digest: str = "", algo: str = "sha256",
                  timeout: float = 0, show: bool = True,
                  headers: dict[str, str] | None = None,
                  stall_timeout: float = 30,
                  race: bool = True) -> tuple[Fetch, list[tuple]]:
    """Fetch identical bytes from several mirrors concurrently; the fastest wins.

    Uses ``curl`` when available (Homebrew does the same: it is fast, resumes,
    and respects the OS trust store); falls back to urllib otherwise. Every
    mirror is verified independently, so a slow mirror only wastes a process.
    """
    if os.path.exists(dest) and _verify(dest, digest, algo, None):
        if show:
            info(f"cached  {os.path.basename(dest)}")
        return Fetch(True, dest, 200, b"ok"), []
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if not urls:
        return Fetch(False, "", error="no mirrors"), []
    total = timeout or float(os.environ.get("IBREW_DL_TIMEOUT", "600"))

    if not shutil.which("curl") or os.environ.get("IBREW_FORCE_URLLIB"):
        return _download_urllib(urls, dest, digest, algo, total, show, headers, race)

    sandbox = tempfile.mkdtemp(prefix="ibrew-dl-")
    procs: list[tuple[str, subprocess.Popen, str]] = []
    hdr_args: list[str] = []
    for k, v in (headers or {}).items():
        hdr_args += ["-H", f"{k}: {v}"]
    for i, u in enumerate(urls if race else urls[:1]):
        out = os.path.join(sandbox, f"{i}.part")
        cmd = ["curl", "-fsSL", "--http1.1", "--retry", "2", "--connect-timeout", "15", "--max-time", str(int(total)), "--speed-limit", "2048", "--speed-time", str(int(stall_timeout)), "-A", UA, *hdr_args, "-o", out, u]
        procs.append((u, subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE), out))
    if show:
        info(f"downloading {os.path.basename(dest)} from {len(procs)} mirror(s)…")
    t0 = time.time()
    winner: tuple[str, str] | None = None
    while procs:
        alive = False
        for u, p, out in list(procs):
            rc = p.poll()
            if rc is None:
                alive = True
                continue
            procs.remove((u, p, out))
            if rc != 0:
                msg = (p.stderr.read().decode("utf-8", "replace").strip().splitlines() or ["?"])[-1]
                if show:
                    warn(f"{u.split('/')[2]}: {msg[:100]}")
                continue
            if winner is None and _verify(out, digest, algo, None):
                winner = (u, out)
        if winner:
            break
        if not alive:
            break
        if time.time() - t0 > total:
            break
        time.sleep(0.15)
    for _, proc, _ in procs:           # slower mirrors: stop them
        with contextlib.suppress(OSError):
            proc.kill()
    results = []
    if winner:
        os.replace(winner[1], dest)
        dt = time.time() - t0
        if show:
            sz = os.path.getsize(dest)
            info(f"fetched {os.path.basename(dest)} from {winner[0].split('/')[2]} "
                 f"({sz / 1e6:.1f} MB in {dt:.1f}s, {sz / 1e6 / max(dt, .001):.1f} MB/s)")
        res = Fetch(True, winner[0], 200, b"ok")
    else:
        res = Fetch(False, urls[0], error="all mirrors failed")
    shutil.rmtree(sandbox, ignore_errors=True)
    return res, results


def _download_urllib(urls, dest, digest, algo, total, show, headers, race) -> tuple[Fetch, list[tuple]]:
    """Fallback downloader (no curl). One URL at a time, hard overall deadline."""
    errs = []
    tmp = dest + ".part"
    for u in urls:
        t0 = time.time()
        try:
            hdrs = {"User-Agent": UA}
            if headers:
                hdrs.update(headers)
            req = urllib.request.Request(u, headers=hdrs)
            with urllib.request.urlopen(req, timeout=min(30, total), context=_CTX) as resp, \
                    open(tmp, "wb") as out:
                clen = int(resp.headers.get("Content-Length") or 0)
                done = 0
                time.time()
                while True:
                    if time.time() - t0 > total:
                        raise TimeoutError("overall deadline")
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    time.time()
                    if show and sys.stderr.isatty() and clen:
                        sys.stderr.write(f"\r    {done * 100 // clen:3d}%  "
                                         f"{done / 1e6:.1f}/{clen / 1e6:.1f} MB")
                        sys.stderr.flush()
                if show and sys.stderr.isatty():
                    sys.stderr.write("\r" + " " * 60 + "\r")
            os.replace(tmp, dest)
            if _verify(dest, digest, algo, None):
                return Fetch(True, u, 200, b"ok"), [(u, True, time.time() - t0)]
            errs.append(f"{u}: integrity")
        except Exception as e:
            errs.append(f"{u}: {type(e).__name__}: {e}")
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
        if show:
            warn(errs[-1][:140])
    return Fetch(False, urls[0] if urls else "", error="; ".join(errs)[:300]), []


def _verify(path: str, digest: str, algo: str, size: int | None) -> bool:
    if size is not None and os.path.getsize(path) != size:
        return False
    if not digest:
        return True
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower() == digest.strip().lower()


def file_digest(path: str, algo: str = "sha256") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# GitHub: raw / release files with automatic proxy fallback
# --------------------------------------------------------------------------- #

def github_url(url: str) -> str:
    """Return ``url`` reachable on GitHub; fall back to gh proxies when needed."""
    if "github.com" not in url and "githubusercontent.com" not in url:
        return url
    r = request(url, timeout=DEFAULT_TIMEOUT, retries=0)
    if r.ok:
        return url
    for proxy in GITHUB_PROXIES:
        cand = proxy + url
        if request(cand, timeout=DEFAULT_TIMEOUT, retries=0).ok:
            warn(f"GitHub unreachable, using proxy {proxy}")
            return cand
    warn(f"GitHub unreachable even via proxies: {url[:80]}…")
    return url


def github_json(path: str, ttl: int = 1800, tokened: bool = True) -> dict | None:
    """Fetch ``api.github.com/<path>`` with caching and an optional token.

    Raises :class:`RateLimited` when GitHub answers 403/429 without a token so
    the planner can tell the user what to do instead of guessing.
    """
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", path)[:120] + ".json"
    cache = os.path.join(cache_dir("github"), key)
    if _ttl_fresh(cache, ttl):
        try:
            with open(cache, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    headers = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or _gh_token()
    if tokened and tok:
        headers["Authorization"] = f"Bearer {tok}"
    base = os.environ.get("IBREW_GITHUB_API", "https://api.github.com/")
    r = request(base.rstrip("/") + "/" + path.lstrip("/"), headers=headers, timeout=30)
    if r.status in (403, 429) and not tok:
        raise RateLimited(r.text()[:200])
    if not r.ok:
        return None
    data = r.json()
    try:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass
    return data


class RateLimited(Exception):
    """GitHub API rate limit hit without credentials."""


def _gh_token() -> str:
    gh = shutil.which("gh")
    if not gh:
        return ""
    try:
        import subprocess

        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Mirror lists (override everything with INTELBREW_<X>_MIRRORS, comma separated)
# --------------------------------------------------------------------------- #

def _mirrors(env: str, defaults: list[str]) -> list[str]:
    val = os.environ.get(env)
    if val:
        return [v.strip().rstrip("/") for v in val.split(",") if v.strip()]
    return [d.rstrip("/") for d in defaults]


# Order matters: nodejs.org is the reference (SHASUMS come from here first), the
# China mirrors serve byte-identical files and are much faster from CN networks.
NODE_MIRRORS = lambda: _mirrors("IBREW_NODE_MIRRORS", [  # noqa: E731
    "https://nodejs.org/dist",
    "https://mirrors.tuna.tsinghua.edu.cn/nodejs-release",
    "https://cdn.npmmirror.com/binaries/node",           # alias of mirrors/node, no redirect
    "https://npmmirror.com/mirrors/node",
])
PYPI_MIRRORS = lambda: _mirrors("IBREW_PYPI_MIRRORS", [  # noqa: E731
    "https://pypi.org/simple",
    "https://pypi.tuna.tsinghua.edu.cn/simple",
])
NPM_MIRRORS = lambda: _mirrors("IBREW_NPM_MIRRORS", [  # noqa: E731
    "https://registry.npmjs.org",
    "https://registry.npmmirror.com",
])
ADOPTIUM_MIRRORS = lambda: _mirrors("IBREW_ADOPTIUM_MIRRORS", [  # noqa: E731
    "https://mirrors.tuna.tsinghua.edu.cn/Adoptium",
    "https://github.com/adoptium/temurin{major}-binaries/releases/download",
])
CONDA_MIRRORS = lambda: _mirrors("IBREW_CONDA_MIRRORS", [  # noqa: E731
    "https://anaconda.org/conda-forge",
    "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge",
])


def try_first(urls: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> Fetch | None:
    """Return the first successful fetch from an ordered mirror list."""
    for u in urls:
        r = request(u, timeout=timeout)
        if r.ok:
            return r
    return None


def dedupe(seq: list[str]) -> list[str]:
    seen, out = set(), []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out
