"""Offline unit tests — no network, no installs to the real prefix.

Run: PYTHONPATH=src python3 -m pytest tests/ -q
"""

from __future__ import annotations

import io
import json
import os
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


def why_verdict(data):
    from intelbrew import why

    return why.verdict(data)
