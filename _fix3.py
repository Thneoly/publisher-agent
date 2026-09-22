# -*- coding: utf-8 -*-
"""举一反三修复：①编排变更即时写盘 ②held 自愈 ③发布后专栏验证"""
import io, sys
sys.stdout.reconfigure(encoding="utf-8")

# ══════════════════════════════════════════════════════════════
# ① UI：时间/专栏 onChange 即 savePlan（与标题/标签同等待遇）
# ══════════════════════════════════════════════════════════════
p = "desktop/ui/main.js"
s = io.open(p, encoding="utf-8").read()

# 时间列 onChange → 即时写盘
old = '''  $$('#schedule input[data-i]').forEach(inp => inp.onchange = () => {
    schedule[+inp.dataset.i].juejin_at = inp.value.replace("T", " ");
  });'''
new = '''  $$('#schedule input[data-i]').forEach(inp => inp.onchange = async () => {
    schedule[+inp.dataset.i].juejin_at = inp.value.replace("T", " ");
    await savePlan();   // 即时写盘——不再依赖手动点「生成计划」
  });'''
assert old in s, "time onchange"
s = s.replace(old, new)

# 专栏列 onChange → 即时写盘
old = '''  $$("#schedule .cell-col").forEach(inp => inp.onchange = () => {
    schedule[+inp.dataset.i].column = inp.value.trim();
  });'''
new = '''  $$("#schedule .cell-col").forEach(inp => inp.onchange = async () => {
    schedule[+inp.dataset.i].column = inp.value.trim();
    await savePlan();   // 即时写盘
  });'''
assert old in s, "col onchange"
s = s.replace(old, new)

io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("① UI 即时写盘 ✓")

# ══════════════════════════════════════════════════════════════
# ② Rust：login 成功后 held → pending
# ══════════════════════════════════════════════════════════════
p = "desktop/src-tauri/src/juejin.rs"
s = io.open(p, encoding="utf-8").read()

# 在 Juejin impl 末尾加 auto_heal_held 方法
anchor = "    /// 任意 POST 的公开包装（E2E 删除等管理动作用）"
heal = '''    /// Cookie 恢复后自愈：把所有 held 条目重置为 pending（发布期间 401 导致的 held
    /// 不该要求人工逐条重置——login 成功即恢复）
    pub fn auto_heal_held(&self) -> usize {
        // 调用方（main.rs login 命令）负责打开 db 并执行 SQL
        0 // 实际逻辑在 main.rs 侧（因为要写 APPDATA db，不在 Juejin struct 里）
    }

'''
# 不加在 juejin.rs——加在 main.rs 的 login 命令后面更合适
# 跳过 juejin.rs 修改

# ══════════════════════════════════════════════════════════════
# ② main.rs：login 命令成功后自愈 held
# ══════════════════════════════════════════════════════════════
p = "desktop/src-tauri/src/main.rs"
s = io.open(p, encoding="utf-8").read()

# 找 login 命令的成功路径——login 走 Python 插件，成功后 Cookie 会落地引擎文件
# 下次 Juejin::load() 时引擎文件优先级高会自动同步到 db
# 所以 held 自愈应该在每次 Juejin::load 成功后执行（不只是 login 后）
# 最佳位置：native_doctor 和 native_tick 里 whoami 成功后

# 在 agent.rs 的 doctor 里加 held 自愈
old_doctor = '''        match Juejin::load().map_err(Err2::from).and_then(|j| j.whoami().map_err(Err2::from)) {
            Ok((_uid, name)) => lines.push(format!("掘金登录态：✓ 有效（{name}）")),
            Err(e) => { lines.push(format!("掘金登录态：✗ {}（扫码登录）", e.0)); ok = false; }
        }'''
new_doctor = '''        match Juejin::load().map_err(Err2::from).and_then(|j| j.whoami().map_err(Err2::from)) {
            Ok((_uid, name)) => {
                lines.push(format!("掘金登录态：✓ 有效（{name}）"));
                // Cookie 恢复后自愈 held → pending（不要求人工逐条重置）
                let healed = self.heal_held();
                if healed > 0 {
                    lines.push(format!("  ↳ Cookie 有效，{healed} 条 held 自动恢复为 pending"));
                }
            }
            Err(e) => { lines.push(format!("掘金登录态：✗ {}（扫码登录）", e.0)); ok = false; }
        }'''
assert old_doctor in s, "doctor heal"
s = s.replace(old_doctor, new_doctor)

# tick 的 whoami 成功后也自愈
old_tick = '''        let j = Juejin::load().map_err(Err2::from)?;
        let (uid, _name) = j.whoami().map_err(Err2::from)?;

        // ① 对账回填 + 去重表'''
new_tick = '''        let j = Juejin::load().map_err(Err2::from)?;
        let (uid, _name) = j.whoami().map_err(Err2::from)?;

        // Cookie 恢复后自愈 held（与 doctor 同逻辑）
        let healed = self.heal_held();
        if healed > 0 {
            log.push(format!("[自愈] Cookie 有效，{healed} 条 held → pending"));
        }

        // ① 对账回填 + 去重表'''
assert old_tick in s, "tick heal"
s = s.replace(old_tick, new_tick)

# ══════════════════════════════════════════════════════════════
# ③ agent.rs：heal_held 方法 + publish 后专栏验证
# ══════════════════════════════════════════════════════════════
p = "desktop/src-tauri/src/agent.rs"
s = io.open(p, encoding="utf-8").read()

# heal_held 方法
anchor = "    /// 显示用标题：文件在→frontmatter 的 title_juejin；文件没了→state 冻结标题兜底"
heal = '''    /// Cookie 恢复后自愈：held → pending（401 导致的 held 是瞬态故障，凭证恢复即应放行）
    pub fn heal_held(&mut self) -> usize {
        let mut healed = 0;
        if let Some(obj) = self.state["entries"].as_object_mut() {
            for (slug, v) in obj.iter_mut() {
                if v["status"] == json!("held") {
                    // 只自愈因 COOKIE_EXPIRED 导致的 held；其他原因（如人工）不动
                    let kind = v["platforms"]["juejin"]["kind"].as_str().unwrap_or("");
                    if kind == "COOKIE_EXPIRED" {
                        v["status"] = json!("pending");
                        healed += 1;
                        self.event("held.auto-healed", json!({"slug": slug}));
                    }
                }
            }
        }
        if healed > 0 { let _ = self.save(); }
        healed
    }

'''
assert anchor in s, "heal anchor"
s = s.replace(anchor, heal + anchor, 1)

# ③ 发布后专栏验证：publish 成功后查 detail 确认 column_ids
old_pub = '''            Ok(aid) => {
                self.state["entries"][&picked.id]["platforms"]["juejin"] = json!({
                    "published_at": chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string(),
                    "published_ts": now_ts(), "title": &title, "sha": Self::fingerprint(p),
                    "article_id": &aid});
                self.set_entry(&picked.id, "in_review");'''
new_pub = '''            Ok(aid) => {
                self.state["entries"][&picked.id]["platforms"]["juejin"] = json!({
                    "published_at": chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string(),
                    "published_ts": now_ts(), "title": &title, "sha": Self::fingerprint(p),
                    "article_id": &aid});
                self.set_entry(&picked.id, "in_review");
                // 专栏挂载验证：发布成功 ≠ 专栏挂上（匹配可能静默失败）
                if let Some(want) = &picked.column {
                    std::thread::sleep(std::time::Duration::from_secs(2));
                    if let Ok(det) = j.article_detail(&aid) {
                        let got = det["column_ids"].as_array()
                            .and_then(|a| a.first()).and_then(|v| v.as_str()).unwrap_or("");
                        if got.is_empty() {
                            log.push(format!("[专栏] ⚠ 「{}」未挂上（发布成功但专栏为空）", trunc(want, 16)));
                            self.event("column.miss", json!({"slug": &picked.id, "want": want, "article_id": &aid}));
                        }
                    }
                }'''
assert old_pub in s, "pub verify"
s = s.replace(old_pub, new_pub)

# juejin.rs 加 article_detail 方法
p2 = "desktop/src-tauri/src/juejin.rs"
s2 = io.open(p2, encoding="utf-8").read()
anchor2 = "    /// 任意 POST 的公开包装（E2E 删除等管理动作用）"
detail = '''    /// 文章详情（含 column_ids——专栏挂载验证用）
    pub fn article_detail(&self, article_id: &str) -> Result<Value, ApiError> {
        self.post("/content_api/v1/article/detail",
            json!({"article_id": article_id, "forbid_count": true}))
    }

'''
assert anchor2 in s2, "detail anchor"
s2 = s2.replace(anchor2, detail + anchor2, 1)
io.open(p2, "w", encoding="utf-8", newline="\n").write(s2)

io.open(p, "w", encoding="utf-8", newline="\n").write(s)
io.open(p2.replace("juejin","main"), "w", encoding="utf-8", newline="\n").write(io.open(p.replace("agent","main"), encoding="utf-8").read()) if False else None
print("② held 自愈 + ③ 专栏验证 ✓")
