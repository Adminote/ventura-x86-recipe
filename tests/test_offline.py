"""Offline unit tests — no network, no installs to the real prefix.

Run: PYTHONPATH=src python3 -m pytest tests/ -q
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from intelbrew import archive, brew, planner, registry, sources, util

# --------------------------------------------------------------------------- #
# platform / tag mapping
# --------------------------------------------------------------------------- #

def test_tag_for_intel_ventura():
    pf = util.PlatformInfo("Darwin", "x86_64", "22.6.0", "Intel", "macOS 13.7.8", (13, 7, 8))
    assert brew.tag_for(pf) == "ventura"


def test_tag_for_arm64_sonoma():
    pf = util.PlatformInfo("Darwin", "arm64", "23.0.0", "Apple Silicon", "macOS 14.0", (14, 0))
    assert brew.tag_for(pf) == "arm64_sonoma"


def test_tag_for_unknown_macos_is_none():
    pf = util.PlatformInfo("Darwin", "x86_64", "99", "Intel", "macOS 42", (42, 0))
    assert brew.tag_for(pf) is None


def test_tag_for_linux_is_none():
    pf = util.PlatformInfo("Linux", "x86_64", "6.1", "x86_64", "Ubuntu", None)
    assert brew.tag_for(pf) is None


# --------------------------------------------------------------------------- #
# request parsing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("token,expected", [
    ("node", ("node", "")),
    ("node@22", ("node", "22")),
    ("brew-node", ("node", "")),
    ("brew-node@20", ("node", "20")),
])
def test_parse_request(token, expected):
    assert planner.parse_request(token) == expected


# --------------------------------------------------------------------------- #
# archive safety
# --------------------------------------------------------------------------- #

def _tar(path, members):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_archive_rejects_parent_escape(tmp_path):
    p = tmp_path / "evil.tar.gz"
    _tar(p, [("../../etc/passwd", b"x")])
    with pytest.raises(archive.UnsafeArchive):
        archive.extract(str(p), str(tmp_path / "out"))


def test_archive_rejects_absolute_path(tmp_path):
    p = tmp_path / "evil.tar.gz"
    _tar(p, [("/tmp/evil", b"x")])
    with pytest.raises(archive.UnsafeArchive):
        archive.extract(str(p), str(tmp_path / "out"))


def test_archive_rejects_escaping_symlink(tmp_path):
    p = tmp_path / "evil.tar.gz"
    with tarfile.open(p, "w:gz") as tf:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../etc/passwd"
        tf.addfile(link)
    with pytest.raises(archive.UnsafeArchive):
        archive.extract(str(p), str(tmp_path / "out"))


def test_archive_extracts_normal_tree(tmp_path):
    p = tmp_path / "ok.tar.gz"
    _tar(p, [("t/bin/tool", b"#!/bin/sh\necho hi\n")])
    out = tmp_path / "out"
    archive.extract(str(p), str(out))
    f = out / "t" / "bin" / "tool"
    assert f.read_bytes().startswith(b"#!/bin/sh")


def test_archive_zip_symlink_and_modes(tmp_path):
    p = tmp_path / "ok.zip"
    with zipfile.ZipFile(p, "w") as zf:
        info = zipfile.ZipInfo("bin/tool")
        info.external_attr = (0o100755 << 16)
        zf.writestr(info, "#!/bin/sh\n")
    out = tmp_path / "out"
    archive.extract(str(p), str(out))
    assert os.access(out / "bin" / "tool", os.X_OK)


def test_unsupported_archive_type(tmp_path):
    p = tmp_path / "x.pkg"
    p.write_bytes(b"x")
    with pytest.raises(util.Unsupported):
        archive.kind(str(p))


def test_flatten_root_detects_single_dir(tmp_path):
    d = tmp_path / "x" / "node-v1"
    d.mkdir(parents=True)
    assert archive.flatten_root(str(tmp_path / "x")) == str(d)


# --------------------------------------------------------------------------- #
# asset filtering (architecture must never be guessed)
# --------------------------------------------------------------------------- #

def _pf(arch="x86_64"):
    mac = (13, 7, 8) if arch == "x86_64" else (14, 0)
    return util.PlatformInfo("Darwin", arch, "22", "Intel" if arch == "x86_64" else "Apple Silicon",
                             "macOS 13.7.8", mac)


def test_filter_assets_picks_intel_and_skips_arm():
    assets = [
        {"name": "tool-1.0-aarch64-apple-darwin.tar.gz"},
        {"name": "tool-1.0-x86_64-apple-darwin.tar.gz"},
        {"name": "tool-1.0-x86_64-pc-windows-msvc.zip"},
        {"name": "tool-1.0-x86_64-unknown-linux-gnu.tar.gz"},
        {"name": "tool-1.0-x86_64-apple-darwin.tar.gz.sha256"},
    ]
    got = [a["name"] for a in sources.filter_assets(assets, _pf())]
    assert got == ["tool-1.0-x86_64-apple-darwin.tar.gz"]


def test_filter_assets_rejects_pkg_and_dmg():
    assets = [{"name": "tool-x86_64-apple-darwin.pkg"},
              {"name": "tool-x86_64-apple-darwin.dmg"}]
    assert sources.filter_assets(assets, _pf()) == []


def test_filter_assets_arm_host_skips_intel_build():
    assets = [{"name": "tool-darwin-amd64.tar.gz"},
              {"name": "tool-darwin-arm64.tar.gz"}]
    assert [a["name"] for a in sources.filter_assets(assets, _pf("arm64"))] == \
        ["tool-darwin-arm64.tar.gz"]


def test_sha256_file_like_accepts_github_digest():
    assert sources.sha256_file_like({"digest": "sha256:" + "a" * 64}) == "a" * 64
    assert sources.sha256_file_like({"digest": "md5:beef"}) == ""
    assert sources.sha256_file_like({}) == ""


# --------------------------------------------------------------------------- #
# npm allowlist — never map arbitrary brew names onto npm
# --------------------------------------------------------------------------- #

def test_npm_allowlist_contains_js_tools():
    assert "typescript" in planner.NPM_TOOLS
    assert "uvicorn" not in planner.NPM_TOOLS     # squatted placeholder on npm
    assert "ffmpeg" not in planner.NPM_TOOLS


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

def test_bundled_recipes_are_valid():
    for name in ("ripgrep", "uv"):
        r = registry.load(name)
        assert r is not None, name
        assert r.steps and r.steps[0]["sha256"], name
        assert len(r.steps[0]["sha256"]) == 64, name


def test_recipe_requirement_gate():
    pf_old = util.PlatformInfo("Darwin", "x86_64", "19", "Intel", "macOS 10.15", (10, 15))
    r = registry.load("uv")
    assert r.check_requirements(pf_old) != ""


# --------------------------------------------------------------------------- #
# verification levels
# --------------------------------------------------------------------------- #

def test_verify_levels():
    v = {"none": ("none", "unverified"),
         "sha256": ("none", "digest-only"),
         "sha256+gpg": ("gpg:ABC", "verified")}
    for _, (prov, level) in v.items():
        from intelbrew import verify
        assert verify.ensure_trusted_level("sha256", prov, []) == level or prov == "none"
    from intelbrew import verify
    assert verify.ensure_trusted_level("", "none", []) == "unverified"
    assert verify.ensure_trusted_level("sha256", "none", []) == "digest-only"
    assert verify.ensure_trusted_level("sha256", "gpg:ABC", []) == "verified"


# --------------------------------------------------------------------------- #
# raw single-binary releases (no archive)
# --------------------------------------------------------------------------- #

def test_raw_asset_is_accepted_and_marked_non_archive():
    assets = [{"name": "jq-macos-arm64"}, {"name": "jq-macos-amd64"},
              {"name": "jq-linux-amd64"}, {"name": "jq-win64.exe"},
              {"name": "sha256sum.txt"}, {"name": "jq-attestation.json"},
              {"name": "jq-1.8.2.tar.gz"}]
    names = [a["name"] for a in sources.filter_assets(assets, _pf())]
    assert "jq-macos-amd64" in names
    assert "jq-macos-arm64" not in names          # other architecture
    assert "sha256sum.txt" not in names           # metadata, not payload
    assert "jq-attestation.json" not in names
    assert sources.is_archive("jq-1.8.2.tar.gz") is True
    assert sources.is_archive("jq-macos-amd64") is False


def test_clean_version_handles_prefixed_tags():
    assert sources._clean_version("v15.2.0") == "15.2.0"
    assert sources._clean_version("jq-1.8.2") == "1.8.2"
    assert sources._clean_version("1.8.2") == "1.8.2"


def test_jq_recipe_is_a_raw_binary():
    r = registry.load("jq")
    assert r is not None
    step = r.steps[0]
    assert step["fmt"] == "raw"
    assert step["bin"] == ["jq"]
    assert len(step["sha256"]) == 64
    assert r.check_requirements(_pf()) == ""


# --------------------------------------------------------------------------- #
# reinstall safety: a rejected replacement must not destroy the working install
# --------------------------------------------------------------------------- #

_MACHO_MAGIC = b"\xcf\xfa\xed\xfe"
_CPU_X86_64 = (0x01000007).to_bytes(4, "little")
_CPU_ARM64 = (0x0100000C).to_bytes(4, "little")


def _macho(cpu: bytes, tag: bytes = b"") -> bytes:
    return _MACHO_MAGIC + cpu + b"\x00" * 60 + tag


def _bin_archive(path, members):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))


def _fake_download(monkeypatch, src):
    from intelbrew import net

    def fake(urls, dest, *, digest="", algo="sha256", **kw):
        shutil.copyfile(src, dest)
        return net.Fetch(ok=True, url=urls[0]), []

    monkeypatch.setattr(net, "download_race", fake)


def _archive_step(name):
    return {"kind": "archive", "url": f"https://example.invalid/{name}",
            "sha256": "0" * 64, "fmt": "", "bin": [], "strip": 1}


def _sandbox(tmp_path, monkeypatch):
    ib = tmp_path / "ib"
    monkeypatch.setenv("INTELBREW_HOME", str(ib))
    monkeypatch.setenv("IBREW_BIN_DIR", str(ib / "bin"))
    from intelbrew import installer

    return installer, ib


def test_reinstall_with_wrong_cpu_keeps_the_working_install(tmp_path, monkeypatch):
    installer, ib = _sandbox(tmp_path, monkeypatch)
    old = ib / "root" / "opt" / "tool" / "bin" / "tool"
    old.parent.mkdir(parents=True)
    old.write_bytes(_macho(_CPU_X86_64, b"old"))
    old.chmod(0o755)
    installer.make_links({"tool": str(old)})

    bad = tmp_path / "tool-arm64.tar.gz"
    _bin_archive(bad, [("tool-1.0/bin/tool", _macho(_CPU_ARM64))])
    _fake_download(monkeypatch, bad)

    out = installer.install_archive(_archive_step("tool-arm64.tar.gz"), "tool", _pf(),
                                    allow_unverified=False)

    assert not out.installed
    assert "another CPU architecture" in out.error
    assert old.read_bytes().endswith(b"old")          # working install untouched
    link = ib / "bin" / "tool"
    assert link.is_symlink()
    assert link.read_bytes().endswith(b"old")


def test_reinstall_rejects_a_wrong_cpu_secondary_binary(tmp_path, monkeypatch):
    installer, ib = _sandbox(tmp_path, monkeypatch)
    d = ib / "root" / "opt" / "uv" / "bin"
    d.mkdir(parents=True)
    for n in ("uv", "uvx"):
        (d / n).write_bytes(_macho(_CPU_X86_64, b"old"))
        (d / n).chmod(0o755)
    installer.make_links({"uv": str(d / "uv"), "uvx": str(d / "uvx")})

    bad = tmp_path / "uv.tar.gz"
    _bin_archive(bad, [("uv-1.0/bin/uv", _macho(_CPU_X86_64, b"new")),
                       ("uv-1.0/bin/uvx", _macho(_CPU_ARM64))])
    _fake_download(monkeypatch, bad)

    out = installer.install_archive(_archive_step("uv.tar.gz"), "uv", _pf(),
                                    allow_unverified=False)

    assert not out.installed
    assert "uvx is built for another CPU architecture" in out.error
    for n in ("uv", "uvx"):
        assert (d / n).read_bytes().endswith(b"old")


def test_reinstall_with_the_right_cpu_replaces_the_old_tree(tmp_path, monkeypatch):
    installer, ib = _sandbox(tmp_path, monkeypatch)
    old = ib / "root" / "opt" / "tool" / "bin" / "tool"
    old.parent.mkdir(parents=True)
    old.write_bytes(_macho(_CPU_X86_64, b"old"))
    old.chmod(0o755)
    installer.make_links({"tool": str(old)})

    good = tmp_path / "tool.tar.gz"
    _bin_archive(good, [("tool-2.0/bin/tool", _macho(_CPU_X86_64, b"new"))])
    _fake_download(monkeypatch, good)

    out = installer.install_archive(_archive_step("tool.tar.gz"), "tool", _pf(),
                                    allow_unverified=False)

    assert out.installed, out.error
    assert (ib / "bin" / "tool").read_bytes().endswith(b"new")
    assert not any((ib / "tmp").iterdir())


# --------------------------------------------------------------------------- #
# shell profile handling (the "ibrew: command not found" class of bug)
# --------------------------------------------------------------------------- #

def test_profile_block_is_written_and_removed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("# user config\nexport FOO=1\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/zsh")

    from intelbrew import installer

    installer.ensure_profile()
    text = (home / ".zshrc").read_text()
    assert installer.PROFILE_START in text
    assert installer.PROFILE_END in text
    assert "export PATH=" in text
    assert "export FOO=1" in text            # user content preserved

    installer.ensure_profile()               # idempotent: no duplicate block
    assert (home / ".zshrc").read_text().count("export PATH=") == 1

    assert installer.drop_profile() is True
    assert "intelbrew" not in (home / ".zshrc").read_text()
    assert "export FOO=1" in (home / ".zshrc").read_text()


def test_profile_targets_zshrc_even_if_zprofile_exists(tmp_path, monkeypatch):
    """Homebrew writes .zprofile; interactive zsh only reads it for login shells."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("")
    (home / ".zprofile").write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    from intelbrew import installer

    assert installer.profile_path() == str(home / ".zshrc")


def test_installing_a_tool_does_not_touch_the_profile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("INTELBREW_HOME", str(tmp_path / "ib"))
    # install_archive() must never edit shell config; only `setup --apply` may
    import inspect

    from intelbrew import installer

    body = inspect.getsource(installer.install_archive)
    assert "ensure_profile(" not in body, "install must not write the shell profile"


# --------------------------------------------------------------------------- #
# node runtime fallback (what happens when no binary exists)
# --------------------------------------------------------------------------- #

def _fake_index(monkeypatch, versions):
    idx = [{"version": v, "lts": False, "files": ["osx-x64-tar"]} for v in versions]
    monkeypatch.setattr(sources, "node_index", lambda: idx)
    monkeypatch.setattr(sources, "node_supported_arch", lambda pf, e: "darwin-x64")


def test_unknown_major_is_refused_with_the_available_list(monkeypatch):
    from intelbrew import nodemgr

    _fake_index(monkeypatch, ["v22.11.0", "v20.18.0"])
    with pytest.raises(nodemgr.NodeUnavailable) as e:
        nodemgr.ensure_runtime("99", _pf())
    assert "no official" in str(e.value)
    assert "22, 20" in str(e.value)
    assert e.value.hint.startswith("ibrew node use")


def test_newest_that_needs_a_newer_macos_falls_back(monkeypatch):
    from intelbrew import nodemgr

    _fake_index(monkeypatch, ["v26.1.0", "v22.11.0"])

    def fake_install(cand, pf, force):
        if cand["version"].startswith("v26"):
            raise nodemgr._TooNew("build requires macOS 15+ (this machine: macOS 13)")
        return "/fake/v22.11.0/bin/node"

    monkeypatch.setattr(nodemgr, "_install_candidate", fake_install)
    assert nodemgr.ensure_runtime(None, _pf()) == "/fake/v22.11.0/bin/node"


def test_all_candidates_too_new_reports_why(monkeypatch):
    from intelbrew import nodemgr

    _fake_index(monkeypatch, ["v26.1.0"])

    def fake_install(cand, pf, force):
        raise nodemgr._TooNew("build requires macOS 15+")

    monkeypatch.setattr(nodemgr, "_install_candidate", fake_install)
    with pytest.raises(nodemgr.NodeUnavailable) as e:
        nodemgr.ensure_runtime(None, _pf())
    assert "newer macOS" in str(e.value)


def test_shims_refuse_to_run_a_missing_runtime(tmp_path, monkeypatch):
    """A broken target must fail loudly, not fall through to another node."""
    monkeypatch.setenv("INTELBREW_HOME", str(tmp_path / "ib"))
    from intelbrew import nodemgr

    node = tmp_path / "ib" / "node" / "v20.0.0" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\necho fake\n")
    node.chmod(0o755)
    cli = tmp_path / "ib" / "node" / "v20.0.0" / "lib" / "node_modules" / "npm" / "bin"
    cli.mkdir(parents=True)
    for name in ("npm-cli.js", "npx-cli.js"):
        (cli / name).write_text("")

    monkeypatch.setattr(nodemgr, "ensure_runtime", lambda major, pf: str(node))
    nodemgr.write_shims("shim", "20", _pf())
    for name in ("node", "npm", "npx"):
        text = (tmp_path / "ib" / "bin" / name).read_text()
        assert "missing" in text and "ibrew node use" in text, name

    # delete the runtime and check the shim's exit code + message
    import subprocess

    node.unlink()
    p = subprocess.run([str(tmp_path / "ib" / "bin" / "node"), "-v"],
                       capture_output=True, text=True)
    assert p.returncode == 127
    assert "managed Node runtime is missing" in p.stderr


# --------------------------------------------------------------------------- #
# doctor exit codes (CI must not fail because a checkout is not "installed")
# --------------------------------------------------------------------------- #

def test_install_path_is_skipped_when_running_from_source(monkeypatch):
    from intelbrew import doctor

    monkeypatch.setattr(doctor.sys, "argv", ["/repo/src/intelbrew/cli.py"])
    monkeypatch.setattr(doctor, "running_from_source", lambda: True)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    check = doctor.check_install_path()
    assert check.status == "skipped"
    assert "source checkout" in check.detail


def test_install_path_fails_for_an_installed_launcher(monkeypatch):
    from intelbrew import doctor

    monkeypatch.setattr(doctor, "running_from_source", lambda: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    check = doctor.check_install_path()
    assert check.status == "fail"


def test_broken_recipe_fails_validation(tmp_path, monkeypatch):
    """A wrong digest must be a failure, not a warning."""
    reg = tmp_path / "registry"
    reg.mkdir()
    (reg / "broken.json").write_text(json.dumps({
        "name": "broken", "version": "1.0",
        "steps": [{"kind": "archive", "source": "manual",
                   "url": "https://example.invalid/x.tar.gz",
                   "sha256": "0" * 64, "strip": 1}],
    }))
    monkeypatch.setenv("IBREW_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("INTELBREW_HOME", str(tmp_path / "ib"))
    from intelbrew import doctor

    checks = doctor.validate_recipe("broken", _pf(), apply=True)
    assert any(c.status == "fail" for c in checks), [c.name for c in checks]


def test_validate_passes_when_digest_and_install_work(tmp_path, monkeypatch):
    """The happy path must stay a pass without touching the network."""
    reg = tmp_path / "registry"
    reg.mkdir()
    (reg / "ok.json").write_text(json.dumps({
        "name": "ok", "version": "1.0",
        "steps": [{"kind": "archive", "source": "manual",
                   "url": "https://example.invalid/x.tar.gz",
                   "sha256": "a" * 64, "strip": 1}],
    }))
    monkeypatch.setenv("IBREW_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("INTELBREW_HOME", str(tmp_path / "ib"))
    from intelbrew import doctor, installer

    class Fake:
        installed = True
        prefix = "/tmp/ok"
        bins = ("ok",)
        report = "digest-only: sha256 [bin]"
        error = ""

    monkeypatch.setattr(installer, "install_archive", lambda *a, **k: Fake())
    monkeypatch.setattr(doctor, "run", lambda *a, **k: type("R", (), {
        "ok": True, "returncode": 0, "stdout": "ok 1.0", "stderr": ""})())
    checks = doctor.validate_recipe("ok", _pf(), apply=True)
    assert not any(c.status == "fail" for c in checks), [c.name for c in checks]


# --------------------------------------------------------------------------- #
# `ibrew why` classification (pure functions, no network)
# --------------------------------------------------------------------------- #

def _npm_meta(**kw):
    base = {"name": "x", "version": "1.0.0"}
    base.update(kw)
    return base


def test_why_npm_pure_js():
    from intelbrew import why

    kind, evidence = why._npm_native(_npm_meta(bin={"x": "cli.js"}))
    assert kind == "js", evidence


def test_why_npm_prebuilt_markers():
    from intelbrew import why

    kind, _ = why._npm_native(_npm_meta(optionalDependencies={"@img/x-darwin-x64": "1"}))
    assert kind == "prebuilt"
    kind, _ = why._npm_native(_npm_meta(dependencies={"prebuild-install": "^7"}))
    assert kind == "prebuilt"


def test_why_npm_needs_a_compiler():
    from intelbrew import why

    kind, evidence = why._npm_native(_npm_meta(scripts={"install": "node-gyp rebuild"}))
    assert kind == "build" and "node-gyp" in evidence


def test_why_npm_platform_and_engines():
    from intelbrew import why

    meta = _npm_meta(os=["linux"], cpu=["x64"])
    assert "not darwin" in why._npm_platform(meta, _pf()) or why._npm_platform(meta, _pf())
    meta = _npm_meta(engines={"node": ">=99"})
    assert "needs node" in why._engines_problem(meta)


def test_why_verdict_warns_about_unrelated_npm_package():
    data = {"query": "htop", "platform": "macOS 13",
            "on_path": {"path": "", "managed": False, "version": ""},
            "brew": {"exists": True, "version": "3.5.3", "native_bottle": False,
                     "tag": "ventura", "deprecated": False, "disabled": False},
            "recipe": {"exists": False}, "plan": {"ok": False, "how": "none"},
            "npm": {"exists": True, "version": "1.0.1", "kind": "js",
                    "bin": [], "description": "handle-to-promise"}}
    lines = " ".join(why_verdict(data))
    assert "unrelated" in lines
    assert "works" not in lines


def test_why_verdict_calls_a_binless_package_a_library():
    data = {"query": "sharp", "platform": "macOS 13",
            "on_path": {"path": "", "managed": False, "version": ""},
            "brew": {"exists": False, "version": "", "native_bottle": False,
                     "tag": "ventura", "deprecated": False, "disabled": False},
            "recipe": {"exists": False}, "plan": {"ok": False, "how": "none"},
            "npm": {"exists": True, "version": "0.35.5", "kind": "prebuilt",
                    "bin": [], "description": "image processing"}}
    text = " ".join(why_verdict(data))
    assert "is a library" in text
    assert "unrelated" not in text


def why_verdict(data):
    from intelbrew import why

    return why.verdict(data)


# --------------------------------------------------------------------------- #
# progress / liveness
# --------------------------------------------------------------------------- #

def test_activity_prints_start_and_finish_when_not_a_tty(capsys):
    import time

    from intelbrew import util

    with util.activity("doing something slow", slow_after=0.05):
        time.sleep(0.12)
    err = capsys.readouterr().err
    assert "doing something slow" in err
    assert "0.1" in err or "0.0" in err           # finished with a duration


def test_progress_header_carries_elapsed(capsys):
    from intelbrew import util

    util.progress("resolving things")
    out = capsys.readouterr().out
    assert "resolving things" in out
    assert "[" in out and "s]" in out


def test_activity_swallows_nothing_on_exception(capsys):
    from intelbrew import util

    with pytest.raises(RuntimeError), util.activity("boom", slow_after=0):
        raise RuntimeError("x")
    assert "✓" not in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# percentages
# --------------------------------------------------------------------------- #

def test_percent_math():
    from intelbrew import util

    assert util.percent(0, 4) == 0
    assert util.percent(2, 4) == 50
    assert util.percent(4, 4) == 100
    assert util.percent(1, 0) == 100        # never divide by zero


def test_progress_header_shows_step_and_percent(capsys):
    from intelbrew import util

    util.set_steps(4)
    util.step_header(3, 4, "installing uvdar")
    out = capsys.readouterr().out
    assert "[3/4" in out and "50.0%" in out and "uvdar" in out


def test_progress_done_counts_up_and_finished_reports_100(capsys):
    from intelbrew import util

    util.set_steps(2)
    util.progress("one done", done=True)
    assert "50.0%" in capsys.readouterr().out
    util.progress("all done", done=True, finished=True)
    out = capsys.readouterr().out
    assert "100.0%" in out


def test_download_ticker_reports_percentage(tmp_path):
    from intelbrew import net

    part = tmp_path / "0.part"
    side = tmp_path / "0.size"
    part.write_bytes(b"x" * 10)
    side.write_text("5000000")
    procs = [("https://example.com/f.tgz", None, str(part), str(side))]
    tick = net._DownloadTicker(str(tmp_path / "f.tgz"), full=10_000_000, procs=procs)
    sizes = tick._sizes()
    assert sizes == [5000000]
    from intelbrew.util import percent

    assert 49.0 < percent(sizes[0], tick.full) < 51.0


# --------------------------------------------------------------------------- #
# bottle tags: CPU comes from the machine, never from the tag string
# --------------------------------------------------------------------------- #

def test_macos_of_tag_handles_the_arm64_prefix():
    assert brew.macos_of_tag("ventura") == 13
    assert brew.macos_of_tag("arm64_sonoma") == 14
    assert brew.macos_of_tag("x86_64_linux") is None
    assert brew.macos_of_tag(None) is None


def test_best_bottle_tag_for_intel_picks_the_newest_runnable_one():
    tags = ["sonoma", "ventura", "arm64_sonoma", "tahoe", "x86_64_linux"]
    assert brew.best_bottle_tag(tags, _pf()) == ("ventura", True)
    assert brew.best_bottle_tag(["monterey", "ventura", "sonoma"], _pf()) == ("ventura", True)
    assert brew.best_bottle_tag(["big_sur", "monterey"], _pf()) == ("monterey", False)
    assert brew.best_bottle_tag(["arm64_sonoma"], _pf()) == (None, False)


def test_best_bottle_tag_on_arm64_ignores_intel_tags():
    pf = _pf("arm64")
    assert brew.best_bottle_tag(["sonoma", "arm64_sonoma"], pf) == ("arm64_sonoma", True)
    assert brew.best_bottle_tag(["sonoma", "arm64_ventura"], pf) == ("arm64_ventura", False)


def test_nearest_tag_is_none_when_the_native_bottle_exists():
    info = brew.BottleInfo("wget", "1.25.0", ["sonoma", "ventura", "arm64_ventura"])
    assert info.nearest_tag(_pf()) is None
    old = brew.BottleInfo("wget", "1.25.0", ["sonoma", "monterey"])
    assert old.nearest_tag(_pf()) == "monterey"
    # an arm64-only formula must not offer an Intel tag (and must not raise)
    arm = brew.BottleInfo("wget", "1.25.0", ["arm64_sonoma"])
    assert arm.nearest_tag(_pf()) is None


def test_parse_oci_tag_separates_the_rebuild():
    assert brew.parse_oci_tag("1.25.0") == ("1.25.0", None)
    assert brew.parse_oci_tag("1.25.0-2") == ("1.25.0", 2)
    assert brew.parse_oci_tag("7.1.2-30-1") == ("7.1.2-30", 1)


def test_version_key_orders_revisions():
    assert brew.version_key("1.2.3_1") == (1, 2, 3, 1)
    assert brew.version_key("1.2.10") > brew.version_key("1.2.9")


# --------------------------------------------------------------------------- #
# candidate cascade
# --------------------------------------------------------------------------- #

def _info(name="wget", version="1.25.0", tags=("sonoma", "ventura"), disabled=False):
    return brew.BottleInfo(name, version, list(tags), disabled=disabled)


def test_bottle_candidates_native_tag_skips_ghcr(monkeypatch):
    monkeypatch.setattr(brew, "bottle_info", lambda n: _info())
    monkeypatch.setattr(brew, "ghcr_tags", lambda *a, **k: pytest.fail("ghcr must not be queried"))
    cands = brew.bottle_candidates("wget", _pf())
    assert cands == [{"oci_tag": "1.25.0", "version": "1.25.0", "tag": "ventura",
                      "rebuild": None}]


def test_bottle_candidates_prefers_a_rebuild_of_the_current_version(monkeypatch):
    monkeypatch.setattr(brew, "bottle_info", lambda n: _info(tags=("monterey",)))
    monkeypatch.setattr(brew, "ghcr_tags", lambda *a, **k: ["1.25.0", "1.25.0-2", "1.24.0"])
    cands = brew.bottle_candidates("wget", _pf())
    assert cands[0]["oci_tag"] == "1.25.0-2"          # same version, fixed build script
    assert cands[0]["tag"] == "monterey"              # best usable tag at the current version
    assert [c["version"] for c in cands[1:]] == ["1.24.0"]


def test_bottle_candidates_walks_back_newest_first_and_can_be_disabled(monkeypatch):
    monkeypatch.setattr(brew, "bottle_info", lambda n: _info(version="3.0.0", tags=()))
    monkeypatch.setattr(brew, "ghcr_tags",
                        lambda *a, **k: ["2.9.0", "2.8.0", "1.0.0", "3.0.0"])
    cands = brew.bottle_candidates("wget", _pf())
    assert [c["version"] for c in cands] == ["3.0.0", "2.9.0", "2.8.0", "1.0.0"]
    assert cands[0]["oci_tag"] == "3.0.0"             # the current version is always tried
    assert cands[0]["rebuild"] is None                # not the internal sort rank (-1)
    monkeypatch.setenv("IBREW_BOTTLE_WALK_BACK", "0")
    assert len(brew.bottle_candidates("wget", _pf())) == 1
    monkeypatch.setenv("IBREW_BOTTLE_WALK_BACK", "99")
    assert brew.walk_back() == 30


def test_bottle_candidates_ignores_disabled_formulas(monkeypatch):
    monkeypatch.setattr(brew, "bottle_info", lambda n: _info(disabled=True))
    assert brew.bottle_candidates("wget", _pf()) == []


def test_brew_bottle_candidate_carries_the_cascade(monkeypatch):
    monkeypatch.setattr(brew, "bottle_info", lambda n: _info(tags=("monterey",)))
    monkeypatch.setattr(brew, "ghcr_tags", lambda *a, **k: ["1.24.0"])
    cand = sources.brew_bottle("wget", _pf())
    assert cand is not None
    assert cand.version == "1.25.0"
    assert cand.meta["tag"] == "monterey"
    assert cand.meta["native"] is False
    assert [c["version"] for c in cand.meta["candidates"]] == ["1.25.0", "1.24.0"]
    assert "older" in cand.notes


def test_brew_bottle_without_any_intel_tag_defers_to_ghcr(monkeypatch):
    # wget 1.25.0 today: arm64 + linux tags only, yet ghcr still holds an amd64 sonoma layer
    monkeypatch.setattr(brew, "bottle_info", lambda n: _info(tags=("arm64_tahoe", "x86_64_linux")))
    monkeypatch.setattr(brew, "ghcr_tags", lambda *a, **k: ["1.25.0", "1.24.0"])
    cand = sources.brew_bottle("wget", _pf())
    assert cand is not None
    assert cand.meta["native"] is False
    assert cand.meta["tag"] == ""
    assert "ghcr.io" in cand.notes


def test_brew_bottle_native_note_starts_with_native(monkeypatch):
    monkeypatch.setattr(brew, "bottle_info", lambda n: _info())
    cand = sources.brew_bottle("wget", _pf())
    assert cand.meta["native"] is True
    assert cand.notes.startswith("native")


def test_planner_step_keeps_native_and_candidates():
    cand = sources.Candidate(source="brew-bottle", url="brew://wget/1.25.0", digest="",
                             version="1.25.0", notes="native",
                             meta={"tag": "ventura", "native": True,
                                                   "candidates": [{"oci_tag": "1.25.0"}]})
    step = planner.candidate_to_step(cand, "wget")
    assert step["native"] is True
    assert step["candidates"] == [{"oci_tag": "1.25.0"}]


# --------------------------------------------------------------------------- #
# ghcr: manifest selection, layer matching, cached tag list
# --------------------------------------------------------------------------- #

def _idx(*entries):
    return {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": list(entries)}


def _entry(digest, arch, osver, ref, os_name="darwin"):
    return {"mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": digest, "size": 512,
            "platform": {"architecture": arch, "os": os_name, "os.version": osver},
            "annotations": {"org.opencontainers.image.ref.name": ref}}


def _man(layer_title, ref, layer="sha256:" + "a" * 64, bottle="b" * 64):
    return {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:" + "c" * 64, "size": 10},
            "layers": [
                {"digest": "sha256:" + "d" * 64, "size": 5,
                 "annotations": {"org.opencontainers.image.title": "wget.rb",
                                 "sh.brew.bottle.install": "1"}},
                {"digest": layer, "size": 1000,
                 "annotations": {"org.opencontainers.image.title": layer_title}},
            ],
            "annotations": {"sh.brew.bottle.digest": bottle,
                            "sh.brew.tab": json.dumps({"arch": "x86_64"}),
                            "org.opencontainers.image.ref.name": ref}}


def _ghcr(monkeypatch, tmp_path, index, manifests):
    from intelbrew import net

    monkeypatch.setenv("INTELBREW_HOME", str(tmp_path / "ib"))
    monkeypatch.setattr(brew, "ghcr_token", lambda repo: "tok")
    asked = []

    def fake_request(url, *, timeout=0, retries=0, headers=None, show_progress=False, label=""):
        asked.append(url)
        key = url.rsplit("/manifests/", 1)[-1]
        body = manifests.get(key)
        if body is None:
            return net.Fetch(False, url, 404, error="not found")
        return net.Fetch(True, url, 200, json.dumps(body).encode())

    monkeypatch.setattr(net, "request", fake_request)
    return asked


def test_ghcr_resolve_picks_the_intel_build_from_a_mixed_index(tmp_path, monkeypatch):
    from intelbrew import installer

    index = _idx(
        _entry("sha256:" + "1" * 64, "amd64", "Ubuntu 22.04", "1.25.0.x86_64_linux",
               os_name="linux"),
        _entry("sha256:" + "2" * 64, "arm64", "macOS 13", "1.25.0.arm64_ventura"),
        _entry("sha256:" + "3" * 64, "amd64", "macOS 14", "1.25.0.sonoma"),
        _entry("sha256:" + "4" * 64, "amd64", "macOS 13", "1.25.0.ventura"),
    )
    _ghcr(monkeypatch, tmp_path, index, {
        "1.25.0": index,
        "sha256:" + "4" * 64: _man("wget--1.25.0.ventura.bottle.tar.gz", "1.25.0.ventura"),
    })
    meta = installer._ghcr_resolve("wget", "1.25.0", _pf(), "ventura")
    assert meta["layer"] == "sha256:" + "a" * 64
    assert meta["version"] == "1.25.0"
    assert meta["tag"] == "ventura"
    assert meta["digest"] == "b" * 64        # from the manifest annotations, not config Labels
    assert json.loads(meta["tab"]) == {"arch": "x86_64"}   # pour metadata for the sidecar


def test_ghcr_resolve_accepts_a_revisioned_layer_name(tmp_path, monkeypatch):
    from intelbrew import installer

    # ghcr writes the ref as `<version>.<tag>.<rebuild>` for a rebuilt bottle
    index = _idx(_entry("sha256:" + "4" * 64, "amd64", "macOS 13", "1.25.0.ventura.2"))
    _ghcr(monkeypatch, tmp_path, index, {
        "1.25.0-2": index,
        "sha256:" + "4" * 64: _man("wget--1.25.0.ventura.bottle.2.tar.gz", "1.25.0.ventura.2"),
    })
    meta = installer._ghcr_resolve("wget", "1.25.0-2", _pf(), "ventura")
    assert meta["rebuild"] == 2
    assert meta["layer"] == "sha256:" + "a" * 64
    assert meta["version"] == "1.25.0"          # not "1.25.0.ventura"
    assert meta["tag"] == "ventura"             # not "2"


def test_split_ref_drops_a_trailing_rebuild_and_keeps_hyphenated_versions():
    from intelbrew import installer

    assert installer._split_ref("1.8.2.sonoma") == ("1.8.2", "sonoma")
    assert installer._split_ref("1.8.2.sonoma.1") == ("1.8.2", "sonoma")
    assert installer._split_ref("7.1.2-30.monterey.3") == ("7.1.2-30", "monterey")
    assert installer._split_ref("1.0.0.arm64_sequoia") == ("1.0.0", "arm64_sequoia")
    assert installer._split_ref("1.2.3") == ("1.2.3", "")


def test_ghcr_resolve_uses_the_newest_runnable_macos_on_a_new_intel_mac(tmp_path, monkeypatch):
    from intelbrew import installer

    pf = util.PlatformInfo("Darwin", "x86_64", "25", "Intel", "macOS 26.0", (26, 0))
    index = _idx(
        _entry("sha256:" + "2" * 64, "arm64", "macOS 26", "9.0.0.arm64_tahoe"),
        _entry("sha256:" + "3" * 64, "amd64", "macOS 14", "9.0.0.sonoma"),
    )
    _ghcr(monkeypatch, tmp_path, index, {
        "9.0.0": index,
        "sha256:" + "3" * 64: _man("x--9.0.0.sonoma.bottle.tar.gz", "9.0.0.sonoma"),
    })
    meta = installer._ghcr_resolve("x", "9.0.0", pf, "tahoe")
    assert meta["tag"] == "sonoma"


def test_ghcr_resolve_rejects_a_bottle_built_for_a_newer_macos(tmp_path, monkeypatch):
    from intelbrew import installer

    index = _idx(_entry("sha256:" + "1" * 64, "amd64", "macOS 15", "1.0.0.sequoia"))
    _ghcr(monkeypatch, tmp_path, index, {"1.0.0": index})
    with pytest.raises(installer._BottleRejected):
        installer._ghcr_resolve("wget", "1.0.0", _pf(), "ventura")


def test_ghcr_resolve_rejects_an_arm64_only_index_on_intel(tmp_path, monkeypatch):
    from intelbrew import installer

    index = _idx(_entry("sha256:" + "1" * 64, "arm64", "macOS 13", "1.0.0.arm64_ventura"))
    _ghcr(monkeypatch, tmp_path, index, {"1.0.0": index})
    with pytest.raises(installer._BottleRejected):
        installer._ghcr_resolve("wget", "1.0.0", _pf(), "ventura")


def test_ghcr_resolve_handles_an_arm64_tag_without_a_keyerror(tmp_path, monkeypatch):
    from intelbrew import installer

    index = _idx(_entry("sha256:" + "1" * 64, "arm64", "macOS 14", "1.0.0.arm64_sonoma"))
    _ghcr(monkeypatch, tmp_path, index, {
        "1.0.0": index,
        "sha256:" + "1" * 64: _man("x--1.0.0.arm64_sonoma.bottle.tar.gz", "1.0.0.arm64_sonoma"),
    })
    meta = installer._ghcr_resolve("x", "1.0.0", _pf("arm64"), "arm64_sonoma")
    assert meta["tag"] == "arm64_sonoma"


def test_ghcr_tags_paginates_with_last_and_is_cached(tmp_path, monkeypatch):
    from intelbrew import net

    monkeypatch.setenv("INTELBREW_HOME", str(tmp_path / "ib"))
    monkeypatch.setattr(brew, "ghcr_token", lambda repo: "tok")
    seen = []

    def fake_request(url, *, timeout=0, retries=0, headers=None, show_progress=False, label=""):
        seen.append(url)
        tags = [f"1.0.{i}" for i in range(1000)] if "last=" not in url else ["0.9.0", "0.8.0"]
        return net.Fetch(True, url, 200, json.dumps({"tags": tags}).encode())

    monkeypatch.setattr(net, "request", fake_request)
    tags = brew.ghcr_tags("wget")
    assert len(tags) == 1002
    assert "last=1.0.999" in seen[1]
    assert len(seen) == 2
    assert brew.ghcr_tags("wget") == tags and len(seen) == 2      # second call hits the cache


# --------------------------------------------------------------------------- #
# pouring a bottle: the file name Homebrew accepts, sidecar, pin
# --------------------------------------------------------------------------- #

def _pour_fixture(tmp_path, monkeypatch, cpu, current="1.26.0", rebuild=None):
    installer, ib = _sandbox(tmp_path, monkeypatch)
    from intelbrew import net

    src = tmp_path / "layer.tar.gz"
    _bin_archive(src, [("wget/1.25.0/bin/wget", _macho(cpu))])
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"architecture": "amd64"}')

    def fake_download(url, dest, *, digest="", algo="sha256", **kw):
        shutil.copyfile(cfg if url.endswith("c" * 64) else src, dest)
        return net.Fetch(True, url)

    monkeypatch.setattr(net, "download", fake_download)
    calls = []

    def fake_run(cmd, *, timeout=0, capture=True, env=None):
        calls.append(list(cmd))
        if "install" in cmd:
            keg = tmp_path / "cellar" / "wget" / "1.25.0" / "bin"
            keg.mkdir(parents=True, exist_ok=True)
            (keg / "wget").write_bytes(_macho(_X86))
        return util.Result(0, "", "")

    monkeypatch.setattr(installer, "run", fake_run)
    monkeypatch.setattr(installer, "_real_brew", lambda: "/fake/bin/brew")
    monkeypatch.setattr(installer.brew, "keg",
                        lambda name, ver: str(tmp_path / "cellar" / name / ver))
    monkeypatch.setattr(installer.brew, "installed_formulae", lambda: {"wget": "1.25.0"})
    monkeypatch.setattr(installer.brew, "bottle_info",
                        lambda name: brew.BottleInfo(name, current, ["sonoma"]))
    meta = {"layer": "sha256:" + "a" * 64, "digest": "b" * 64, "oci_tag": "1.25.0",
            "version": "1.25.0", "rebuild": rebuild, "tag": "ventura",
            "os_version": "macOS 13", "os_major": 13,
            "tab": '{"built_on": {"os": "Macintosh", "os_version": "macOS 14.8"}}'}
    return installer, ib, calls, meta


_X86 = (0x01000007).to_bytes(4, "little")


def test_pour_bottle_uses_the_homebrew_file_name_and_a_sidecar(tmp_path, monkeypatch):
    installer, ib, calls, meta = _pour_fixture(tmp_path, monkeypatch, _CPU_X86_64)
    out = installer.Outcome(formula="wget", step={})
    installer._pour_bottle({}, "wget", _pf(), out, meta)

    cache = ib / "cache" / "bottles"
    final = cache / "wget--1.25.0.ventura.bottle.tar.gz"
    assert final.exists()                                  # no ".incomplete" suffix
    # `brew`'s Utils::Bottles.load_tab reads `<name minus .tar.gz>.json`
    side = json.loads((cache / "wget--1.25.0.ventura.bottle.json").read_text())
    assert side["wget"]["bottle"]["tags"]["ventura"]["tab"]["built_on"]["os"] == "Macintosh"
    assert calls[0][1:] == ["install", str(final)]
    assert out.installed and out.bins == ["wget"]


def test_pour_bottle_without_tab_writes_no_sidecar(tmp_path, monkeypatch):
    installer, ib, _calls, meta = _pour_fixture(tmp_path, monkeypatch, _CPU_X86_64)
    meta["tab"] = ""
    installer._pour_bottle({}, "wget", _pf(), installer.Outcome(formula="wget", step={}), meta)
    assert (ib / "cache" / "bottles" / "wget--1.25.0.ventura.bottle.tar.gz").exists()
    assert not (ib / "cache" / "bottles" / "wget--1.25.0.ventura.bottle.json").exists()


def test_pour_bottle_rebuild_gets_the_revision_part_of_the_name(tmp_path, monkeypatch):
    installer, ib, _calls, meta = _pour_fixture(
        tmp_path, monkeypatch, _CPU_X86_64, rebuild=2, current="1.25.0")
    installer._pour_bottle({}, "wget", _pf(), installer.Outcome(formula="wget", step={}), meta)
    assert (ib / "cache" / "bottles" / "wget--1.25.0.ventura.bottle.2.tar.gz").exists()


def test_pour_bottle_pins_an_older_release(tmp_path, monkeypatch):
    installer, _ib, calls, meta = _pour_fixture(tmp_path, monkeypatch, _CPU_X86_64)
    out = installer.Outcome(formula="wget", step={})
    installer._pour_bottle({}, "wget", _pf(), out, meta)
    assert ["/fake/bin/brew", "pin", "wget"] in calls
    assert "pinned" in out.report


def test_pour_bottle_rejects_a_wrong_arch_tarball_before_brew(tmp_path, monkeypatch):
    installer, _ib, calls, meta = _pour_fixture(tmp_path, monkeypatch, _CPU_ARM64)
    with pytest.raises(installer._BottleRejected, match="arm64"):
        installer._pour_bottle({}, "wget", _pf(), installer.Outcome(formula="wget", step={}), meta)
    assert calls == []                                     # brew never saw it


def test_a_broken_brew_prefix_stops_the_candidate_walk(tmp_path, monkeypatch):
    installer, _ib, calls, meta = _pour_fixture(tmp_path, monkeypatch, _CPU_X86_64)
    monkeypatch.setattr(installer, "_ghcr_resolve", lambda *a, **k: dict(meta))

    def failing_run(cmd, *, timeout=0, capture=True, env=None):
        calls.append(list(cmd))
        if "ruby" in cmd:                                  # the prefix probe
            return util.Result(0, "/usr/local/share/man/man8\n", "")
        return util.Result(1, "", "")                       # brew install refused

    monkeypatch.setattr(installer, "run", failing_run)
    step = {"kind": "bottle", "name": "wget", "tag": "ventura", "version": "1.25.0",
            "candidates": [{"oci_tag": "1.25.0", "tag": "ventura"},
                           {"oci_tag": "1.24.0", "tag": "ventura"}]}
    out = installer.install_bottle(step, "wget", _pf())
    assert not out.installed
    assert "not writable by" in out.error and "sudo chown" in out.error
    # a prefix Homebrew refuses to write is not this bottle's fault: do not walk back
    assert len([c for c in calls if c[1:2] == ["install"]]) == 1


def test_install_bottle_falls_back_to_the_next_candidate(tmp_path, monkeypatch):
    installer, _ib = _sandbox(tmp_path, monkeypatch)
    tried = []

    def fake_resolve(formula, oci, pf, tag=""):
        tried.append(oci)
        if oci != "1.24.0":
            raise installer._BottleRejected(f"{oci} is gone")
        return {"layer": "sha256:" + "a" * 64, "digest": "b",
                "oci_tag": oci, "version": "1.24.0", "rebuild": None, "tag": "ventura",
                "os_version": "macOS 13", "os_major": 13, "tab": ""}

    monkeypatch.setattr(installer, "_ghcr_resolve", fake_resolve)
    monkeypatch.setattr(installer.shutil, "which", lambda n: "/usr/bin/brew")

    def fake_pour(step, formula, pf, out, meta):
        out.installed = True
        out.report = f"poured {meta['version']}"
        return out

    monkeypatch.setattr(installer, "_pour_bottle", fake_pour)
    step = {"kind": "bottle", "name": "wget", "tag": "ventura", "version": "1.25.0",
            "candidates": [{"oci_tag": "1.25.0", "tag": "ventura"},
                           {"oci_tag": "1.24.0", "tag": ""}]}
    out = installer.install_bottle(step, "wget", _pf())
    assert out.installed and out.report == "poured 1.24.0"
    assert tried == ["1.25.0", "1.24.0"]


def test_install_bottle_reports_every_failed_candidate(tmp_path, monkeypatch):
    installer, _ib = _sandbox(tmp_path, monkeypatch)

    def fake_resolve(formula, oci, pf, tag=""):
        raise installer._BottleRejected(f"{oci}: nope")

    monkeypatch.setattr(installer, "_ghcr_resolve", fake_resolve)
    monkeypatch.setattr(installer.shutil, "which", lambda n: "/usr/bin/brew")
    step = {"kind": "bottle", "name": "wget", "candidates": [{"oci_tag": "2.0"}, {"oci_tag": "1.0"}]}
    out = installer.install_bottle(step, "wget", _pf())
    assert not out.installed
    assert "2.0: nope" in out.error and "1.0: nope" in out.error


def test_install_bottle_native_step_goes_through_real_brew(tmp_path, monkeypatch):
    installer, _ib = _sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(installer.shutil, "which", lambda n: "/usr/bin/brew")
    monkeypatch.setattr(installer, "_real_brew", lambda: "/fake/bin/brew")
    monkeypatch.setattr(installer, "_installed_version", lambda n: "1.25.0")
    monkeypatch.setattr(installer.brew, "keg",
                        lambda name, ver: str(tmp_path / "cellar" / name / ver))
    calls = []
    monkeypatch.setattr(installer, "run",
                        lambda cmd, **kw: (calls.append(list(cmd)), util.Result(0, "", ""))[1])
    out = installer.install_bottle({"kind": "bottle", "native": True}, "wget", _pf())
    assert out.installed
    assert calls[0] == ["/fake/bin/brew", "install", "wget"]
    assert "native" in out.report


# --------------------------------------------------------------------------- #
# census tiers
# --------------------------------------------------------------------------- #

def test_census_counts_native_older_maybe_and_compile(monkeypatch):
    pf = _pf()
    monkeypatch.setattr(brew, "formula_index", lambda fetch=True: [
        {"name": "native-one", "bottle": {"stable": {"files": {"ventura": "a"}}}},
        {"name": "older-one", "bottle": {"stable": {"files": {"monterey": "b"}}}},
        {"name": "arm-only", "bottle": {"stable": {"files": {"arm64_ventura": "c"}}}},
        {"name": "linux-only", "bottle": {"stable": {"files": {"x86_64_linux": "e"}}}},
        {"name": "no-bottle", "bottle": {"stable": {"files": {}}}},
        {"name": "gone", "bottle": {"stable": {"files": {"ventura": "d"}}}, "disabled": True},
    ])
    c = brew.census(pf)
    assert c.binary == ["native-one"]
    assert c.older == ["older-one"]
    assert c.maybe == ["arm-only"]          # a macOS bottle exists, just not for this Intel tag
    assert c.compile == ["linux-only", "no-bottle"]
    assert c.disabled == ["gone"]
    assert c.total == 6
    assert round(c.usable_pct, 0) == 33     # maybe is not counted as usable
