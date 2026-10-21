#!/bin/sh
# intelbrew bootstrap installer
#   ./install.sh [--prefix DIR] [--python PATH] [--no-path] [--uninstall]
set -eu

PREFIX="${HOME}/.local"
PYTHON=""
ADD_PATH=1
DO_UNINSTALL=0
REPO="${INTELBREW_REPO:-https://github.com/intelbrew/intelbrew}"

usage() {
  cat <<'USAGE'
usage: install.sh [--prefix DIR] [--python PATH] [--no-path] [--uninstall]

  --prefix DIR    install into DIR (default: ~/.local)
  --python PATH   python interpreter to embed in the launcher (default: first python3 with 3.9+)
  --no-path       do not add DIR/bin to your shell profile
  --uninstall     remove the launcher and the intelbrew block from the shell profile
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --no-path) ADD_PATH=0; shift ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

BIN="$PREFIX/bin"
LIB="$PREFIX/lib/intelbrew"
PROFILE_MARK="# >>> intelbrew >>>"

profile_file() {
  case "${SHELL:-/bin/zsh}" in
    *zsh) echo "${HOME}/.zprofile" ;;
    *bash) [ -f "${HOME}/.bash_profile" ] && echo "${HOME}/.bash_profile" || echo "${HOME}/.profile" ;;
    *) echo "${HOME}/.profile" ;;
  esac
}

drop_profile_block() {
  p="$(profile_file)"
  [ -f "$p" ] || return 0
  awk -v m="$PROFILE_MARK" '
    $0 ~ m {skip=1}
    skip && $0 ~ m" <<<" {skip=0; next}
    !skip {print}
  ' "$p" > "$p.intelbrew.$$" && mv "$p.intelbrew.$$" "$p"
}

if [ "$DO_UNINSTALL" = 1 ]; then
  rm -f "$BIN/ibrew"
  rm -rf "$LIB"
  drop_profile_block
  echo "intelbrew removed. Your data in ~/.intelbrew was left in place; delete it to start over."
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
    git clone --depth 1 "$REPO" "$tmp/intelbrew" >/dev/null 2>&1 || {
      curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" | tar xz -C "$tmp" || {
        echo "error: cannot download sources" >&2; exit 1; }
    }
  else
    curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" | tar xz -C "$tmp"
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
if [ "$ADD_PATH" = 1 ]; then
  p="$(profile_file)"
  if ! grep -q "$PROFILE_MARK" "$p" 2>/dev/null; then
    {
      echo ""
      echo "$PROFILE_MARK"
      echo "# intelbrew-managed binaries first, then the brew shim, then Homebrew"
      echo "export PATH=\"$BIN:\$HOME/.intelbrew/brew-bin:\$PATH\""
      echo "$PROFILE_MARK <<<"
    } >> "$p"
    echo "==> added $BIN to PATH in $p (open a new shell)"
  else
    echo "==> PATH already configured in $p"
  fi
fi

echo ""
echo "next:"
echo "  $BIN/ibrew doctor          # verify this machine"
echo "  $BIN/ibrew setup --apply   # node/npm/npx + brew shim"
