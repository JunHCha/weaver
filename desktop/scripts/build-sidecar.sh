#!/usr/bin/env bash
# Freeze the Weaver server into a single executable for Tauri's externalBin
# and build the static web UI it serves. Run from anywhere.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(cd "$here/.." && pwd)"
triple="$(rustc -vV | sed -n 's/^host: //p')"
work="$here/build/pyinstaller"

pnpm --dir "$repo/web" install --frozen-lockfile
pnpm --dir "$repo/web" build

uv sync --project "$repo/server" --locked
mkdir -p "$work" "$here/src-tauri/binaries"
uv run --project "$repo/server" --python 3.12 --with 'pyinstaller==6.21.0' \
  pyinstaller --noconfirm --clean --onefile \
  --name weaver-server \
  --collect-submodules weaver \
  --workpath "$work/work" --specpath "$work" --distpath "$work/dist" \
  "$here/scripts/sidecar_entry.py"

cp "$work/dist/weaver-server" "$here/src-tauri/binaries/weaver-server-$triple"
echo "sidecar: $here/src-tauri/binaries/weaver-server-$triple"
du -h "$here/src-tauri/binaries/weaver-server-$triple"
