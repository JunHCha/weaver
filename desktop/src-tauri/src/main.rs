// Minimal Tauri shell for Weaver: starts the frozen Python server twice as a
// sidecar (`run` daemon and loopback `web` UI) and points the window at it.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{SocketAddr, TcpStream};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, Url};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const WEB_PORT: u16 = 8765;
const SIDECAR: &str = "weaver-server";

#[derive(Default)]
struct Sidecars(Mutex<Vec<CommandChild>>);

fn spawn(app: &tauri::AppHandle, name: &'static str, args: Vec<String>) -> tauri::Result<()> {
    let (mut rx, child) = app
        .shell()
        .sidecar(SIDECAR)
        .map_err(|e| tauri::Error::Anyhow(e.into()))?
        .args(args)
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

/// SIGTERM instead of the shell plugin's SIGKILL: the PyInstaller onefile
/// bootloader forwards SIGTERM to the real Python process, while SIGKILL
/// would orphan it and leave the state lock and port held.
fn stop_all(app: &tauri::AppHandle) {
    let children: Vec<CommandChild> = app.state::<Sidecars>().0.lock().unwrap().drain(..).collect();
    for child in &children {
        #[cfg(unix)]
        unsafe {
            libc::kill(child.pid() as libc::pid_t, libc::SIGTERM);
        }
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    for child in children {
        #[cfg(unix)]
        while Instant::now() < deadline
            && unsafe { libc::kill(child.pid() as libc::pid_t, 0) } == 0
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
            let web_dir = app.path().resource_dir()?.join("web");
            spawn(&handle, "run", vec!["run".into(), "--no-console".into()])?;
            if wait_for_port(WEB_PORT, Duration::from_millis(300)) {
                eprintln!("[desktop] port {WEB_PORT} already serving; reusing it");
            } else {
                spawn(
                    &handle,
                    "web",
                    vec![
                        "web".into(),
                        "--port".into(),
                        WEB_PORT.to_string(),
                        "--static-dir".into(),
                        web_dir.to_string_lossy().into_owned(),
                    ],
                )?;
            }
            std::thread::spawn(move || {
                let url = format!("http://127.0.0.1:{WEB_PORT}/");
                if wait_for_port(WEB_PORT, Duration::from_secs(60)) {
                    if let Some(window) = handle.get_webview_window("main") {
                        let _ = window.navigate(Url::parse(&url).unwrap());
                    }
                } else if let Some(window) = handle.get_webview_window("main") {
                    let _ = window.eval(
                        "document.getElementById('status').textContent = \
                         'Weaver 서버가 60초 안에 시작되지 않았습니다. 로그를 확인하세요.'",
                    );
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
