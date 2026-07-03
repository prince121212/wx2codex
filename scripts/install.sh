#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${WX2CODEX_HOME:-$HOME/.wx2codex}"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$INSTALL_DIR" "$BIN_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install "$ROOT_DIR"
ln -sf "$VENV_DIR/bin/wx2codex" "$BIN_DIR/wx2codex"

cat <<MSG
wx2codex 已安装。

请确保 PATH 包含：$BIN_DIR
然后运行：
  wx2codex configure --cloud-url https://codex.292828.xyz --codex-provider desktop
  wx2codex codex doctor
  wx2codex codex cwd /path/to/project
  wx2codex connect
  wx2codex run

如果需要后台运行：
  wx2codex install-service
MSG
