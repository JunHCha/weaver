#!/usr/bin/env bash
# Freeze the Weaver server (PyInstaller onedir, no per-launch unpacking) into
# src-tauri/server, bundled as a Tauri resource
# and build the static web UI it serves. Run from anywhere.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(cd "$here/.." && pwd)"
work="$here/build/pyinstaller"

pnpm --dir "$repo/web" install --frozen-lockfile
pnpm --dir "$repo/web" build

uv sync --project "$repo/server" --locked
mkdir -p "$work"
uv run --project "$repo/server" --python 3.12 --with 'pyinstaller==6.21.0' \
  pyinstaller --noconfirm --clean --onedir \
  --name weaver-server \
  --collect-submodules weaver \
  --workpath "$work/work" --specpath "$work" --distpath "$work/dist" \
  "$here/scripts/sidecar_entry.py"

rm -rf "$here/src-tauri/server"
cp -R "$work/dist/weaver-server" "$here/src-tauri/server"
echo "sidecar: $here/src-tauri/server/weaver-server"
du -sh "$here/src-tauri/server"
