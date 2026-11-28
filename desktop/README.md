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
Ad-hoc signed by default (`signingIdentity: "-"`, so the dmg holds a valid bundle signature); set `APPLE_SIGNING_IDENTITY`
and the notarization variables to sign.

## First run

If no configuration exists (`WEAVER_CONFIG`, `$XDG_CONFIG_HOME/weaver/config.toml`,
`~/.config/weaver/config.toml`), the shell writes `config.toml.partial` with the
vault at `~/Documents/Weaver Vault` and other data under
`~/Library/Application Support/Weaver`, lets the server validate it with
`run --config <partial> --init-directories --json`, and renames it into place
only on success. An existing config is never rewritten. The Anthropic API key
is read from the login Keychain (service `dev.junhcha.weaver`, account
`anthropic`) and passed to the children as `ANTHROPIC_API_KEY`:

```sh
security add-generic-password -s dev.junhcha.weaver -a anthropic -w
```
