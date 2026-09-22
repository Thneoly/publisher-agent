//! 掘金 API 客户端（Rust 原生，混合架构核心）。
//!
//! 口径全部来自 Python 引擎的真机验证（publish.py 2026-09）：
//! - 请求必须带 ?aid=2608&uuid=<真实设备uuid>（随机值被 WAF 掐）
//! - cookie 从引擎 .juejin.env 读（login 由 Python 插件的扫码完成）
//! - 建草稿响应 article_id 恒为 "0"，真实 ID 在 data.id
//! - 审核先看 audit_status（-1=驳回）再看 status（0=审核中/1,2=上线）

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

use serde_json::{json, Value};

pub const API: &str = "https://api.juejin.cn";
const UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";

pub struct Juejin {
    client: reqwest::blocking::Client,
    cookie: String,
    uuid: String,
    pub user_id: String,
}

#[derive(Debug)]
pub struct ApiError(pub String);

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

fn engine_root() -> PathBuf {
    if let Ok(p) = std::env::var("PUBLISHER_AGENT_ROOT") {
        let pb = PathBuf::from(&p);
        if pb.join("agent.yaml").exists() {
            return pb;
        }
    }
    root_fallback()
}

fn root_fallback() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap()
}

/// 引擎目录（Python 插件/旧数据的所在；自包含模式下仅作回退与迁移源）
pub fn engine_cwd() -> PathBuf {
    let root = engine_root();
    let yaml = std::fs::read_to_string(root.join("agent.yaml")).unwrap_or_default();
    yaml.lines()
        .find(|l| l.trim_start().starts_with("cwd:"))
        .and_then(|l| l.split("cwd:").nth(1))
        .map(|s| s.split('#').next().unwrap_or("").trim().to_string())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("D:/FDE/blog"))
}

impl Juejin {
    pub fn load() -> Result<Self, ApiError> {
        // ① App 自有存储（%APPDATA%\publisher-agent\data.db 的 meta 表）
        let (mut cookie, mut uuid, mut user_id) = (String::new(), String::new(), String::new());
        if let Ok(db) = rusqlite::Connection::open(crate::agent::app_data_root().join("data.db")) {
            let g = |k: &str| -> String {
                db.query_row("SELECT value FROM meta WHERE key=?1", [k], |r| r.get::<_, String>(0))
                    .unwrap_or_default()
            };
            cookie = g("juejin_cookie");
            uuid = g("juejin_uuid");
            user_id = g("juejin_user_id");
        }
        // ② 引擎文件优先级提升：登录（扫码）只写引擎 .juejin.env——
        // 若与 db 不同说明刚重新登录，以引擎为准并回写 db（否则旧 Cookie 恒生效，检视 minor）
        let engine_cookie = std::fs::read_to_string(engine_cwd().join(".juejin.env")).ok()
            .and_then(|raw| raw.lines().find(|l| l.starts_with("JUEJIN_COOKIE="))
                .map(|l| l.splitn(2, '=').nth(1).unwrap_or("").trim().to_string()))
            .unwrap_or_default();
        if !engine_cookie.is_empty() && engine_cookie != cookie {
            if let Ok(db) = rusqlite::Connection::open(crate::agent::app_data_root().join("data.db")) {
                let _ = db.execute("INSERT INTO meta(key, value) VALUES('juejin_cookie', ?1)
                    ON CONFLICT(key) DO UPDATE SET value=?1", [&engine_cookie]);
            }
            cookie = engine_cookie;
        }
        if cookie.is_empty() {
            let env = engine_cwd().join(".juejin.env");
            let raw = std::fs::read_to_string(&env)
                .map_err(|e| ApiError(format!("App 库与 {env:?} 都没有 Cookie：{e}（先跑 login）")))?;
            cookie = raw
                .lines()
                .find(|l| l.starts_with("JUEJIN_COOKIE="))
                .map(|l| l.splitn(2, '=').nth(1).unwrap_or("").trim().to_string())
                .ok_or(ApiError(".juejin.env 缺 JUEJIN_COOKIE".into()))?;
            if uuid.is_empty() {
                let meta: Value = serde_json::from_str(
                    &std::fs::read_to_string(engine_cwd().join("_wf/juejin_meta.json"))
                        .unwrap_or_else(|_| "{}".into()),
                ).unwrap_or(json!({}));
                uuid = meta["uuid"].as_str().unwrap_or("").to_string();
                user_id = meta["user_id"].as_str().unwrap_or("").to_string();
            }
        }
        Ok(Self {
            client: reqwest::blocking::Client::builder()
                .timeout(Duration::from_secs(30))
                .build()
                .map_err(|e| ApiError(e.to_string()))?,
            cookie,
            uuid,
            user_id,
        })
    }

    fn post(&self, path: &str, payload: Value) -> Result<Value, ApiError> {
        let url = format!("{API}{path}?aid=2608&uuid={}", self.uuid);
        let resp = self
            .client
            .post(&url)
            .header("User-Agent", UA)
            .header("Referer", "https://juejin.cn/")
            .header("Origin", "https://juejin.cn")
            .header("Cookie", &self.cookie)
            .json(&payload)
            .send()
            .map_err(|e| ApiError(format!("网络错误：{e}")))?;
        let status = resp.status();
        let body: Value = resp.json().map_err(|e| ApiError(format!("响应解析：{e}")))?;
        if !status.is_success() {
            return Err(ApiError(format!("HTTP {status}（401/403=Cookie 失效）")));
        }
        if body["err_no"].as_i64() != Some(0) {
            return Err(ApiError(format!(
                "[{}] {}",
                body["err_no"].as_i64().unwrap_or(-1),
                body["err_msg"].as_str().unwrap_or("?")
            )));
        }
        Ok(body["data"].clone())
    }

    /// whoami：真实验证 Cookie 有效性，返回 (user_id, user_name)。
    /// 必须校验 HTTP 状态 + err_no + 空值——掘金 Cookie 失效返回 200+err_no!=0+data=null，
    /// 不校验会得到 Ok(("","")) 让 doctor 误报「✓ 有效」（检视 major）。
    pub fn whoami(&self) -> Result<(String, String), ApiError> {
        let url = format!("{API}/user_api/v1/user/get?aid=2608&uuid={}", self.uuid);
        let resp = self
            .client
            .get(&url)
            .header("User-Agent", UA)
            .header("Referer", "https://juejin.cn/")
            .header("Cookie", &self.cookie)
            .send()
            .map_err(|e| ApiError(e.to_string()))?;
        let status = resp.status();
        let body: Value = resp.json().map_err(|e| ApiError(e.to_string()))?;
        if !status.is_success() {
            return Err(ApiError(format!("HTTP {status}（Cookie 失效或风控）")));
        }
        if body["err_no"].as_i64() != Some(0) {
            return Err(ApiError(format!("[{}] {}（Cookie 失效）",
                body["err_no"].as_i64().unwrap_or(-1), body["err_msg"].as_str().unwrap_or("?"))));
        }
        let d = &body["data"];
        let uid = d["user_id"].as_str().unwrap_or("").to_string();
        let name = d["user_name"].as_str().unwrap_or("").to_string();
        if uid.is_empty() {
            return Err(ApiError("user_id 为空（Cookie 失效）".into()));
        }
        Ok((uid, name))
    }

    /// 我的专栏清单：[(column_id, title)]
    pub fn columns(&self, uid: &str) -> Result<Vec<(String, String)>, ApiError> {
        let d = self.post(
            "/content_api/v1/column/self_center_list",
            json!({"user_id": uid, "cursor": "0", "keyword": "", "limit": 20}),
        )?;
        let items: Vec<Value> = if d.is_array() {
            d.as_array().cloned().unwrap_or_default()
        } else {
            d["data"].as_array().cloned().unwrap_or_default()
        };
        Ok(items
            .iter()
            .map(|x| {
                (
                    x["column"]["column_id"].as_str().unwrap_or("").to_string(),
                    x["column_version"]["title"].as_str().unwrap_or("").to_string(),
                )
            })
            .collect())
    }

    /// 全部分类 [(category_id, 名称)]——UI 下拉用（query_category_briefs 实测空，list 可用）
    pub fn categories(&self) -> Result<Vec<(String, String)>, ApiError> {
        let d = self.post(
            "/tag_api/v1/query_category_list",
            json!({"cursor": "0", "limit": 50, "key_word": ""}),
        )?;
        let items: Vec<Value> = if d.is_array() {
            d.as_array().cloned().unwrap_or_default()
        } else {
            d["data"].as_array().cloned().unwrap_or_default()
        };
        Ok(items.iter().filter_map(|x| {
            let id = x["category_id"].as_str().unwrap_or("").to_string();
            if id.is_empty() { return None; }
            Some((id, x["category"]["category_name"].as_str().unwrap_or("").to_string()))
        }).collect())
    }

    /// 已发文章（标题+article_id+ctime），发布时间倒序——对账与去重用
    pub fn recent_articles(&self, uid: &str) -> Result<Vec<(String, String, f64)>, ApiError> {
        let d = self.post(
            "/content_api/v1/article/list_by_user",
            json!({"user_id": uid, "cursor": "0", "limit": 30, "sort_type": 2}),
        )?;
        let items: Vec<Value> = if d.is_array() {
            d.as_array().cloned().unwrap_or_default()
        } else {
            d["data"].as_array().cloned().unwrap_or_default()
        };
        Ok(items
            .iter()
            .map(|x| {
                let a = &x["article_info"];
                (
                    a["title"].as_str().unwrap_or("").to_string(),
                    a["article_id"].as_str().unwrap_or("").to_string(),
                    a["ctime"].as_f64().unwrap_or(0.0),
                )
            })
            .collect())
    }

    /// 审核状态：("rejected"|"live"|"reviewing", 原文提示)
    pub fn audit_status(&self, article_id: &str) -> Result<(String, String), ApiError> {
        let d = self.post(
            "/content_api/v1/article/detail",
            json!({"article_id": article_id, "forbid_count": true}),
        )?;
        let info = &d["article_info"];
        let audit = info["audit_status"].as_i64().unwrap_or(0);
        let st = info["status"].as_i64().unwrap_or(0);
        if audit == -1 {
            return Ok(("rejected".into(), "audit_status=-1".into()));
        }
        if st == 1 || st == 2 {
            return Ok(("live".into(), format!("status={st}")));
        }
        Ok(("reviewing".into(), format!("status={st}")))
    }

    /// 标签搜索：query_tag_list（名字嵌在 tag.tag_name）
    pub fn search_tag(&self, keyword: &str) -> Result<Vec<(String, String)>, ApiError> {
        let d = self.post(
            "/tag_api/v1/query_tag_list",
            json!({"cursor": "0", "key_word": keyword, "limit": 8, "sort_type": 1}),
        )?;
        let items: Vec<Value> = if d.is_array() {
            d.as_array().cloned().unwrap_or_default()
        } else {
            d["data"].as_array().cloned().unwrap_or_default()
        };
        Ok(items
            .iter()
            .filter_map(|x| {
                let id = x["tag_id"].as_str().unwrap_or("").to_string();
                let id = if id.is_empty() {
                    x["tag_id"].as_i64().map(|v| v.to_string())?
                } else {
                    id
                };
                if id.is_empty() {
                    return None;
                }
                Some((x["tag"]["tag_name"].as_str().unwrap_or("").to_string(), id))
            })
            .collect())
    }

    /// 建草稿：返回真实 draft_id（article_id 恒 "0" 是未发布语义）
    pub fn create_draft(
        &self,
        title: &str,
        brief: &str,
        body: &str,
        category_id: &str,
        tag_ids: &[String],
    ) -> Result<String, ApiError> {
        let d = self.post(
            "/content_api/v1/article_draft/create",
            json!({
                "category_id": category_id,
                "tag_ids": tag_ids,
                "title": title,
                "brief_content": brief,
                "edit_type": 10,
                "mark_content": body,
                "cover_image": "",
                "html_content": "deprecated",
                "link_url": "",
                "theme_ids": [],
            }),
        )?;
        let id = d["id"].as_str().map(|s| s.to_string())
            .or_else(|| d["id"].as_i64().map(|v| v.to_string()))
            .unwrap_or_default();
        if id.is_empty() || id == "0" {
            return Err(ApiError("建草稿返回异常（data.id 空）".into()));
        }
        Ok(id)
    }


    /// 文章详情（含 column_ids——专栏挂载验证用）
    pub fn article_detail(&self, article_id: &str) -> Result<Value, ApiError> {
        self.post("/content_api/v1/article/detail",
            json!({"article_id": article_id, "forbid_count": true}))
    }

    /// 任意 POST 的公开包装（E2E 删除等管理动作用）
    pub fn post_raw(&self, path: &str, payload: Value) -> Result<Value, ApiError> {
        self.post(path, payload)
    }
    /// 删草稿（R2 安全闭环验证用）
    pub fn delete_draft(&self, draft_id: &str) -> Result<(), ApiError> {
        self.post("/content_api/v1/article_draft/delete", json!({"draft_id": draft_id}))?;
        Ok(())
    }

    /// 发布：column_ids 挂专栏；返回 article_id
    pub fn publish(&self, draft_id: &str, column_id: Option<&str>, word_count: usize) -> Result<String, ApiError> {
        let d = self.post(
            "/content_api/v1/article/publish",
            json!({
                "draft_id": draft_id,
                "sync_to_org": false,
                "column_ids": if column_id.is_some() { json!([column_id.unwrap()]) } else { json!([]) },
                "theme_ids": [],
                "origin_word_count": word_count,
                "encrypted_word_count": word_count,
            }),
        )?;
        let aid = d["article_id"].as_str().or_else(|| d["id"].as_str()).unwrap_or("");
        if aid.is_empty() {
            return Err(ApiError("发布响应缺 article_id".into()));
        }
        Ok(aid.to_string())
    }
}

/// 标签解析：KNOWN_TAGS 常表 + TAG_FALLBACK 映射（与 Python 引擎同口径）
pub fn resolve_tags(
    j: &Juejin,
    names: &[String],
    known: &HashMap<String, String>,
    fallback: &HashMap<String, Option<String>>,
) -> Vec<String> {
    let mut out: Vec<String> = vec![];
    for n in names {
        let target = match fallback.get(n.as_str()) {
            Some(Some(t)) => t.clone(),
            Some(None) => continue,
            None => n.clone(),
        };
        if let Some(id) = known.get(&target) {
            if !out.contains(id) {
                out.push(id.clone());
            }
        } else if let Ok(found) = j.search_tag(&target) {
            if let Some((_, id)) = found.first() {
                if !out.contains(id) {
                    out.push(id.clone());
                }
            }
        }
    }
    if out.len() == 1 {
        if let Some(id) = known.get("人工智能") {
            if !out.contains(id) {
                out.push(id.clone());
            }
        }
    }
    out.truncate(2);
    out
}
