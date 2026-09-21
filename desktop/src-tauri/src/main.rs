#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
// publisher-agent 桌面壳（Tauri 2）。
// 混合架构（2026-09-19 起）：掘金核心原生 Rust（juejin.rs，零 Python 依赖）；
// 浏览器相关（扫码/封面 TOS/知乎）为可选 Python 插件。

mod agent;
mod juejin;

use std::fs;
use std::path::PathBuf;
use std::process::Command;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::Manager;
use serde_json::json;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// schtasks 输出是 GBK——粗解转可读（状态判定用）
pub fn gbk_decode(bytes: &[u8]) -> String {
    // 简易 GBK→UTF8：借 PowerShell 太重，用关键特征字节判定即可，这里退化返回 lossy
    String::from_utf8_lossy(bytes).to_string()
}

fn agent_root() -> PathBuf {
    if let Ok(p) = std::env::var("PUBLISHER_AGENT_ROOT") {
        return PathBuf::from(p);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap()
}

/// 构造 driver 子进程：优先直用 venv python（免 uv 环境解析的 1~2s 白耗），
/// 回退 uv run；Windows 下必须 CREATE_NO_WINDOW——GUI 派生控制台程序不禁止
/// 会为每个子进程分配控制台，撞上 Windows Terminal 时整个应用卡死十几秒
/// （实测：开一个终端再退出才恢复——conhost 状态被终端生命周期解锁）。
fn spawn_driver(args: &[String]) -> Command {
    let root = agent_root();
    let venv_py = root.join(".venv").join("Scripts").join("python.exe");
    let mut cmd = if venv_py.exists() {
        let mut c = Command::new(&venv_py);
        c.args(["-m", "publisher_agent.driver"]);
        c
    } else {
        let mut c = Command::new("uv");
        c.args(["run", "python", "-m", "publisher_agent.driver"]);
        c
    };
    cmd.args(args).current_dir(&root);
    #[cfg(windows)]
    cmd.creation_flags(0x0800_0000);          // CREATE_NO_WINDOW
    cmd
}

fn driver_cmd(args: &[&str]) -> Result<String, String> {
    let owned: Vec<String> = args.iter().map(|s| s.to_string()).collect();
    let out = spawn_driver(&owned)
        .output()
        .map_err(|e| format!("启动 driver 失败：{e}"))?;
    Ok(format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    ))
}

/// 后台跑 driver（真实 tick / run / login）：输出落 _state/tick.log，与计划任务同路径
fn run_bg(base: &[&str], extra: &[String]) -> Result<String, String> {
    let log = agent_root().join("_state/tick.log");
    if let Some(dir) = log.parent() {
        fs::create_dir_all(dir).ok();
    }
    let f = fs::OpenOptions::new()
        .create(true).append(true).open(&log)
        .map_err(|e| format!("打不开日志 {e}"))?;
    let mut argv: Vec<String> = base.iter().map(|s| s.to_string()).collect();
    argv.extend(extra.iter().cloned());
    let mut cmd = spawn_driver(&argv);
    cmd.stdout(std::process::Stdio::from(f.try_clone().map_err(|e| e.to_string())?))
        .stderr(std::process::Stdio::from(f));
    cmd.spawn().map_err(|e| format!("后台启动失败：{e}"))?;
    Ok(format!("已后台启动（{base:?} {extra:?}），完成后刷新看板/日志"))
}

#[tauri::command]
fn get_dashboard() -> Result<serde_json::Value, String> {
    let root = agent::app_data_root();
    let mut a = agent::Agent::load().map_err(|e| e.0)?;
    let dash: serde_json::Value = serde_json::from_str(&a.status_json().map_err(|e| e.0)?)
        .map_err(|e| e.to_string())?;
    let events = a.recent_events(40);
    let heartbeat = a.meta_get("heartbeat").unwrap_or_else(|| "无".into());
    let plan_raw = fs::read_to_string(root.join("plan.yaml")).unwrap_or_default();
    let mut pipelines: Vec<String> = vec!["default(内置)".into()];
    if let Ok(rd) = fs::read_dir(root.join("pipelines")) {
        for e in rd.flatten() {
            if e.path().extension().map(|x| x == "yaml").unwrap_or(false) {
                if let Some(n) = e.path().file_stem() {
                    pipelines.push(n.to_string_lossy().into_owned());
                }
            }
        }
    }
    Ok(serde_json::json!({
        "rows": dash["rows"], "gates": dash["gates"], "pipeline": dash["pipeline"],
        "heartbeat_task": dash["heartbeat_task"],
        "events": events, "heartbeat": heartbeat, "plan_raw": plan_raw,
        "pipelines": pipelines,
    }))
}

/// R4：原生 deploy——注册本 exe 自身为每小时心跳（--tick 参数，纯 Rust 无 Python）
#[tauri::command]
fn native_deploy() -> Result<String, String> {
    let q = std::process::Command::new("schtasks")
        .args(["/Query", "/TN", "BlogAgent_hourly", "/FO", "LIST"]).creation_flags(0x0800_0000).output();
    if let Ok(o) = q {
        if o.status.success() {
            // 已存在但被停用（队列发完会自动停）→ 重新启用而不是只报「已存在」
            // （2026-09-20 实测事故：报已存在但任务禁用，心跳一天没跑）
            let text = format!("{}{}", String::from_utf8_lossy(&o.stdout), String::from_utf8_lossy(&o.stderr));
            let ps = "(schtasks /Query /TN BlogAgent_hourly /FO LIST | Out-String) -match '已禁用|Disabled'";
            let dis = std::process::Command::new("powershell")
                .args(["-NoProfile", "-Command", ps]).creation_flags(0x0800_0000).output()
                .map(|o| String::from_utf8_lossy(&o.stdout).trim() == "True")
                .unwrap_or(false);
            if dis {
                let _ = std::process::Command::new("schtasks")
                    .args(["/Change", "/TN", "BlogAgent_hourly", "/ENABLE"])
                    .creation_flags(0x0800_0000).output();
                return Ok("✓ 心跳任务已存在（此前停用）——已重新启用".into());
            }
            let _ = text;
            return Ok("✓ 心跳任务 BlogAgent_hourly 已存在且在跑".into());
        }
    }
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let ps = format!(
        "$a = New-ScheduledTaskAction -Execute '{}' -Argument '--tick'; \
         $t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) \
         -RepetitionInterval (New-TimeSpan -Hours 1); \
         $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; \
         Register-ScheduledTask -TaskName BlogAgent_hourly -Action $a -Trigger $t -Settings $s | Out-Null",
        exe.to_string_lossy());
    let r = std::process::Command::new("powershell")
        .args(["-NoProfile", "-Command", &ps]).creation_flags(0x0800_0000).output()
        .map_err(|e| e.to_string())?;
    if r.status.success() {
        Ok("✓ 已注册 BlogAgent_hourly（每小时心跳，目标=本 exe --tick，纯 Rust）".into())
    } else {
        Err(format!("注册失败：{}", String::from_utf8_lossy(&r.stderr)))
    }
}

/// 内容源扫描（原生）：文件夹/单文件 → 文章清单 JSON（不再依赖 Python driver）
#[tauri::command]
fn native_scan(path: String) -> Result<String, String> {
    let p = std::path::PathBuf::from(&path);
    let files: Vec<std::path::PathBuf> = if p.is_file() {
        vec![p]
    } else {
        let mut v: Vec<std::path::PathBuf> = walk_files(&p);
        v.sort();
        v
    };
    let mut rows = vec![];
    for f in files {
        if f.extension().map(|x| x != "md").unwrap_or(true) { continue; }
        let Ok((fields, body)) = agent::Agent::parse_post(&f) else { continue };
        rows.push(json!({
            "file": f.to_string_lossy(),
            "name": f.file_name().map(|x| x.to_string_lossy().to_string()).unwrap_or_default(),
            "folder": f.parent().and_then(|d| d.file_name()).map(|x| x.to_string_lossy().to_string()).unwrap_or_default(),
            "title_juejin": fields.get("title_juejin").cloned().unwrap_or_default(),
            "tags": fields.get("tags").cloned().unwrap_or_default(),
            "description": fields.get("description").cloned().unwrap_or_default(),
            "chars": body.chars().count(),
        }));
    }
    Ok(serde_json::to_string(&rows).unwrap())
}

fn walk_files(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
    let mut out = vec![];
    if let Ok(rd) = fs::read_dir(dir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                out.extend(walk_files(&p));
            } else {
                out.push(p);
            }
        }
    }
    out
}

/// 停用心跳（原生，替代 Python undeploy 路由）
#[tauri::command]
fn native_undeploy() -> Result<String, String> {
    let r = std::process::Command::new("schtasks")
        .args(["/Change", "/TN", "BlogAgent_hourly", "/DISABLE"])
        .creation_flags(0x0800_0000).output()
        .map_err(|e| e.to_string())?;
    if r.status.success() {
        let a = agent::Agent::load().map_err(|e| e.0)?;
        a.event("deploy.undeploy", json!({"ok": true}));
        Ok("✓ 心跳已停用（计划与状态保留，重新部署即恢复）".into())
    } else {
        Err(format!("停用失败（任务不存在？）：{}", String::from_utf8_lossy(&r.stderr)))
    }
}

/// 分类下拉数据：[(id, 名称)]
#[tauri::command]
fn native_categories() -> Result<String, String> {
    let j = juejin::Juejin::load().map_err(|e| e.to_string())?;
    let cats = j.categories().map_err(|e| e.to_string())?;
    Ok(serde_json::to_string(&cats).unwrap())
}

/// 改文章 frontmatter 字段（写回 md——文件即档案，单一事实源）。
/// 行级替换且仅作用于 frontmatter 区块（--- 与 --- 之间）：
/// 正则全文替换会把正文代码块里的 `tags: xxx` 示例一并改坏，且 $ 展开会吞用户值（检视 major）。
#[tauri::command]
fn native_set_field(file: String, key: String, value: String) -> Result<String, String> {
    const KEYS: [&str; 4] = ["title_juejin", "tags", "category_id", "description"];
    if !KEYS.contains(&key.as_str()) {
        return Err(format!("不支持的字段 {key}（可用：{KEYS:?}）"));
    }
    let p = std::path::PathBuf::from(&file);
    let raw = fs::read_to_string(&p).map_err(|e| format!("读不了：{e}"))?;
    let mut lines: Vec<String> = raw.split('\n').map(|l| l.to_string()).collect();
    // 定位 frontmatter：首行 --- 起，到下一个 --- 止
    if lines.first().map(|l| l.trim() != "---").unwrap_or(true) {
        return Err("文件缺 frontmatter（首行应为 ---）".into());
    }
    let Some(end) = (1..lines.len()).find(|&i| lines[i].trim() == "---") else {
        return Err("frontmatter 未闭合（缺第二个 ---）".into());
    };
    let safe = value.replace(['\n', '\r'], " ").trim().to_string();
    if safe.is_empty() && key != "tags" {
        return Err("不能为空（tags 可清空）".into());
    }
    if key == "description" && safe.chars().count() < 50 {
        return Err(format!("摘要 {} 字 < 掘金 50 字下限（发布也会拦）", safe.chars().count()));
    }
    let prefix = format!("{key}:");
    let hit = (1..end).find(|&i| lines[i].starts_with(&prefix));
    let Some(i) = hit else {
        return Err(format!("frontmatter 里没有 {key} 行"));
    };
    lines[i] = format!("{key}: {safe}");
    fs::write(&p, lines.join("\n")).map_err(|e| format!("写回失败：{e}"))?;
    Ok(format!("✓ {key} 已写回 {}", p.file_name().map(|x| x.to_string_lossy().to_string()).unwrap_or_default()))
}

/// 立即发布指定条目（可多篇，跳过排期时间，保留防重发护栏）——异步不冻 UI
#[tauri::command]
async fn native_publish_now(slugs: Vec<String>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        agent::Agent::load().map_err(|e| e.0)?.publish_now(slugs).map_err(|e| e.0)
    }).await.map_err(|e| e.to_string())?
}

/// 原生 tick（R3）：Rust driver 核心跑默认流水线，dry=干跑。
/// spawn_blocking：分钟级发布不再冻结 UI 主线程（检视 minor）
#[tauri::command]
async fn native_tick(dry: bool) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        agent::Agent::load().map_err(|e| e.0)?.tick(dry).map_err(|e| e.0)
    }).await.map_err(|e| e.to_string())?
}

/// 原生自检（App 自包含体系：凭据/计划/心跳；不再依赖 Python 引擎在场）
#[tauri::command]
async fn native_doctor() -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        agent::Agent::load().map_err(|e| e.0)?.doctor().map_err(|e| e.0)
    }).await.map_err(|e| e.to_string())?
}

/// 原生 status --json（R3）
#[tauri::command]
fn native_status() -> Result<String, String> {
    agent::Agent::load().map_err(|e| e.0)?.status_json().map_err(|e| e.0)
}

/// R1 只读真机验证：Rust 原生掘金客户端（whoami/columns/近作）
#[tauri::command]
fn rust_api_selftest() -> Result<String, String> {
    let j = juejin::Juejin::load().map_err(|e| e.to_string())?;
    let (uid, name) = j.whoami().map_err(|e| e.to_string())?;
    let cols = j.columns(&uid).map_err(|e| e.to_string())?;
    let arts = j.recent_articles(&uid).map_err(|e| e.to_string())?;
    Ok(format!(
        "✓ Rust 原生掘金客户端全通：Cookie 有效（{name}）｜专栏 {} 个｜已发 {} 篇，最新《{}》",
        cols.len(),
        arts.len(),
        arts.first().map(|a| a.0.clone()).unwrap_or_default()
    ))
}

/// 内容源扫描（工作台「内容源」页）：文件夹/单文件 → 文章清单 JSON
#[tauri::command]
fn scan_source(path: String) -> Result<String, String> {
    driver_cmd(&["scan", &path])
}

/// 编排结果落盘（原生）：JSON 规格 → 校验 → 写 APPDATA\plan.yaml + 事件
#[tauri::command]
fn save_plan(spec: String) -> Result<String, String> {
    let v: serde_json::Value = serde_json::from_str(&spec).map_err(|e| format!("规格 JSON 解析失败：{e}"))?;
    let queue = v["queue"].as_array().ok_or("queue 缺失或不是数组")?;
    if queue.is_empty() {
        return Err("队列为空，拒绝写入".into());
    }
    let mut seen = std::collections::HashSet::new();
    for e in queue {
        let id = e["id"].as_str().unwrap_or("");
        let file = e["file"].as_str().unwrap_or("");
        if id.is_empty() || file.is_empty() {
            return Err(format!("条目缺 id/file：{e}"));
        }
        if !seen.insert(id.to_string()) {
            return Err(format!("slug 重复：{id}"));
        }
        if !std::path::PathBuf::from(file).exists() {
            return Err(format!("文件不存在：{file}"));
        }
        for pf in ["juejin"] {
            if let Some(at) = e[pf]["at"].as_str() {
                if chrono::NaiveDateTime::parse_from_str(at, "%Y-%m-%d %H:%M").is_err() {
                    return Err(format!("at 时间格式应为 'YYYY-MM-DD HH:MM'：{id}.{pf}={at}"));
                }
            }
        }
    }
    // cadence 默认 + 覆盖
    let mut doc = serde_json::json!({"cadence": {"juejin": {"min_gap_h": 0}}});
    if let Some(c) = v["cadence"].as_object() {
        doc["cadence"] = serde_json::Value::Object(c.clone());
    }
    doc["queue"] = v["queue"].clone();
    let yaml = serde_yaml::to_string(&doc).map_err(|e| format!("YAML 序列化：{e}"))?;
    let root = agent::app_data_root();
    let tmp = root.join("plan.yaml.tmp");
    fs::write(&tmp, yaml).map_err(|e| format!("写失败：{e}"))?;
    fs::rename(&tmp, root.join("plan.yaml")).map_err(|e| format!("原子替换失败：{e}"))?;
    let a = agent::Agent::load().map_err(|e| e.0)?;
    a.event("plan.updated", serde_json::json!({"items": queue.len()}));
    // 新计划写入即自动唤醒被停用的心跳（9-21 事故：队列发完自动停用后，用户补排新计划
    // 却以为还在自动发——save_plan 必须自愈启用，不能再依赖人记得点部署）
    let mut woke = false;
    if agent::heartbeat_task_state() == "disabled" {
        let r = std::process::Command::new("schtasks")
            .args(["/Change", "/TN", "BlogAgent_hourly", "/ENABLE"])
            .creation_flags(0x0800_0000).output();
        if r.map(|o| o.status.success()).unwrap_or(false) {
            woke = true;
            a.event("deploy.auto-woke", json!({"by": "plan.updated"}));
        }
    }
    let _ = agent::toast("📅 发布计划已更新", &format!("{} 条入队{}",
        queue.len(), if woke { "，心跳已自动恢复" } else { "" }));
    Ok(format!("✓ plan.yaml 已更新（{n} 条，APPDATA）{wake}",
        n = queue.len(), wake = if woke { "——检测到心跳停用，已自动恢复" } else { "，部署心跳后按时间执行" }))
}

/// 命令面即 IPC：白名单子命令 + 透传参数向量（红队承认的 IPC 变更）
#[tauri::command]
fn run_driver(cmd: String, args: Option<Vec<String>>) -> Result<String, String> {
    let extra = args.unwrap_or_default();
    match cmd.as_str() {
        "tick" => run_bg(&["tick"], &[]),
        "run" => {
            let dry = extra.iter().any(|a| a == "--dry");
            let simple = extra.iter().all(|a| a == "--dry" || !a.starts_with('-'));
            if dry && simple {
                let mut a: Vec<String> = vec!["run".into()];
                a.extend(extra.iter().cloned());
                let refs: Vec<&str> = a.iter().map(|s| s.as_str()).collect();
                driver_cmd(&refs)
            } else {
                run_bg(&["run"], &extra)
            }
        }
        "gate" | "doctor" | "status" | "report" | "deploy" | "undeploy" => {
            let mut a: Vec<String> = vec![cmd.clone()];
            a.extend(extra.iter().cloned());
            let refs: Vec<&str> = a.iter().map(|s| s.as_str()).collect();
            driver_cmd(&refs)
        }
        // 登录要开扫码窗口最长 5 分钟——必须后台跑，输出落日志，绝不堵 UI
        "login" => run_bg(&["login"], &extra),
        other => Err(format!("未知子命令：{other}")),
    }
}

fn main() {
    // --tick / --dry：exe 自身作为计划任务目标的无窗口执行模式（纯 Rust 心跳）
    let argv: Vec<String> = std::env::args().collect();
    if let Some(idx) = argv.iter().position(|a| a == "--publish-now") {
        let slugs: Vec<String> = argv.get(idx + 1).map(|s|
            s.split(',').map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect())
            .unwrap_or_default();
        let log = agent::Agent::load()
            .and_then(|mut a| a.publish_now(slugs))
            .unwrap_or_else(|e| format!("✗ publish-now 失败：{}", e.0));
        println!("{log}");
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true)
            .open(agent::app_data_root().join("tick.log")) {
            use std::io::Write;
            let _ = writeln!(f, "
[{}] 
{}", chrono::Local::now().format("%Y-%m-%d %H:%M:%S"), log);
        }
        return;
    }
    if argv.iter().any(|a| a == "--tick") {
        let dry = argv.iter().any(|a| a == "--dry");
        let log = agent::Agent::load()
            .and_then(|mut a| a.tick(dry))
            .unwrap_or_else(|e| format!("✗ tick 失败：{}", e.0));
        let root = agent::app_data_root();
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true)
            .open(root.join("tick.log")) {
            use std::io::Write;
            let _ = writeln!(f, "\n[{}] \n{}", chrono::Local::now().format("%Y-%m-%d %H:%M:%S"), log);
        }
        return;
    }
    tauri::Builder::default()
        .setup(|app| {
            let show = MenuItem::with_id(app, "show", "打开看板", true, None::<&str>)?;
            let tick = MenuItem::with_id(app, "tick", "立即 tick（真实发布）", true, None::<&str>)?;
            let dry = MenuItem::with_id(app, "dry", "干跑 tick", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &tick, &dry, &quit])?;
            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("publisher-agent")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            w.show().ok();
                            w.set_focus().ok();
                        }
                    }
                    "tick" => { run_bg(&["tick"], &[]).ok(); }
                    "dry" => {
                        let e: Vec<String> = vec!["--dry".into()];
                        run_bg(&["tick"], &e).ok();
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                window.hide().ok();
                api.prevent_close();
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_dashboard, run_driver, scan_source, save_plan, rust_api_selftest,
            native_tick, native_status, native_deploy, native_set_field, native_doctor,
            native_categories, native_publish_now, native_scan, native_undeploy
        ])
        .plugin(tauri_plugin_dialog::init())
        .run(tauri::generate_context!())
        .expect("publisher-agent 桌面壳启动失败");
}
