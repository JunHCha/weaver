// Minimal Tauri shell for Weaver: starts the frozen Python server twice as a
// child process (`run` daemon and loopback `web` UI) and points the window at it.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, Url};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const WEB_PORT: u16 = 8765;

#[derive(Default)]
struct Sidecars(Mutex<Vec<CommandChild>>);

const KEYCHAIN_SERVICE: &str = "dev.junhcha.weaver";

fn home() -> Result<PathBuf, String> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| "HOME is not set".to_string())
}

/// Same selection order as the server: WEAVER_CONFIG, then XDG_CONFIG_HOME,
/// then ~/.config/weaver/config.toml.
fn config_path() -> Result<PathBuf, String> {
    if let Some(p) = std::env::var_os("WEAVER_CONFIG").filter(|p| !p.is_empty()) {
        return Ok(PathBuf::from(p));
    }
    let base = match std::env::var_os("XDG_CONFIG_HOME").filter(|p| !p.is_empty()) {
        Some(x) => PathBuf::from(x),
        None => home()?.join(".config"),
    };
    Ok(base.join("weaver").join("config.toml"))
}

fn toml_str(path: &Path) -> String {
    let raw = path
        .to_string_lossy()
        .replace('\\', "\\\\")
        .replace('"', "\\\"");
    format!("\"{raw}\"")
}

fn default_config(home: &Path) -> String {
    let data = home.join("Library/Application Support/Weaver");
    format!(
        "# Written by Weaver Desktop on first run. Edit freely; it is never overwritten.\n\
         sync_root = {}\narchive_root = {}\nvault_root = {}\nstate_root = {}\nlog_root = {}\n\n\
         [llm]\nbackend = \"anthropic\"\napi_key_env = \"ANTHROPIC_API_KEY\"\n",
        toml_str(&data.join("sync")),
        toml_str(&data.join("archive")),
        toml_str(&home.join("Documents/Weaver Vault")),
        toml_str(&data.join("state")),
        toml_str(&data.join("logs")),
    )
}

/// Run the frozen server once and return its stdout (JSON from --json).
fn server_once(exe: &Path, args: &[&str], api_key: Option<&str>) -> Result<(bool, String), String> {
    let mut cmd = Command::new(exe);
    cmd.args(args);
    if let Some(key) = api_key {
        cmd.env("ANTHROPIC_API_KEY", key);
    }
    let out = cmd
        .output()
        .map_err(|e| format!("cannot start server: {e}"))?;
    Ok((
        out.status.success(),
        String::from_utf8_lossy(&out.stdout).into_owned(),
    ))
}

/// Create the configuration on first run; an existing file is only checked,
/// never rewritten. The file appears atomically and only after the server
/// accepted it (`--init-directories` also creates the data folders).
fn ensure_config(exe: &Path) -> Result<PathBuf, String> {
    let path = config_path()?;
    let cfg = path.to_str().ok_or("config path is not UTF-8")?.to_string();
    if path.exists() {
        let (ok, out) = server_once(
            exe,
            &["run", "--config", &cfg, "--check-config", "--json"],
            None,
        )?;
        return if ok {
            Ok(path)
        } else {
            Err(format!(
                "설정 파일이 올바르지 않습니다: {}\n{out}",
                path.display()
            ))
        };
    }
    std::fs::create_dir_all(path.parent().ok_or("config path has no parent")?)
        .map_err(|e| format!("설정 폴더를 만들 수 없습니다: {e}"))?;
    let partial = path.with_extension("toml.partial");
    std::fs::write(&partial, default_config(&home()?))
        .map_err(|e| format!("설정 파일을 쓸 수 없습니다: {e}"))?;
    let partial_s = partial.to_str().ok_or("config path is not UTF-8")?;
    let (ok, out) = server_once(
        exe,
        &["run", "--config", partial_s, "--init-directories", "--json"],
        None,
    )?;
    if !ok {
        let _ = std::fs::remove_file(&partial);
        return Err(format!("설정을 만들지 못했습니다:\n{out}"));
    }
    std::fs::rename(&partial, &path).map_err(|e| format!("설정 파일을 저장할 수 없습니다: {e}"))?;
    Ok(path)
}

/// API key from the login Keychain (item written beforehand with
/// `security add-generic-password -s dev.junhcha.weaver -a anthropic -w`).
fn keychain_key() -> Option<String> {
    let out = Command::new("/usr/bin/security")
        .args([
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            "anthropic",
            "-w",
        ])
        .output()
        .ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
        .filter(|k| !k.is_empty())
}

fn show_status(handle: &tauri::AppHandle, text: &str) {
    if let Some(window) = handle.get_webview_window("main") {
        let js = format!(
            "document.getElementById('status').textContent = {};",
            serde_json_str(text)
        );
        let _ = window.eval(&js);
    }
}

fn serde_json_str(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            c if (c as u32) < 0x20 => out.push(' '),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn spawn(
    app: &tauri::AppHandle,
    name: &'static str,
    args: Vec<String>,
    api_key: Option<&str>,
) -> tauri::Result<()> {
    let exe = app
        .path()
        .resource_dir()?
        .join("server")
        .join("weaver-server");
    let mut command = app.shell().command(exe).args(args);
    if let Some(key) = api_key {
        command = command.env("ANTHROPIC_API_KEY", key);
    }
    let (mut rx, child) = command
        .spawn()
        .map_err(|e| tauri::Error::Anyhow(e.into()))?;
    eprintln!("[desktop] {name}: pid {}", child.pid());
    app.state::<Sidecars>().0.lock().unwrap().push(child);
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    eprintln!("[{name}] {}", String::from_utf8_lossy(&line).trim_end())
                }
                CommandEvent::Stderr(line) => {
                    eprintln!("[{name}] {}", String::from_utf8_lossy(&line).trim_end())
                }
                CommandEvent::Terminated(status) => {
                    // `run` exits with EXIT_ALREADY_RUNNING when a launchd agent
                    // already holds the state lock; the web UI still works.
                    eprintln!("[desktop] {name} exited: {:?}", status.code);
                }
                _ => {}
            }
        }
    });
    Ok(())
}

fn wait_for_port(port: u16, timeout: Duration) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let start = Instant::now();
    while start.elapsed() < timeout {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    false
}

/// SIGTERM instead of the shell plugin's SIGKILL: the server shuts down cleanly on SIGTERM (releasing the state lock and port),
/// while SIGKILL would skip that and leave the state lock and port held.
fn stop_all(app: &tauri::AppHandle) {
    let children: Vec<CommandChild> = app
        .state::<Sidecars>()
        .0
        .lock()
        .unwrap()
        .drain(..)
        .collect();
    for child in &children {
        #[cfg(unix)]
        unsafe {
            libc::kill(child.pid() as libc::pid_t, libc::SIGTERM);
        }
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    for child in children {
        #[cfg(unix)]
        while Instant::now() < deadline && unsafe { libc::kill(child.pid() as libc::pid_t, 0) } == 0
        {
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = child.kill();
    }
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Sidecars::default())
        .setup(|app| {
            let handle = app.handle().clone();
            let res = app.path().resource_dir()?;
            let web_dir = res.join("web");
            let exe = res.join("server").join("weaver-server");
            std::thread::spawn(move || {
                let config = match ensure_config(&exe) {
                    Ok(c) => c,
                    Err(msg) => {
                        eprintln!("[desktop] {msg}");
                        show_status(&handle, &msg);
                        return;
                    }
                };
                let cfg = config.to_string_lossy().into_owned();
                let key = keychain_key();
                if key.is_none() {
                    eprintln!("[desktop] no Anthropic key in Keychain ({KEYCHAIN_SERVICE}); LLM calls will fail");
                }
                let key = key.as_deref();
                if let Err(e) = spawn(&handle, "run", vec!["run".into(), "--config".into(), cfg.clone(), "--no-console".into()], key) {
                    show_status(&handle, &format!("서버를 시작하지 못했습니다: {e}"));
                    return;
                }
                if wait_for_port(WEB_PORT, Duration::from_millis(300)) {
                    eprintln!("[desktop] port {WEB_PORT} already serving; reusing it");
                } else if let Err(e) = spawn(
                    &handle,
                    "web",
                    vec![
                        "web".into(),
                        "--config".into(),
                        cfg,
                        "--port".into(),
                        WEB_PORT.to_string(),
                        "--static-dir".into(),
                        web_dir.to_string_lossy().into_owned(),
                    ],
                    key,
                ) {
                    show_status(&handle, &format!("웹 서버를 시작하지 못했습니다: {e}"));
                    return;
                }
                if wait_for_port(WEB_PORT, Duration::from_secs(90)) {
                    if let Some(window) = handle.get_webview_window("main") {
                        let _ = window.navigate(Url::parse(&format!("http://127.0.0.1:{WEB_PORT}/")).unwrap());
                    }
                } else {
                    show_status(&handle, "Weaver 서버가 90초 안에 시작되지 않았습니다. 로그를 확인하세요.");
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build Weaver desktop");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            stop_all(handle);
        }
    });
}
