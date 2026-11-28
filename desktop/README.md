# Weaver desktop (spike)

Minimal Tauri v2 shell for macOS. It bundles the Weaver server, frozen with
PyInstaller into one executable, as a Tauri sidecar and starts it twice:

- `weaver-server run --no-console`: the resident runtime (exits with
  `EXIT_ALREADY_RUNNING` when the launchd agent already holds the state lock;
  the UI keeps working).
- `weaver-server web --port 8765 --static-dir <Resources>/web`: the loopback UI,
  serving the bundled `web/out`.

The window shows `loading/index.html` until port 8765 answers, then navigates
to `http://127.0.0.1:8765/`. The server reads the usual
`~/.config/weaver/config.toml` (or `WEAVER_CONFIG`). On quit the shell sends
SIGTERM so the onefile bootloader forwards it to Python.

## Build

Requires Rust (stable), Node 22, pnpm 10.4 and uv. From the repository root:

```sh
pnpm --dir desktop install --frozen-lockfile
pnpm --dir desktop build          # sidecar + web/out + tauri build
```

Output: `desktop/src-tauri/target/release/bundle/macos/Weaver.app` and `dmg/`.
Unsigned by default (`signingIdentity: null`); set `APPLE_SIGNING_IDENTITY`
and the notarization variables to sign.
