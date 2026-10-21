#!/bin/sh
# intelbrew bootstrap installer
#   ./install.sh [--prefix DIR] [--python PATH] [--no-path] [--uninstall]
set -eu

PREFIX="${HOME}/.local"
PYTHON=""
ADD_PATH=1
DO_UNINSTALL=0
LINK_DIR=""
REPO="${INTELBREW_REPO:-https://github.com/Adminote/ventura-x86-recipe}"
BRANCH="${INTELBREW_BRANCH:-main}"

usage() {
  cat <<'USAGE'
usage: install.sh [--prefix DIR] [--python PATH] [--no-path] [--link-dir DIR] [--uninstall]

  --prefix DIR    install into DIR (default: ~/.local)
  --python PATH   python interpreter to embed in the launcher (default: first python3 with 3.9+)
  --no-path       do not touch your shell profile
  --link-dir DIR  also symlink ibrew into DIR (e.g. /usr/local/bin, must be on PATH)
  --uninstall     remove the launcher and the intelbrew block from the shell profile
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --no-path) ADD_PATH=0; shift ;;
    --link-dir) LINK_DIR="$2"; shift 2 ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

BIN="$PREFIX/bin"
LIB="$PREFIX/lib/intelbrew"
PROFILE_MARK="# >>> intelbrew >>>"

# The file the *interactive* shell reads. zsh users get ~/.zshrc (always read),
# even though Homebrew-style tools usually write ~/.zprofile (login shells only).
profile_file() {
  case "$(basename "${SHELL:-/bin/zsh}")" in
    zsh)
      if [ -f "${HOME}/.zshrc" ]; then echo "${HOME}/.zshrc"
      elif [ -f "${HOME}/.zprofile" ]; then echo "${HOME}/.zprofile"
      else echo "${HOME}/.zshrc"; fi ;;
    bash)
      if [ -f "${HOME}/.bash_profile" ]; then echo "${HOME}/.bash_profile"
      elif [ -f "${HOME}/.bashrc" ]; then echo "${HOME}/.bashrc"
      else echo "${HOME}/.profile"; fi ;;
    *) echo "${HOME}/.profile" ;;
  esac
}

drop_profile_block() {
  p="$(profile_file)"
  [ -f "$p" ] || return 0
  awk -v start="$PROFILE_MARK" -v end="# <<< intelbrew <<<" '
    $0 == start {skip=1; next}
    skip && ($0 == end || $0 ~ start) {skip=0; next}
    !skip {print}
  ' "$p" > "$p.intelbrew.$$" && mv "$p.intelbrew.$$" "$p"
}

if [ "$DO_UNINSTALL" = 1 ]; then
  rm -f "$BIN/ibrew"
  rm -rf "$LIB"
  [ -n "$LINK_DIR" ] && rm -f "$LINK_DIR/ibrew"
  drop_profile_block
  echo "intelbrew removed. Your data in ~/.intelbrew (and ~/.local/bin/node, npm, ...) was left in place."
  exit 0
fi

# --- locate a usable python (3.9+, must have ssl) ---------------------------
pick_python() {
  [ -n "$PYTHON" ] && { echo "$PYTHON"; return; }
  for c in /usr/bin/python3 "$(command -v python3 || true)" /opt/homebrew/bin/python3; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c 'import sys,ssl; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
      echo "$c"; return
    fi
  done
  echo ""
}

PY="$(pick_python)"
if [ -z "$PY" ]; then
  echo "error: need python3 >= 3.9 with ssl support (xcode-select --install)" >&2
  exit 1
fi
echo "==> python: $PY"

# --- fetch sources ----------------------------------------------------------
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -d "$SRC_DIR/src/intelbrew" ]; then
  tmp="$(mktemp -d)"
  echo "==> fetching $REPO"
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$tmp/intelbrew" >/dev/null 2>&1 || {
      curl -fsSL "$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar xz -C "$tmp" || {
        echo "error: cannot download sources from $REPO ($BRANCH)" >&2; exit 1; }
    }
  else
    curl -fsSL "$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar xz -C "$tmp"
  fi
  SRC_DIR="$(find "$tmp" -maxdepth 2 -name src -type d | head -1 | xargs dirname)"
fi
echo "==> sources: $SRC_DIR"

# --- install ----------------------------------------------------------------
mkdir -p "$BIN" "$LIB"
rm -rf "$LIB/intelbrew"
cp -R "$SRC_DIR/src/intelbrew" "$LIB/intelbrew"
[ -d "$SRC_DIR/registry" ] && cp -R "$SRC_DIR/registry" "$LIB/registry"
cp "$SRC_DIR/README.md" "$LIB/" 2>/dev/null || true

cat > "$BIN/ibrew" <<LAUNCHER
#!$PY
import os, sys
_here = os.path.dirname(os.path.realpath(__file__))
_lib = os.path.join(os.path.dirname(_here), "lib", "intelbrew")
sys.path.insert(0, _lib)
from intelbrew.cli import main
sys.exit(main())
LAUNCHER
chmod 755 "$BIN/ibrew"
echo "==> launcher: $BIN/ibrew"

# --- PATH -------------------------------------------------------------------
if [ -n "$LINK_DIR" ]; then
  mkdir -p "$LINK_DIR" && ln -sf "$BIN/ibrew" "$LINK_DIR/ibrew" \
    && echo "==> linked $LINK_DIR/ibrew"
fi

if [ "$ADD_PATH" = 1 ]; then
  p="$(profile_file)"
  drop_profile_block
  {
    echo ""
    echo "$PROFILE_MARK"
    echo "# intelbrew binaries first, then the brew shim, then Homebrew."
    echo "# Remove this block (or run install.sh --uninstall) to undo."
    echo "export PATH=\"$BIN:\$HOME/.intelbrew/brew-bin:\$PATH\""
    echo "# <<< intelbrew <<<"
  } >> "$p"
  echo "==> PATH block written to $p"
fi

# Prove whether the command is usable *right now* rather than claiming success.
if command -v ibrew >/dev/null 2>&1; then
  echo "==> ibrew is already on PATH: $(command -v ibrew)"
elif [ "$ADD_PATH" = 1 ]; then
  echo ""
  echo "!! ibrew is NOT on PATH in this shell yet. Activate it with:"
  echo "     source $(profile_file)"
  echo "   or open a new terminal."
else
  echo ""
  echo "!! --no-path: add $BIN to PATH yourself, e.g."
  echo "     export PATH=\"$BIN:\$PATH\""
fi

echo ""
echo "next:"
echo "  $BIN/ibrew doctor          # verify this machine"
echo "  $BIN/ibrew setup --apply   # node/npm/npx shims + brew shim + PATH"
