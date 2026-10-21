"""Plan resolution: from a user request to an ordered list of install steps.

The plan is *pure*: it never downloads or installs, so ``ibrew plan`` is a dry
run and ``ibrew install`` shares the exact same code path.

Order (first hit wins):
    registry recipe  →  brew bottle for this platform  →  upstream shortcut
    →  pypi / conda  →  npm  →  guessed GitHub repo (heuristic, needs review)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import brew, net, registry, sources
from .util import PlatformInfo, warn


@dataclass
class Resolved:
    formula: str
    how: str                 # registry | brew-bottle | node-dist | github-release | ...
    version: str = ""
    contract: str = "none"   # same-contract | shortcut | npm | none
    confidence: str = "none"  # verified | probable | heuristic | none
    url: str = ""
    detail: str = ""
    steps: list[dict] = field(default_factory=list)
    blocked: str = ""

    @property
    def ok(self) -> bool:
        return not self.blocked and bool(self.steps)

    def line(self) -> str:
        if self.blocked:
            return f"{self.formula:22} ✗  {self.blocked}"
        return f"{self.formula:22} → {self.how:16} {self.version:16} [{self.contract}, {self.confidence}]"


# --------------------------------------------------------------------------- #

def parse_request(token: str) -> tuple[str, str]:
    """``node@22`` → ("node", "22"); ``brew-node`` → ("node", "")."""
    tok = token.strip()
    if tok.startswith("brew-"):
        tok = tok[5:]
    if "@" in tok:
        name, ver = tok.split("@", 1)
        return name, ver
    return tok, ""


def native_bottle_exists(name: str, pf: PlatformInfo) -> bool:
    info = brew.bottle_info(name)
    return bool(info and info.has(brew.tag_for(pf)))


def candidate_to_step(cand: sources.Candidate, formula: str) -> dict:
    if cand.source == "brew-bottle":
        return {"kind": "bottle", "name": formula, "tag": cand.meta.get("tag", ""),
                "version": cand.version, "source": "brew-bottle"}
    if cand.source == "npm-global":
        return {"kind": "npm-global", "package": cand.meta.get("package", formula),
                "version": cand.version, "source": "npm-global"}
    return {
        "kind": "archive",
        "source": cand.source,
        "url": cand.url,
        "fallback_urls": cand.fallbacks,
        "sha256": cand.digest,
        "fmt": cand.fmt,
        "filename": cand.meta.get("asset") or "",
        "bin": cand.meta.get("bin") or [],
        "provenance": cand.provenance,
        "version": cand.version,
        "gpg": cand.meta.get("gpg"),
        "codesign_team": cand.meta.get("codesign_team"),
        "strip": 1 if cand.source != "conda-forge" else 0,
    }


# --------------------------------------------------------------------------- #
# upstream shortcut chain
# --------------------------------------------------------------------------- #

def _try(fn, *args, **kwargs) -> sources.Candidate | None:
    try:
        return fn(*args, **kwargs)
    except net.RateLimited:
        raise
    except Exception as e:  # a source going away must not break the plan
        warn(f"{fn.__name__}: {e}")
        return None


def upstream(tool: str, pf: PlatformInfo, name: str, version: str = "") -> sources.Candidate | None:
    ver = version or ""
    if tool == "node":
        return _try(sources.node_dist, ver or "24", pf, "lts")
    if tool == "python":
        return _try(sources.github_release, "python", pf, ver or "")
    if tool == "rust":
        return _try(sources.github_release, "rustup", pf)
    if tool == "jdk":
        return _try(sources.adoptium_jdk, ver or "21", pf)
    if tool in sources.GITHUB_REPOS:
        return _try(sources.github_release, tool, pf, ver or "")
    if tool in PYPI_TOOLS:
        return (_try(sources.pypi_tool, PYPI_TOOLS[tool], pf)
                or _try(sources.github_release, tool, pf, ver or ""))
    if name in NPM_TOOLS:
        return _try(sources.npm_package, name, pf, ver or "")
    return None


# Only these are known *native npm* tools. Arbitrary brew names are never
# mapped to npm: npm has squatted placeholders ("0.0.1-security") that would
# silently install the wrong thing.
NPM_TOOLS = {
    "typescript", "ts-node", "tsx", "pnpm", "yarn", "eslint", "prettier", "serve",
    "vercel", "netlify-cli", "nodemon", "pm2", "http-server", "npm-check-updates",
    "corepack", "nx", "turbo", "webpack", "vite", "parcel", "rollup", "esbuild",
    "@anthropic-ai/claude-code", "@openai/codex", "wrangler", "firebase-tools",
    "aws-cdk", "cdktf-cli", "serverless", "newman", "localtunnel", "ngrok",
}

PYPI_TOOLS = {
    "pipx": "pipx", "poetry": "poetry", "black": "black", "pre-commit": "pre-commit",
    "pytest": "pytest", "mypy": "mypy", "yt-dlp": "yt-dlp", "youtube-dl": "youtube-dl",
}

CONDA_TOOLS = {
    "ffmpeg": "ffmpeg", "imagemagick": "imagemagick", "redis": "redis", "zstd": "zstd",
    "sqlite": "sqlite", "graphviz": "graphviz", "pandoc": "pandoc", "protobuf": "protobuf",
    "libvips": "libvips", "nasm": "nasm", "libmagic": "file",
}

# Well-known single-binary formulas and their (guessed) upstream repos. Only used
# when no recipe exists; results are marked "heuristic" and require review.
GUESS_REPOS = {
    "fzf": "junegunn/fzf", "ripgrep": "BurntSushi/ripgrep", "fd": "sharkdp/fd",
    "bat": "sharkdp/bat", "jq": "jqlang/jq", "yq": "mikefarah/yq", "gh": "cli/cli",
    "lazygit": "jesseduffield/lazygit", "dive": "wagoodman/dive", "hdparm": "",
    "delta": "dandavison/delta", "zoxide": "ajeetdsouza/zoxide", "eza": "eza-community/eza",
    "dust": "bootandy/dust", "procs": "dalance/procs", "bottom": "ClementTsang/bottom",
    "hyperfine": "sharkdp/hyperfine", "tokei": "XAMPPRocky/tokei", "sd": "chmln/sd",
    "xh": "ducaale/xh", "wrk": "wg/wrk", "hey": "rakyll/hey", "k9s": "derailed/k9s",
    "helm": "helm/helm", "kubectl": "kubernetes/kubernetes", "kind": "kubernetes-sigs/kind",
    "minikube": "kubernetes/minikube", "terraform": "hashicorp/terraform",
    "packer": "hashicorp/packer", "vault": "hashicorp/vault", "consul": "hashicorp/consul",
    "nomad": "hashicorp/nomad", "deno": "denoland/deno", "bun": "oven-sh/bun",
    "zig": "zigtools/zpm", "just": "casey/just", "zellij": "zellij-org/zellij",
    "starship": "starship/starship", "atuin": "atuinsh/atuin", "mise": "jdx/mise",
    "chezmoi": "twpayne/chezmoi", "gitleaks": "gitleaks/gitleaks", "trivy": "aquasecurity/trivy",
    "cosign": "sigstore/cosign", "syft": "anchore/syft", "grype": "anchore/grype",
    "hugo": "gohugoio/hugo", "helmfile": "helmfile/helmfile", "talosctl": "siderolabs/talos",
    "age": "FiloSottile/age", "wget": "rockdaboot/wget2", "curl": "curl/curl",
    "nmap": "nmap/nmap", "pv": "a-j-wood/pv", "direnv": "direnv/direnv",
    "mkcert": "FiloSottile/mkcert", "restic": "restic/restic", "rclone": "rclone/rclone",
    "yt-dlp": "yt-dlp/yt-dlp", "glab": "profclems/glab", "act": "nektos/act",
}


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #

def resolve(name: str, pf: PlatformInfo, version: str = "") -> Resolved:
    # 0) already a native binary on this platform: nothing to do
    if native_bottle_exists(name, pf):
        c = brew.brew_bottle(name, pf)
        if c and c.notes.startswith("native"):
            return Resolved(name, "brew-bottle", c.version, "same-contract", "verified",
                            url=c.url, detail="native bottle for this platform exists; "
                            "`brew install` works as-is", steps=[candidate_to_step(c, name)])

    # 1) curated registry
    recipe = registry.load(name)
    if recipe:
        blocked = recipe.check_requirements(pf)
        if blocked:
            return Resolved(name, "registry", recipe.version, "none", "verified", blocked=blocked)
        steps = [dict(st, version=st.get("version") or recipe.version) for st in recipe.steps]
        return Resolved(name, "registry", recipe.version, "same-contract", "verified",
                        url=(steps[0].get("url") or "") if steps else "",
                        detail="curated recipe validated on this macOS family",
                        steps=steps)

    tool = sources.SHORTCUT_MAP.get(name)

    # 2) bottle of this formula built for an older macOS (brew path, probable)
    c = brew.brew_bottle(name, pf)
    if c:
        return Resolved(name, "brew-bottle", c.version, "same-contract", "probable",
                        url=c.url, detail=c.notes, steps=[candidate_to_step(c, name)])

    # 3) upstream shortcut / package ecosystems
    try:
        cand = None
        if tool:
            cand = upstream(tool, pf, name, version)
        elif name in PYPI_TOOLS:
            cand = _try(sources.pypi_tool, PYPI_TOOLS[name], pf)
        elif name in CONDA_TOOLS:
            cand = _try(sources.conda_pkg, CONDA_TOOLS[name], pf)
        if not cand and name in sources.GITHUB_REPOS:
            cand = _try(sources.github_release, name, pf, version)
        if not cand and name in NPM_TOOLS:
            cand = _try(sources.npm_package, name, pf, version)
    except ValueError as e:          # documented requirement not met
        return Resolved(name, "upstream", "", "none", "verified", blocked=str(e))
    except net.RateLimited:
        return Resolved(name, "upstream", "", "none", "none",
                        blocked="GitHub API rate limited: export GITHUB_TOKEN, or use a recipe")
    if cand:
        contract = "npm" if cand.source == "npm-global" else "same-contract"
        detail = cand.notes
        if contract == "npm":
            detail = "npm package (contract = the npm/Javascript CLI, not the brew wrapper)"
        return Resolved(name, cand.source, cand.version, contract, "verified",
                        url=cand.url, detail=detail, steps=[candidate_to_step(cand, name)])

    # 4) heuristic GitHub guess
    repo = GUESS_REPOS.get(name)
    if repo:
        cand = _try(sources.github_release_repo, repo, pf, version)
        if cand:
            return Resolved(name, "github-release", cand.version, "same-contract", "heuristic",
                            url=cand.url,
                            detail=f"guessed upstream repo {repo}; review with `ibrew plan 1`",
                            steps=[candidate_to_step(cand, name)])

    return Resolved(name, "none", "", "none", "none",
                    blocked="no binary source known; see docs/source-playbook.md or "
                            "`ibrew add <cmd> <url> --sha256 …`")


def resolve_many(names: list[str], pf: PlatformInfo) -> list[Resolved]:
    out = []
    for tok in names:
        n, v = parse_request(tok)
        out.append(resolve(n, pf, v))
    return out
