//! Rust 原生 driver 核心（混合架构 R3）——与 Python 版共用同一套数据文件
//! （plan.yaml / _state/pub_state.json / events.jsonl，schema 一致，可互换接管）。
//! 默认流水线五步：对账回填 → 审核轮询 → 选篇护栏 → 发布 → 收尾。

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::juejin::{Juejin, ApiError};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const TERMINAL: [&str; 4] = ["live", "closed", "held", "rejected"];


/// 字符安全截断（按字节切中文会 panic；&s[..n] 族全部换这个）
pub fn trunc(s: &str, n: usize) -> String { s.chars().take(n).collect::<String>() }
/// 字符安全截尾（取最后 n 个字符）
pub fn trunc_tail(s: &str, n: usize) -> String {
    let c: Vec<char> = s.chars().collect();
    if c.len() <= n { s.to_string() } else { c[c.len() - n..].iter().collect() }
}

fn now_ts() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

fn agent_root() -> PathBuf {
    if let Ok(p) = std::env::var("PUBLISHER_AGENT_ROOT") {
        let pb = PathBuf::from(&p);
        if pb.join("agent.yaml").exists() {
            return pb;
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize()
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
}

/// 跨进程互斥锁（critical 修复）：计划任务 --tick 与 UI/CLI 立即发布可能并发，
/// 全量快照回写会把对方刚写的 in_review/article_id 回滚回 pending → 重复发文。
/// 文件锁 + 15 分钟陈旧破除。
pub struct AppLock { path: PathBuf }
impl AppLock {
    pub fn acquire() -> Option<AppLock> {
        let p = app_data_root().join("tick.lock");
        if p.exists() {
            let stale = fs::metadata(&p).and_then(|m| m.modified()).ok()
                .and_then(|t| t.elapsed().ok())
                .map(|e| e.as_secs() > 900).unwrap_or(true);
            if !stale { return None; }
            let _ = fs::remove_file(&p);
        }
        fs::write(&p, std::process::id().to_string()).ok()?;
        Some(AppLock { path: p })
    }
}
impl Drop for AppLock {
    fn drop(&mut self) { let _ = fs::remove_file(&self.path); }
}

/// App 自有数据根：%APPDATA%\publisher-agent\（不依赖源码仓库/工作目录）
pub fn app_data_root() -> PathBuf {
    let base = std::env::var("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| agent_root().join("_state"));
    let dir = base.join("publisher-agent");
    let _ = fs::create_dir_all(&dir);
    dir
}

pub struct Agent {
    pub root: PathBuf,          // %APPDATA%\publisher-agent
    pub db: rusqlite::Connection,
    pub state: Value,           // 内存态（load 自 DB，save 写回 DB）
}

pub struct PlanItem {
    pub id: String,
    pub file: String,
    pub juejin_at: Option<String>,
    pub column: Option<String>,
    pub zhihu_declared: bool,   // 计划里声明了 zhihu 平台（partial 判定用）
}

#[derive(Debug)]
pub struct Err2(pub String);

impl From<ApiError> for Err2 {
    fn from(e: ApiError) -> Self { Err2(e.0) }
}

fn yaml_str(v: &serde_yaml::Value, k: &str) -> Option<String> {
    v.get(k).and_then(|x| x.as_str()).map(|s| s.to_string())
}

impl Agent {
    pub fn load() -> Result<Self, Err2> {
        let root = app_data_root();
        let db = rusqlite::Connection::open(root.join("data.db"))
            .map_err(|e| Err2(format!("打开 data.db 失败：{e}")))?;
        db.execute_batch(
            "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
             CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts TEXT, type TEXT, json TEXT);
             CREATE TABLE IF NOT EXISTS entries(slug TEXT PRIMARY KEY, data TEXT);")
            .map_err(|e| Err2(e.to_string()))?;
        let mut a = Self { root: root.clone(), db, state: json!({"entries": {}}) };
        a.migrate_from_repo();
        a.load_state_from_db();
        Ok(a)
    }

    /// 一次性迁移：从源码仓库的旧数据（pub_state.json/events.jsonl/plan.yaml/Cookie）搬进 APPDATA
    fn migrate_from_repo(&mut self) {
        let old = agent_root();
        let done: bool = self.meta_get("migrated").is_some();
        if done { return; }
        // plan.yaml
        let old_plan = old.join("plan.yaml");
        if old_plan.exists() && !self.root.join("plan.yaml").exists() {
            let _ = fs::copy(&old_plan, self.root.join("plan.yaml"));
        }
        // 状态与事件
        let old_state = old.join("_state/pub_state.json");
        if let Ok(text) = fs::read_to_string(&old_state) {
            if let Ok(v) = serde_json::from_str::<Value>(&text) {
                if let Some(obj) = v.as_object() {
                    for (slug, entry) in obj.get("entries").and_then(|e| e.as_object()).into_iter().flatten() {
                        let _ = self.db.execute(
                            "INSERT OR REPLACE INTO entries(slug, data) VALUES(?1, ?2)",
                            rusqlite::params![slug, entry.to_string()]);
                    }
                    for (k, val) in obj {
                        if k != "entries" && !val.is_null() {
                            let _ = self.meta_set(k, &val.to_string());
                        }
                    }
                }
            }
        }
        if let Ok(text) = fs::read_to_string(old.join("_state/events.jsonl")) {
            for line in text.lines() {
                if let Ok(v) = serde_json::from_str::<Value>(line) {
                    let _ = self.db.execute(
                        "INSERT INTO events(ts, type, json) VALUES(?1, ?2, ?3)",
                        rusqlite::params![v["ts"].as_str().unwrap_or(""), v["type"].as_str().unwrap_or(""), line]);
                }
            }
        }
        if let Ok(hb) = fs::read_to_string(old.join("_state/heartbeat")) {
            let _ = self.meta_set("heartbeat", hb.trim());
        }
        // Cookie / uuid（从引擎 .juejin.env 与 juejin_meta.json）
        let engine = crate::juejin::engine_cwd();
        if let Ok(env) = fs::read_to_string(engine.join(".juejin.env")) {
            if let Some(ck) = env.lines().find(|l| l.starts_with("JUEJIN_COOKIE=")) {
                let _ = self.meta_set("juejin_cookie", ck.splitn(2, '=').nth(1).unwrap_or("").trim());
            }
        }
        if let Ok(meta) = fs::read_to_string(engine.join("_wf/juejin_meta.json")) {
            if let Ok(v) = serde_json::from_str::<Value>(&meta) {
                if let Some(u) = v["uuid"].as_str() { let _ = self.meta_set("juejin_uuid", u); }
                if let Some(u) = v["user_id"].as_str() { let _ = self.meta_set("juejin_user_id", u); }
            }
        }
        let _ = self.meta_set("migrated", "1");
        let _ = self.event("app.migrated-to-appdata", json!({"from": old.to_string_lossy()}));
    }

    fn load_state_from_db(&mut self) {
        let mut state = json!({"entries": {}});
        if let Ok(mut stmt) = self.db.prepare("SELECT slug, data FROM entries") {
            if let Ok(rows) = stmt.query_map([], |r| {
                Ok::<(String, String), rusqlite::Error>((r.get(0)?, r.get(1)?))
            }) {
                for row in rows.flatten() {
                    let (slug, data) = row;
                    if let Ok(v) = serde_json::from_str::<Value>(&data) {
                        state["entries"][slug] = v;
                    }
                }
            }
        }
        if let Ok(mut stmt) = self.db.prepare("SELECT key, value FROM meta") {
            if let Ok(rows) = stmt.query_map([], |r| {
                Ok::<(String, String), rusqlite::Error>((r.get(0)?, r.get(1)?))
            }) {
                for row in rows.flatten() {
                    let (k, v) = row;
                    if k != "migrated" && !v.is_empty() {
                        if let Ok(val) = serde_json::from_str::<Value>(&v) {
                            state[&k] = val;
                        }
                    }
                }
            }
        }
        self.state = state;
    }

    pub fn meta_get(&self, key: &str) -> Option<String> {
        self.db.query_row("SELECT value FROM meta WHERE key=?1", [key], |r| r.get(0)).ok()
    }

    pub fn meta_set(&self, key: &str, value: &str) -> rusqlite::Result<usize> {
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?1, ?2) ON CONFLICT(key) DO UPDATE SET value=?2",
            rusqlite::params![key, value])
    }

    pub fn save(&mut self) -> Result<(), Err2> {
        // entries 全量回写（量级小，slug 数 = 文章数）
        if let Some(obj) = self.state.as_object() {
            for (slug, entry) in obj.get("entries").and_then(|e| e.as_object()).into_iter().flatten() {
                let _ = self.db.execute(
                    "INSERT OR REPLACE INTO entries(slug, data) VALUES(?1, ?2)",
                    rusqlite::params![slug, entry.to_string()]);
            }
            for (k, val) in obj {
                if k != "entries" && !val.is_null() {
                    let _ = self.meta_set(k, &val.to_string());
                }
            }
        }
        Ok(())
    }

    pub fn event(&self, typ: &str, kv: Value) {
        let mut rec = json!({"ts": chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string(), "type": typ});
        if let (Some(r), Some(k)) = (rec.as_object_mut(), kv.as_object()) {
            for (key, v) in k {
                r.insert(key.clone(), v.clone());
            }
        }
        let _ = self.db.execute(
            "INSERT INTO events(ts, type, json) VALUES(?1, ?2, ?3)",
            rusqlite::params![rec["ts"].as_str().unwrap_or(""), typ, rec.to_string()]);
        let _ = self.meta_set("heartbeat", rec["ts"].as_str().unwrap_or(""));
    }

    /// 日志页：最近 n 条事件（最新在前）
    pub fn recent_events(&self, n: usize) -> Vec<Value> {
        let mut out = vec![];
        if let Ok(mut stmt) = self.db.prepare(
            "SELECT json FROM events ORDER BY id DESC LIMIT ?1") {
            if let Ok(rows) = stmt.query_map([n as i64], |r| r.get::<_, String>(0)) {
                for j in rows.flatten() {
                    if let Ok(v) = serde_json::from_str::<Value>(&j) {
                        out.push(v);
                    }
                }
            }
        }
        out
    }

    pub fn plan_items(&self) -> Result<Vec<PlanItem>, Err2> {
        let plan = self.root.join("plan.yaml");
        let text = match fs::read_to_string(&plan) {
            Ok(t) => t,
            Err(e) => return Err(Err2(format!("plan.yaml（{}）读不了：{e}", plan.display()))),
        };
        let y: serde_yaml::Value = serde_yaml::from_str(&text)
            .map_err(|e| Err2(format!("plan.yaml 解析失败：{e}")))?;
        let mut out = vec![];
        if let Some(q) = y.get("queue").and_then(|q| q.as_sequence()) {
            for e in q {
                let id = yaml_str(e, "id").unwrap_or_default();
                let file = yaml_str(e, "file").unwrap_or_default();
                let jj = e.get("juejin").cloned().unwrap_or(serde_yaml::Value::Null);
                out.push(PlanItem {
                    id,
                    file,
                    juejin_at: yaml_str(&jj, "at"),
                    column: yaml_str(&jj, "column"),
                    zhihu_declared: e.get("zhihu").map(|z| !z.is_null()).unwrap_or(false),
                });
            }
        }
        Ok(out)
    }

    pub fn entry_status_pub(&self, slug: &str) -> String {
        self.entry_status(slug)
    }

    /// 重置条目到 pending（清平台记录——被删文章的旧 id/冻结标题一并清除）
    pub fn reset_entry(&mut self, slug: &str) {
        self.state["entries"][slug] = json!({"status": "pending"});
    }

    fn entry_status(&self, slug: &str) -> String {
        self.state["entries"][slug]["status"].as_str().unwrap_or("pending").to_string()
    }

    fn set_entry(&mut self, slug: &str, status: &str) {
        self.state["entries"][slug]["status"] = json!(status);
        self.state["entries"][slug]["updated"] = json!(chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string());
    }

    /// 轻量 frontmatter 解析（title_juejin/description/tags/category_id）+ 剥 HTML 注释
    pub fn parse_post(path: &Path) -> Result<(HashMap<String, String>, String), Err2> {
        let raw = fs::read_to_string(path).map_err(|e| Err2(format!("{:?} 读不了：{e}", path)))?;
        let re = regex::Regex::new(r"(?s)^---\s*\n(.*?)\n---\s*\n?(.*)$").unwrap();
        let caps = re.captures(&raw).ok_or(Err2(format!("{} 缺 frontmatter", path.display())))?;
        let mut fields = HashMap::new();
        for line in caps[1].lines() {
            let mut it = line.trim().splitn(2, ':');
            if let (Some(k), Some(v)) = (it.next(), it.nth(0)) {
                let k = k.trim().to_string();
                if k.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') && !k.is_empty() {
                    fields.insert(k, v.trim().trim_matches('"').trim_matches('\'').to_string());
                }
            }
        }
        let body = regex::Regex::new(r"(?s)<!--.*?-->").unwrap()
            .replace_all(&caps[2], "")
            .to_string();
        Ok((fields, body))
    }

    pub fn fingerprint(path: &Path) -> String {
        let mut h = Sha256::new();
        h.update(fs::read_to_string(path).unwrap_or_default().as_bytes());
        format!("{:x}", h.finalize())[..8].to_string()
    }

    /// 默认流水线（= Python 版 default 的掘金五步）
    pub fn tick(&mut self, dry: bool) -> Result<String, Err2> {
        let _lock = match AppLock::acquire() {
            Some(l) => l,
            None => return Ok("[锁] 另一发布进程正在运行，本次跳过（防并发回写/重发）".into()),
        };
        let run_id = format!("{}-{:04x}", chrono::Local::now().format("%m%d%H%M"),
                             (now_ts() * 1000.0) as u32 & 0xffff);
        self.event("pipeline.start", json!({"run_id": run_id, "pipeline": "default(rust)", "dry": dry}));
        let mut log = vec![];
        let j = Juejin::load().map_err(Err2::from)?;
        let (uid, _name) = j.whoami().map_err(Err2::from)?;

        // ① 对账回填 + 去重表
        let arts = j.recent_articles(&uid).map_err(Err2::from)?;
        let titles: std::collections::HashSet<String> = arts.iter().map(|a| a.0.clone()).collect();
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        let need_reconcile = self.state["last_reconcile"].as_str() != Some(today.as_str());
        if need_reconcile {
            let items = self.plan_items()?;
            for it in &items {
                if self.state["entries"][&it.id]["platforms"]["juejin"]["live"].as_bool() == Some(true) {
                    continue;
                }
                let p = Path::new(&it.file);
                if !p.exists() { continue; }
                let Ok((fields, _)) = Self::parse_post(p) else { continue };
                let tj = fields.get("title_juejin").cloned().unwrap_or_default();
                if let Some((_, aid, ctime)) = arts.iter().find(|a| a.0 == tj) {
                    let e = &mut self.state["entries"][&it.id]["platforms"]["juejin"];
                    *e = json!({"live": true, "live_ts": ctime, "published_ts": ctime,
                                "title": tj, "sha": Self::fingerprint(p), "article_id": aid});
                    let zh_live = self.state["entries"][&it.id]["platforms"]["zhihu"]["live"].as_bool() == Some(true);
                    self.set_entry(&it.id, if it.zhihu_declared && !zh_live { "partial" } else { "live" });
                    self.event("reconcile.backfilled", json!({"slug": &it.id, "run_id": &run_id}));
                    log.push(format!("[对账] 回填 {}（{aid}）", it.id));
                }
            }
            self.state["last_reconcile"] = json!(today);
            let _ = self.save();
        }

        // ② 审核轮询
        let items = self.plan_items()?;
        let slugs: Vec<String> = self.state["entries"].as_object()
            .map(|m| m.iter().filter(|(_, v)| v["status"] == json!("in_review"))
                .map(|(k, _)| k.clone()).collect()).unwrap_or_default();
        for slug in slugs {
            let aid = self.state["entries"][&slug]["platforms"]["juejin"]["article_id"]
                .as_str().unwrap_or("").to_string();
            if aid.is_empty() { continue; }
            match j.audit_status(&aid) {
                Ok((st, why)) => {
                    if st == "live" {
                        self.state["entries"][&slug]["platforms"]["juejin"]["live"] = json!(true);
                        self.state["entries"][&slug]["platforms"]["juejin"]["live_ts"] = json!(now_ts());
                        // 只掘金声明 → live；声明了知乎且知乎未 live → partial（原实现漏查声明，纯掘金也 partial——9-20 实测 bug）
                        let zh = items.iter().find(|i| i.id == slug).map(|i| i.zhihu_declared).unwrap_or(false);
                        let zh_live = self.state["entries"][&slug]["platforms"]["zhihu"]["live"].as_bool() == Some(true);
                        self.set_entry(&slug, if zh && !zh_live { "partial" } else { "live" });
                        self.event("audit.passed", json!({"slug": &slug, "article_id": &aid, "run_id": &run_id}));
                        log.push(format!("[轮询] {} 已上线", slug));
                    } else if st == "rejected" {
                        self.set_entry(&slug, "rejected");
                        self.event("audit.rejected", json!({"slug": &slug, "run_id": &run_id}));
                        log.push(format!("[轮询] {} 被驳回（冻结不重发）", slug));
                    } else {
                        log.push(format!("[轮询] {} 审核中（{why}）", slug));
                    }
                }
                Err(e) => {
                    // 404/内容为空 = 文章已被删（外部删除或测试清理）→ 终结状态，不再每轮报错
                    if e.0.contains("404") || e.0.contains("内容为空") {
                        self.set_entry(&slug, "closed");
                        self.event("article.gone", json!({"slug": &slug, "run_id": &run_id}));
                        log.push(format!("[轮询] {} 已不存在（被删）→ closed", slug));
                    } else {
                        log.push(format!("[轮询] {} 查询失败：{}", slug, e.0));
                    }
                }
            }
        }
        let _ = self.save();

        // ③ 选篇（终端态跳过 / at 未到跳过 / 标题双表去重）
        let picked = items.iter().find(|it| {
            let st = self.entry_status(&it.id);
            if TERMINAL.contains(&st.as_str()) || ["in_review", "partial"].contains(&st.as_str()) {
                return false;
            }
            if let Some(at) = &it.juejin_at {
                let Ok(t) = chrono::NaiveDateTime::parse_from_str(at, "%Y-%m-%d %H:%M") else { return false };
                let local = chrono::Local::now().naive_local();
                if local < t { return false; }
            }
            let p = Path::new(&it.file);
            if !p.exists() { return false; }
            let (fields, _) = Self::parse_post(p).unwrap_or_default();
            let tj = fields.get("title_juejin").cloned().unwrap_or_default();
            !tj.is_empty() && !titles.contains(&tj)
                && !self.frozen_titles().contains(&tj)
        });
        let Some(picked) = picked else {
            log.push("[选篇] 无可发条目".into());
            self.finish(&items, &run_id, dry);
            let _ = self.save();
            return Ok(log.join("\n"));
        };

        if dry {
            let tj = Self::parse_post(Path::new(&picked.file)).ok()
                .and_then(|(f, _)| f.get("title_juejin").cloned()).unwrap_or_default();
            log.push(format!("[DRY] 将发布 {}《{}》", picked.id, trunc(&tj, 24)));
            self.event("dry.publish-skipped", json!({"slug": &picked.id, "platform": "juejin", "run_id": &run_id}));
            let _ = self.save();
            return Ok(log.join("
"));
        }

        self.publish_one(&picked, &j, &uid, &run_id, &mut log)?;

        Ok(log.join("\n"))
    }

    /// ④ 发布单篇（tick 选篇后与「立即发布」共用）：意图→摘要校验→标签→专栏→建稿→封面插件→发布
    fn publish_one(&mut self, picked: &PlanItem, j: &Juejin, uid: &str, run_id: &str, log: &mut Vec<String>) -> Result<(), Err2> {
        let p = Path::new(&picked.file);
        let (fields, body) = Self::parse_post(p)?;
        let title = fields.get("title_juejin").cloned().unwrap_or_default();

        // 先写意图（崩溃恢复时先核验平台再动作）
        self.state["entries"][&picked.id]["platforms"]["juejin"] = json!({
            "intent_at": now_ts(), "title": &title, "sha": Self::fingerprint(p)});
        let _ = self.save();
        log.push(format!("[发布] {}《{}》", picked.id, trunc(&title, 24)));

        let brief_raw = fields.get("description").cloned().unwrap_or_default();
        if brief_raw.chars().count() < 50 {
            self.event("publish.failed", json!({"slug": &picked.id, "kind": "BRIEF_TOO_SHORT", "run_id": run_id}));
            log.push(format!("摘要 {} 字 < 50 下限，中止本篇", brief_raw.chars().count()));
            return Ok(());
        }
        let brief: String = brief_raw.chars().take(100).collect();
        let category = fields.get("category_id").cloned().unwrap_or("6809637773935378440".into());
        let tag_names: Vec<String> = fields.get("tags").map(|s|
            s.split(',').map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect()).unwrap_or_default();

        let mut known = HashMap::new();
        for (k, v) in [("人工智能", "6809640642101116936"), ("面试", "6809640404791590919"),
                       ("架构", "6809640501776482317"), ("团队管理", "6809641183699009550"),
                       ("程序员", "6809640482725953550"), ("AI编程", "7467857238494019610")] {
            known.insert(k.to_string(), v.to_string());
        }
        let mut fb: HashMap<String, Option<String>> = HashMap::new();
        for (k, v) in [("AI", "人工智能"), ("职业发展", "程序员"), ("转型", "程序员"), ("行业观察", "程序员"),
                       ("求职面试", "面试"), ("企业管理", "团队管理"), ("项目管理", "团队管理"),
                       ("商业模式", "团队管理"), ("系统架构", "架构"), ("能力模型", "架构")] {
            fb.insert(k.to_string(), Some(v.to_string()));
        }
        for k in ["FDE", "企业服务", "工业互联网"] {
            fb.insert(k.to_string(), None);
        }
        let tags = crate::juejin::resolve_tags(j, &tag_names, &known, &fb);

        // 专栏匹配：大小写不敏感 + 去空白/连字符/点号/下划线（r2r-jev 要能匹配 R2R × Jev：判断之后的治理）
        let column_id = match &picked.column {
            Some(c) => {
                let norm = |s: &str| s.to_lowercase()
                    .chars().filter(|x| !x.is_whitespace() && *x != '-' && *x != '_' && *x != '.')
                    .collect::<String>();
                let target = norm(c);
                let cols = j.columns(uid).unwrap_or_default();
                let hit = cols.iter().find(|(_, t)| {
                    let n = norm(t);
                    n == target || n.contains(&target) || target.contains(&n)
                });
                match hit {
                    Some((id, t)) => {
                        log.push(format!("[专栏] {} → {}", trunc(c, 20), trunc(t, 20)));
                        Some(id.clone())
                    }
                    None => {
                        log.push(format!("[专栏] 「{}」未匹配到任何专栏（不挂专栏发布）", trunc(c, 20)));
                        None
                    }
                }
            }
            None => None,
        };

        let draft = j.create_draft(&title, &brief, &body, &category, &tags);
        match draft.and_then(|did| {
            match upload_cover_via_plugin(&did, &picked.file) {
                Some(u) => log.push(format!("[封面] ✓ …{}", trunc_tail(&u, 30))),
                None => log.push("[封面] 跳过（无 Python 插件或上传失败）".into()),
            }
            j.publish(&did, column_id.as_deref(), body.chars().count())
        }) {
            Ok(aid) => {
                self.state["entries"][&picked.id]["platforms"]["juejin"] = json!({
                    "published_at": chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string(),
                    "published_ts": now_ts(), "title": &title, "sha": Self::fingerprint(p),
                    "article_id": &aid});
                self.set_entry(&picked.id, "in_review");
                self.event("publish.ok", json!({"slug": &picked.id, "platform": "juejin",
                    "article_id": &aid, "run_id": run_id, "engine": "rust"}));
                log.push(format!("[发布] ✓ https://juejin.cn/post/{aid}（审核中）"));
                let _ = toast(&format!("✓ 已发布：{}", picked.id), &format!("juejin.cn/post/{aid}"));
            }
            Err(e) => {
                let kind = if e.0.contains("401") || e.0.contains("403") { "COOKIE_EXPIRED" } else { "PLATFORM_ERROR" };
                self.state["entries"][&picked.id]["platforms"]["juejin"]["kind"] = json!(kind);
                self.state["entries"][&picked.id]["platforms"]["juejin"]["last_error"] = json!(trunc(&e.0, 300));
                self.set_entry(&picked.id, if kind == "COOKIE_EXPIRED" { "held" } else { "failed" });
                self.event("publish.failed", json!({"slug": &picked.id, "kind": kind, "run_id": run_id}));
                log.push(format!("[发布] ✗ {kind}: {}", e.0));
                let _ = toast(&format!("✗ 发布失败：{}", picked.id), &trunc(&e.0, 60));
            }
        }
        let _ = self.save();
        Ok(())
    }

    /// 立即发布指定 slug（可多篇）：跳过 at 时间与队列顺序，保留状态/标题去重护栏
    pub fn publish_now(&mut self, slugs: Vec<String>) -> Result<String, Err2> {
        let _lock = match AppLock::acquire() {
            Some(l) => l,
            None => return Ok("[锁] 另一发布进程正在运行，本次跳过".into()),
        };
        let run_id = format!("{}-now{:04x}", chrono::Local::now().format("%m%d%H%M"),
                             (now_ts() * 1000.0) as u32 & 0xffff);
        self.event("pipeline.start", json!({"run_id": &run_id, "pipeline": "publish-now", "dry": false}));
        let mut log = vec![];
        let j = Juejin::load().map_err(Err2::from)?;
        let (uid, _name) = j.whoami().map_err(Err2::from)?;
        let arts = j.recent_articles(&uid).map_err(Err2::from)?;
        let titles: std::collections::HashSet<String> = arts.iter().map(|a| a.0.clone()).collect();
        let items = self.plan_items()?;
        for slug in slugs {
            let Some(it) = items.iter().find(|i| i.id == slug) else {
                log.push(format!("[立即] {} 不在计划里——先加入编排", slug));
                continue;
            };
            let st = self.entry_status(&slug);
            if TERMINAL.contains(&st.as_str()) || ["in_review", "partial"].contains(&st.as_str()) {
                log.push(format!("[立即] {} 跳过（状态 {}）", slug, st));
                continue;
            }
            let p = Path::new(&it.file);
            let tj = Self::parse_post(p).ok()
                .and_then(|(f, _)| f.get("title_juejin").cloned()).unwrap_or_default();
            if titles.contains(&tj) || self.frozen_titles().contains(&tj) {
                log.push(format!("[立即] {} 跳过（标题已发布，防重发）", slug));
                continue;
            }
            if let Err(e) = self.publish_one(it, &j, &uid, &run_id, &mut log) {
                log.push(format!("[立即] {} 失败：{}", slug, e.0));
            }
        }
        let _ = self.save();
        let items2 = self.plan_items()?;
        self.finish(&items2, &run_id, false);
        let _ = self.save();
        Ok(log.join("
"))
    }

    fn frozen_titles(&self) -> std::collections::HashSet<String> {
        // 只冻结「实际发布过」的标题（有 article_id）——意图写入的标题不算，
        // 否则发布失败的条目重试时会自己拦自己（e2e-cover3 实测踩中）
        let mut out = std::collections::HashSet::new();
        if let Some(obj) = self.state["entries"].as_object() {
            for v in obj.values() {
                let j = &v["platforms"]["juejin"];
                let has_id = j["article_id"].as_str().map(|s| !s.is_empty()).unwrap_or(false);
                if has_id {
                    if let Some(t) = j["title"].as_str() {
                        out.insert(t.to_string());
                    }
                }
            }
        }
        out
    }

    fn finish(&mut self, items: &[PlanItem], run_id: &str, dry: bool) {
        if dry { return; }
        // held（如 Cookie 失效）是「等待人工」而非「队列完结」——不能据此停用心跳，
        // 否则一次登录态失效永久杀死队列（检视确认 major）
        let all_done = !items.is_empty() && items.iter().all(|it|
            ["live", "closed", "rejected"].contains(&self.entry_status(&it.id).as_str()));
        let has_held = items.iter().any(|it| self.entry_status(&it.id) == "held");
        if has_held && !all_done {
            let _ = toast("⏸ 有条目等待人工处理（held）", "心跳保持运行——处理后自动继续");
            return;
        }
        if all_done {
            for tn in ["BlogAgent_hourly"] {
                let _ = std::process::Command::new("schtasks")
                    .args(["/Change", "/TN", tn, "/DISABLE"]).creation_flags(0x0800_0000).spawn();
            }
            self.event("queue.done-disabled-tasks", json!({"tasks": "BlogAgent_hourly", "run_id": run_id}));
            let _ = toast("🎉 队列发完，心跳已停用", "重新排期时部署再启用");
        }
    }

    /// 显示用标题：文件在→frontmatter 的 title_juejin；文件没了→state 冻结标题兜底
    fn title_of(&self, it: &PlanItem, e: &Value) -> String {
        let p = Path::new(&it.file);
        if p.exists() {
            if let Ok((fields, _)) = Self::parse_post(p) {
                if let Some(t) = fields.get("title_juejin") {
                    if !t.is_empty() { return t.clone(); }
                }
            }
        }
        e["platforms"]["juejin"]["title"].as_str().unwrap_or("").to_string()
    }

    /// 文件级元数据（标签/分类/摘要）——文件在则读 frontmatter，不在回退 state
    fn meta_of(&self, it: &PlanItem) -> (String, String, String) {
        let p = Path::new(&it.file);
        if p.exists() {
            if let Ok((fields, _)) = Self::parse_post(p) {
                return (
                    fields.get("tags").cloned().unwrap_or_default(),
                    fields.get("category_id").cloned().unwrap_or_default(),
                    fields.get("description").cloned().unwrap_or_default(),
                );
            }
        }
        (String::new(), String::new(), String::new())
    }

    /// 原生自检：凭据真验证（whoami）+ 计划完整性（只查非终态条目的文件）+ 心跳任务
    pub fn doctor(&mut self) -> Result<String, Err2> {
        let mut lines = vec![];
        let mut ok = true;
        match Juejin::load().map_err(Err2::from).and_then(|j| j.whoami().map_err(Err2::from)) {
            Ok((_uid, name)) => lines.push(format!("掘金登录态：✓ 有效（{name}）")),
            Err(e) => { lines.push(format!("掘金登录态：✗ {}（扫码登录）", e.0)); ok = false; }
        }
        let items = self.plan_items()?;
        let mut bad = 0;
        for it in &items {
            // 已完结的条目（live/closed/rejected）不再依赖文件——历史记录允许文件已归档/改名
            if TERMINAL.contains(&self.entry_status(&it.id).as_str()) { continue; }
            if !Path::new(&it.file).exists() {
                lines.push(format!("  ✗ {} 文件缺失：{}", it.id, it.file));
                bad += 1; ok = false;
            }
        }
        lines.push(format!("计划：{} 条（待发 {}）；文件检查{}",
            items.len(),
            items.iter().filter(|i| !TERMINAL.contains(&self.entry_status(&i.id).as_str())).count(),
            if bad == 0 { " ✓".to_string() } else { format!("：{bad} 处缺失") }));
        let hb = heartbeat_task_state();
        lines.push(match hb.as_str() {
            "running" => "心跳任务：✓ 每小时在跑".to_string(),
            "disabled" => "心跳任务：⏸ 已停用（有新计划时部署恢复）".to_string(),
            _ => { ok = false; "心跳任务：✗ 未部署（计划没人定时执行）".to_string() }
        });
        lines.push(if ok { "自检通过 ✓".into() } else { "自检有问题 ✗".into() });
        Ok(lines.join("\n"))
    }

    /// status --json 等价物（rows/gates/pipeline/heartbeat_task）
    pub fn status_json(&mut self) -> Result<String, Err2> {
        let items = self.plan_items()?;
        let mut rows = vec![];
        for it in &items {
            let e = &self.state["entries"][&it.id];
            let (tags, cat, desc) = self.meta_of(it);
            rows.push(json!({
                "slug": &it.id, "status": e["status"].as_str().unwrap_or("pending"),
                "file": &it.file, "juejin_at": &it.juejin_at,
                "title_juejin": self.title_of(it, e),
                "column": it.column.clone().unwrap_or_default(),
                "tags": tags, "category_id": cat, "description": desc,
                "juejin": plat_brief(e, "juejin"),
            }));
        }
        let hb = heartbeat_task_state();
        let gates = if self.state["gates"].is_null() { json!({}) } else { self.state["gates"].clone() };
        let pipeline = if self.state["last_pipeline"].is_null() { json!({}) } else { self.state["last_pipeline"].clone() };
        Ok(json!({"rows": rows, "gates": gates, "pipeline": pipeline, "heartbeat_task": hb}).to_string())
    }
}

pub fn plat_brief(e: &Value, p: &str) -> String {
    let d = &e["platforms"][p];
    if d.is_null() { return "—".into(); }
    if d["live"].as_bool() == Some(true) {
        let id = d["article_id"].as_str().unwrap_or("");
        let url = d["url"].as_str().unwrap_or("");
        let s = if id.is_empty() { url } else { id };
        return format!("live …{}", trunc_tail(s, 16));
    }
    if let Some(a) = d["article_id"].as_str() {
        if !a.is_empty() { return format!("in_review …{}", &a[a.len().saturating_sub(10)..]); }
    }
    d["kind"].as_str().unwrap_or("pending").chars().take(20).collect()
}

pub fn heartbeat_task_state() -> String {
    let out = std::process::Command::new("schtasks")
        .args(["/Query", "/TN", "BlogAgent_hourly", "/FO", "LIST"])
        .creation_flags(0x0800_0000).output();
    match out {
        Ok(o) if o.status.success() => {
            // schtasks 中文输出是 GBK：utf8 lossy 下「已禁用」不可靠，用 PowerShell 直接问状态
            let ps = "(schtasks /Query /TN BlogAgent_hourly /FO LIST | Out-String) -match '已禁用|Disabled' | ForEach-Object { if ($_) { 'disabled' } else { 'running' } }";
            let st = std::process::Command::new("powershell")
                .args(["-NoProfile", "-Command", ps])
                .creation_flags(0x0800_0000).output();
            match st {
                Ok(o) if o.status.success() => {
                    let s = String::from_utf8_lossy(&o.stdout).trim().to_string();
                    if s == "disabled" { "disabled".into() } else { "running".into() }
                }
                _ => "running".into(),
            }
        }
        _ => "missing".into(),
    }
}

/// 封面插件：调 publisher_agent.cover 上传进草稿，返回 COVER_URL（无 Python/失败→None，发布继续）
fn upload_cover_via_plugin(draft_id: &str, md_path: &str) -> Option<String> {
    let root = agent_root();
    let venv_py = root.join(".venv").join("Scripts").join("python.exe");
    if !venv_py.exists() {
        return None;
    }
    let mut child = std::process::Command::new(&venv_py)
        .args(["-m", "publisher_agent.cover", draft_id, md_path])
        .current_dir(&root)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .creation_flags(0x0800_0000)
        .spawn()
        .ok()?;
    // Playwright 卡死会让 tick 永久挂起、后续心跳全部被跳过（检视 major）——240s 硬超时
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(240);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if std::time::Instant::now() > deadline => {
                let _ = child.kill();
                return None;
            }
            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(500)),
            Err(_) => return None,
        }
    }
    let out = child.wait_with_output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    text.lines().find(|l| l.starts_with("COVER_URL "))
        .and_then(|l| l.splitn(2, ' ').nth(1).map(|s| s.trim().to_string()))
}

/// Windows toast（与 Python 版同款 PowerShell 方案）
pub fn toast(title: &str, body: &str) -> Result<(), String> {
    // 换行必须剥：PS here-string 只在行首 '@ 终止，多行文本可能提前截断脚本（检视 minor）
    let esc = |s: &str| s.replace(['\n', '\r'], " ").replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;");
    let xml = format!("<toast><visual><binding template='ToastGeneric'><text>{}</text><text>{}</text></binding></visual></toast>",
                      esc(title), esc(body));
    let ps = format!(r#"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml(@'
{xml}
'@)
$t = New-Object Windows.UI.Notifications.ToastNotification($doc)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('publisher-agent').Show($t)"#);
    let _ = std::process::Command::new("powershell")
        .args(["-NoProfile", "-Command", &ps])
        .creation_flags(0x0800_0000).spawn();
    Ok(())
}
