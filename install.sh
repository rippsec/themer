#!/usr/bin/env bash
# Install themer for the current user (no root): ~/.local/bin/themer + an app-menu entry.
#   ./install.sh              install (symlinks back to this checkout)
#   ./install.sh --uninstall  remove
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin/themer"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/themer.desktop"

if [[ ${1:-} == --uninstall ]]; then
  rm -f "$BIN" "$DESKTOP"
  echo "themer removed (your ~/.config/themer hooks were left alone)"
  exit 0
fi

python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null \
  || echo "warning: PyGObject / GTK 3 not found (Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0)" >&2
command -v xsettingsd >/dev/null \
  || echo "note: xsettingsd not found; running apps only change theme after a restart (sudo apt install xsettingsd)" >&2

chmod +x "$HERE/themer.py"
mkdir -p "$(dirname "$BIN")" "$(dirname "$DESKTOP")"
ln -sfn "$HERE/themer.py" "$BIN"
cp "$HERE/themer.desktop" "$DESKTOP"
echo "installed: run 'themer' (make sure ~/.local/bin is in your PATH)"
