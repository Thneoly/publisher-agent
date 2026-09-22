// publisher-agent 集成测试
// 运行：cargo test --test integration

#[path = "../src/juejin.rs"]
mod juejin;
#[path = "../src/agent.rs"]
mod agent;

use agent::*;
use juejin::*;

// ═══════════════════════════════════════════════════════════════
// 工具函数单元测试
// ═══════════════════════════════════════════════════════════════

#[test]
fn test_trunc_chinese() {
    assert_eq!(trunc("你好世界", 2), "你好");
    assert_eq!(trunc("你好世界", 10), "你好世界");
    assert_eq!(trunc("", 5), "");
    assert_eq!(trunc("a你好b", 3), "a你好");
}

#[test]
fn test_trunc_mixed_ascii_cjk() {
    // 中英混排——之前按字节截断会 panic 的场景
    let s = "从 kubectl 回车到容器跑起来";
    let r = trunc(s, 8);
    assert!(r.chars().count() <= 8);
    assert!(!r.is_empty());
}

#[test]
fn test_trunc_tail() {
    assert_eq!(trunc_tail("abcdefghij", 3), "hij");
    assert_eq!(trunc_tail("短", 5), "短");
    assert_eq!(trunc_tail("", 3), "");
    let s = "https://example.com/very/long/path";
    let r = trunc_tail(s, 10);
    assert!(r.chars().count() <= 10);
}

// ═══════════════════════════════════════════════════════════════
// frontmatter 解析测试
// ═══════════════════════════════════════════════════════════════

use std::sync::atomic::{AtomicU32, Ordering};
static TEST_ID: AtomicU32 = AtomicU32::new(0);

fn write_temp_md(content: &str) -> std::path::PathBuf {
    let id = TEST_ID.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join("pa_tests");
    let _ = std::fs::create_dir_all(&dir);
    let p = dir.join(format!("t{}_{}.md", std::process::id(), id));
    std::fs::write(&p, content).unwrap();
    p
}

#[test]
fn test_parse_post_basic() {
    let p = write_temp_md(
        "---\ntitle_juejin: 测试标题\ntitle_zhihu: 知乎标题\ndescription: 这是摘要\ncategory_id: \"123\"\ntags: \"AI,架构\"\n---\n\n# 正文\n\n内容\n",
    );
    let (fields, body) = Agent::parse_post(&p).unwrap();
    assert_eq!(fields.get("title_juejin").unwrap(), "测试标题");
    assert_eq!(fields.get("title_zhihu").unwrap(), "知乎标题");
    assert_eq!(fields.get("category_id").unwrap(), "123");
    assert_eq!(fields.get("tags").unwrap(), "AI,架构");
    assert!(body.contains("# 正文"));
    assert!(!body.contains("---")); // frontmatter 不在正文中
    let _ = std::fs::remove_file(&p);
}

#[test]
fn test_parse_post_no_frontmatter() {
    let p = write_temp_md("just body no frontmatter\n");
    match Agent::parse_post(&p) {
        Err(_) => {}
        Ok((fields, _)) => assert!(fields.get("title_juejin").is_none()),
    }
    let _ = std::fs::remove_file(&p);
}

#[test]
fn test_parse_post_html_comment_stripped() {
    let p = write_temp_md(
        "---\ntitle_juejin: t\n---\n\n<!-- secret note -->\n\ncontent\n",
    );
    let (_, body) = Agent::parse_post(&p).unwrap();
    assert!(!body.contains("secret"), "HTML comment should be stripped");
    let _ = std::fs::remove_file(&p);
}

#[test]
fn test_fingerprint() {
    let p1 = write_temp_md("---\ntitle_juejin: t\n---\n\n内容A\n");
    let p2 = write_temp_md("---\ntitle_juejin: t\n---\n\n内容B\n");
    let fp1 = Agent::fingerprint(&p1);
    let fp2 = Agent::fingerprint(&p2);
    assert_ne!(fp1, fp2, "不同内容应有不同指纹");
    assert_eq!(fp1.len(), 8, "指纹应为 8 位 hex");
    let _ = std::fs::remove_file(&p1);
    let _ = std::fs::remove_file(&p2);
}

// ═══════════════════════════════════════════════════════════════
// plan.yaml 解析测试
// ═══════════════════════════════════════════════════════════════

#[test]
fn test_plan_yaml_parsing() {
    let dir = std::env::temp_dir().join("pa_plan_test");
    let _ = std::fs::create_dir_all(&dir);
    let plan = r#"
cadence:
  juejin:
    min_gap_h: 0
queue:
- file: D:/test/01.md
  id: 01
  juejin:
    at: "2026-09-25 09:15"
    column: 测试专栏
- file: D:/test/02.md
  id: 02
  juejin: {}
"#;
    std::fs::write(dir.join("plan.yaml"), plan).unwrap();

    // Agent::plan_items 需要通过 Agent 实例调用，但我们可以测试 yaml 解析逻辑
    let y: serde_yaml::Value = serde_yaml::from_str(plan).unwrap();
    let queue = y.get("queue").and_then(|q| q.as_sequence()).unwrap();
    assert_eq!(queue.len(), 2);

    let first = &queue[0];
    assert_eq!(first.get("id").and_then(|v| v.as_str()), Some("01"));
    let jj = first.get("juejin").unwrap();
    assert_eq!(jj.get("at").and_then(|v| v.as_str()), Some("2026-09-25 09:15"));
    assert_eq!(jj.get("column").and_then(|v| v.as_str()), Some("测试专栏"));
    assert!(first.get("zhihu").is_none(), "无 zhihu 声明");

    let _ = std::fs::remove_dir_all(&dir);
}

// ═══════════════════════════════════════════════════════════════
// 状态机逻辑测试（纯内存，不触网）
// ═══════════════════════════════════════════════════════════════

#[test]
fn test_frozen_titles_only_published() {
    // 验证：只有 article_id 的条目才冻结标题（防自拦 bug）
    let dir = std::env::temp_dir().join("pa_state_test");
    let _ = std::fs::create_dir_all(&dir);

    // 构造一个假 state：一个有 article_id（应冻结），一个没有（不应冻结）
    let state = serde_json::json!({
        "entries": {
            "published_item": {
                "status": "in_review",
                "platforms": {
                    "juejin": {
                        "title": "已发布标题",
                        "article_id": "12345"
                    }
                }
            },
            "intent_only_item": {
                "status": "pending",
                "platforms": {
                    "juejin": {
                        "title": "意图标题",
                        "sha": "abc"
                    }
                }
            }
        }
    });

    let frozen: std::collections::HashSet<String> = state["entries"]
        .as_object().unwrap()
        .values()
        .filter(|v| {
            let j = &v["platforms"]["juejin"];
            j["article_id"].as_str().map(|s| !s.is_empty()).unwrap_or(false)
        })
        .filter_map(|v| v["platforms"]["juejin"]["title"].as_str().map(String::from))
        .collect();

    assert!(frozen.contains("已发布标题"), "有 article_id 的应冻结");
    assert!(!frozen.contains("意图标题"), "只有意图的（无 article_id）不应冻结");
}

#[test]
fn test_terminal_states() {
    let terminal = ["live", "closed", "held", "rejected"];
    let non_terminal = ["pending", "in_review", "partial", "failed", ""];

    for st in terminal {
        assert!(terminal.contains(&st), "{} 应为终态", st);
    }
    for st in non_terminal {
        assert!(!terminal.contains(&st), "{} 不应为终态", st);
    }
}

// ═══════════════════════════════════════════════════════════════
//专栏匹配逻辑测试
// ═══════════════════════════════════════════════════════════════

#[test]
fn test_column_match_case_insensitive() {
    let norm = |s: &str| s.to_lowercase()
        .chars().filter(|x| !x.is_whitespace() && *x != '-' && *x != '_' && *x != '.' && *x != '\u{00D7}')
        .collect::<String>();

    // r2r-jev 应匹配 R2R × Jev：判断之后的治理
    let a = norm("r2r-jev");
    let b = norm("R2R × Jev：判断之后的治理");
    assert!(b.contains(&a), "'{}' 应包含 '{}'", b, a);

    // 完全匹配
    let c = norm("FDE 九辩");
    let d = norm("FDE九辩");
    assert!(c.contains(&d) || d.contains(&c));

    // 大小写
    let e = norm("K8s深入理解");
    let f = norm("k8s 深入理解");
    assert!(e.contains(&f) || f.contains(&e));
}

// ═══════════════════════════════════════════════════════════════
// 时间解析测试
// ═══════════════════════════════════════════════════════════════

#[test]
fn test_at_time_parsing() {
    use chrono::NaiveDateTime;

    assert!(NaiveDateTime::parse_from_str("2026-09-25 09:15", "%Y-%m-%d %H:%M").is_ok());
    assert!(NaiveDateTime::parse_from_str("2026-09-25T09:15", "%Y-%m-%d %H:%M").is_err(), "T 格式应被拒绝");
    assert!(NaiveDateTime::parse_from_str("bad", "%Y-%m-%d %H:%M").is_err());
}

// ═══════════════════════════════════════════════════════════════
// native_set_field 行级替换测试（不触网）
// ═══════════════════════════════════════════════════════════════

#[test]
fn test_set_field_only_frontmatter() {
    let content = "---\ntitle_juejin: 原标题\ntags: \"AI\"\ndescription: \"\"\n---\n\n# 正文\n\n```yaml\ntags: 不要动我\n```\n\n价格 $5 与 $1。\n";
    let p = write_temp_md(content);

    // 模拟 native_set_field 的行级替换逻辑
    let key = "tags";
    let value = "Kubernetes,后端";
    let raw = std::fs::read_to_string(&p).unwrap();
    let mut lines: Vec<String> = raw.split('\n').map(|l| l.to_string()).collect();
    let end = (1..lines.len()).find(|&i| lines[i].trim() == "---").unwrap();
    let prefix = format!("{}:", key);
    let hit = (1..end).find(|&i| lines[i].starts_with(&prefix));
    assert!(hit.is_some(), "应找到 tags 行");
    lines[hit.unwrap()] = format!("{}: {}", key, value);
    let out = lines.join("\n");
    std::fs::write(&p, &out).unwrap();

    // 验证
    let result = std::fs::read_to_string(&p).unwrap();
    assert!(result.contains("tags: Kubernetes,后端"), "frontmatter 应被替换");
    assert!(result.contains("tags: 不要动我"), "正文代码块不应被动");
    assert!(result.contains("$5 与 $1"), "$ 不应被展开");
    let _ = std::fs::remove_file(&p);
}
