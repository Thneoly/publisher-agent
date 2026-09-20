const { invoke } = window.__TAURI__.core;
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

let found = [];        // 内容源扫描结果
let schedule = [];     // 编排中的条目 {id,file,name,title_juejin,juejin_at}

/* ---------------- Tab 切换 ---------------- */
$$(".tab").forEach(b => b.onclick = () => {
  $$(".tab").forEach(x => x.classList.remove("active"));
  $$(".panel").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  $("#tab-" + b.dataset.tab).classList.add("active");
  if (b.dataset.tab === "log") refreshLog();
});

/* ---------------- ① 内容源 ---------------- */
async function pick(dialogOpts) {
  try {
    return await invoke("plugin:dialog|open", { options: dialogOpts });
  } catch (err) {
    $("#plan-output").textContent = `✗ 选择器失败：${err}\n（capabilities/dialog 权限问题？看 _state/tick.log 排查）`;
    return null;
  }
}
$("#btn-folder").onclick = async () => scanSource(await pick({ directory: true, title: "选择文章文件夹" }));
$("#btn-file").onclick = async () => {
  const f = await pick({ multiple: true, filters: [{ name: "Markdown", extensions: ["md"] }] });
  if (Array.isArray(f)) for (const p of f) await scanSource(p);
  else if (f) await scanSource(f);
};

async function scanSource(path) {
  if (!path) return;
  $("#source-path").textContent = path;
  try {
    const out = await invoke("native_scan", { path });
    const rows = JSON.parse(out.slice(out.indexOf("[")));
    const known = new Set(found.map(f => f.file));       // 多文件夹累积，不互相覆盖
    found = found.concat(rows.filter(r => !known.has(r.file)));
    renderFound();
  } catch (err) { $("#plan-output").textContent = String(err); }
}
$("#btn-clear-found").onclick = () => { found = []; renderFound(); };
$("#sel-all").onchange = (e) => {
  $$('#found input[type=checkbox]:not(:disabled)').forEach(cb => cb.checked = e.target.checked);
};

const escA = (x) => String(x ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;")
  .replace(/</g, "&lt;").replace(/>/g, "&gt;");
function renderFound() {
  const tb = $("#found tbody");
  tb.innerHTML = "";
  for (const f of found) {
    const inSched = schedule.some(s => s.file === f.file);
    tb.innerHTML += `<tr>
      <td><input type="checkbox" ${inSched ? "disabled checked" : ""} data-file="${escA(f.file)}"></td>
      <td title="${escA(f.file)}">${escA(f.name)}</td>
      <td>${escA(f.folder || "—")}</td>
      <td>${escA((f.title_juejin || "").slice(0, 28))}</td>
      <td>${escA(f.chars)}</td></tr>`;
  }
}

$("#btn-to-plan").onclick = () => {
  let n = 0;
  $$('#found input[type=checkbox]:checked:not(:disabled)').forEach(cb => {
    const f = found.find(x => x.file === cb.dataset.file);
    if (!f) return;
    schedule.push({
      id: f.name.replace(/\.md$/, "").slice(0, 24),
      file: f.file, name: f.name, title_juejin: f.title_juejin,
      tags: f.tags || "", category_id: "", description: f.description || "",
      column: f.folder || "",
      juejin_at: "",
    });
    cb.disabled = true; n++;
  });
  renderSchedule();
  if (n) $$(".tab")[1].click();
};

const planSel = new Set();   // 跨重渲染保留的勾选（检视 minor）
function syncSelHeader() {
  const boxes = $$("#schedule .row-sel:not(:disabled)");
  $("#sel-plan").checked = boxes.length > 0 && boxes.every(b => b.checked);
}

/* ---------------- ② 编排（仅掘金） ---------------- */
function renderSchedule() {
  const tb = $("#schedule tbody");
  tb.innerHTML = "";
  schedule.forEach((s, i) => {
    const st = s.status ? `<span class="badge-st ${s.status}">${s.status}</span>` : "";
    const esc = (x) => String(x || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
    tb.innerHTML += `<tr>
      <td><input type="checkbox" class="row-sel" data-slug="${esc(s.id)}" ${s.status && !["pending"].includes(s.status) ? "disabled title=\"已完结/处理中\"" : ""} ${planSel.has(s.id) ? "checked" : ""}></td>
      <td title="${esc(s.file)}">${esc(s.name)} ${st}</td>
      <td><input type="text" class="cell-title" value="${esc(s.title_juejin)}" data-i="${i}"
                 placeholder="（取文件 frontmatter 标题）" title="改后写回 md 的 title_juejin"></td>
      <td class="td-tags" data-i="${i}" title="输入后回车添加；×移除。发布时自动映射掘金标签并取前 2 个">
        ${(s.tags || "").split(",").filter(Boolean).map(t =>
          `<span class="chip-tag">${esc(t)}<b class="x">×</b></span>`).join("")}
        <input class="tag-add" list="tag-list" placeholder="＋" />
        <datalist id="tag-list">
          <option>人工智能</option><option>面试</option><option>架构</option>
          <option>团队管理</option><option>程序员</option><option>AI编程</option>
        </datalist>
      </td>
      <td><input type="text" class="cell-col" value="${esc(s.column)}" data-i="${i}"
                 placeholder="（默认专栏）" title="发布时按此名模糊匹配掘金专栏；留空=默认"></td>
      <td><input type="datetime-local" value="${s.juejin_at}" data-i="${i}"></td>
      <td><button class="run1" data-i="${i}" title="立即发布这一篇（跳过排期时间，防重发护栏仍在）">▶</button>
          <button class="cfg" data-i="${i}" title="分类 / 摘要">🛠</button>
          <button class="del" data-i="${i}">✕</button></td></tr>`;
  });
  $$('#schedule input[data-i]').forEach(inp => inp.onchange = () => {
    schedule[+inp.dataset.i].juejin_at = inp.value.replace("T", " ");
  });
  $$("#schedule .cell-title").forEach(inp => inp.onchange = async () => {
    const i = +inp.dataset.i;
    const nt = inp.value.trim();
    if (!nt || nt === schedule[i].title_juejin) { inp.value = schedule[i].title_juejin; return; }
    if (!confirm(`把标题写回文件？\n《${nt}》→ ${schedule[i].name}`)) { inp.value = schedule[i].title_juejin; return; }
    try {
      await invoke("native_set_field", { file: schedule[i].file, key: "title_juejin", value: nt });
      schedule[i].title_juejin = nt;
      $("#plan-output").textContent = `✓ 标题已写回 ${schedule[i].name}（title_juejin）`;
    } catch (err) { $("#plan-output").textContent = `✗ ${err}`; inp.value = schedule[i].title_juejin; }
  });
  const writeTags = async (i) => {
    const v = (schedule[i].tags || "").split(",").filter(Boolean).join(",");
    try {
      await invoke("native_set_field", { file: schedule[i].file, key: "tags", value: v });
      schedule[i].tags = v;
      const n = v ? v.split(",").length : 0;
      $("#plan-output").textContent = `✓ 标签已写回 ${schedule[i].name}（${n} 个${n > 2 ? "，发布时自动映射取前 2" : ""}）`;
    } catch (err) { $("#plan-output").textContent = `✗ ${err}`; }
  };
  $$("#schedule .td-tags").forEach(td => {
    const i = +td.dataset.i;
    td.querySelectorAll(".chip-tag .x").forEach(x => x.onclick = async () => {
      const chip = x.parentElement;
      const t = chip.textContent.replace(/×$/, "").trim();
      schedule[i].tags = (schedule[i].tags || "").split(",").filter(Boolean)
        .filter(x2 => x2.trim() !== t).join(",");
      await writeTags(i); renderSchedule();
    });
    const add = td.querySelector(".tag-add");
    const commit = async () => {
      const v = add.value.trim().replace(/,+$/, "");
      add.value = "";
      if (!v) return;
      const cur = (schedule[i].tags || "").split(",").filter(Boolean);
      v.split(/[,，]/).map(x => x.trim()).filter(Boolean).forEach(t => {
        if (!cur.includes(t)) cur.push(t);
      });
      schedule[i].tags = cur.join(",");
      await writeTags(i); renderSchedule();
    };
    add.onkeydown = (e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(); } };
    add.onblur = commit;
  });
  $$("#schedule .cell-col").forEach(inp => inp.onchange = () => {
    schedule[+inp.dataset.i].column = inp.value.trim();
  });
  $$("#schedule .cfg").forEach(b => b.onclick = () => openFieldDialog(+b.dataset.i));
  $("#sel-plan").onchange = (e) => {
    $$("#schedule .row-sel:not(:disabled)").forEach(cb => {
      cb.checked = e.target.checked;
      if (e.target.checked) planSel.add(cb.dataset.slug); else planSel.delete(cb.dataset.slug);
    });
  };
  $$("#schedule .row-sel").forEach(cb => cb.onchange = () => {
    if (cb.checked) planSel.add(cb.dataset.slug); else planSel.delete(cb.dataset.slug);
    syncSelHeader();
  });
  $$("#schedule .run1").forEach(b => b.onclick = () => publishNow([schedule[+b.dataset.i].id]));
  $$("#schedule .del").forEach(b => b.onclick = () => {
    planSel.delete(schedule[+b.dataset.i].id);
    schedule.splice(+b.dataset.i, 1); renderSchedule();
  });
  syncSelHeader();
}


/* 只同步状态位，不重建表格（保护未保存的编排增删改——检视 major） */
async function refreshStatusesOnly() {
  try {
    const d = await invoke("get_dashboard");
    for (const r of (d.rows || [])) {
      const hit = schedule.find(x => x.id === r.slug);
      if (hit) hit.status = r.status;
    }
    renderSchedule();
  } catch (err) { /* 静默 */ }
}

/* ---------------- 立即发布（单篇/多选） ---------------- */
async function publishNow(slugs) {
  if (!slugs.length) return;
  if (!confirm(`立即发布 ${slugs.length} 篇？（跳过排期时间；已发布/处理中的会被护栏跳过）`)) return;
  $("#plan-output").textContent = `⏳ 立即发布 ${slugs.length} 篇执行中…`;
  try {
    const out = await invoke("native_publish_now", { slugs });
    $("#plan-output").textContent = out || "（无输出）";
  } catch (err) { $("#plan-output").textContent = `✗ ${err}`; }
  refreshLog(); loadPlan();
}
$("#btn-publish-sel").onclick = () => {
  const slugs = $$("#schedule .row-sel:checked").map(cb => cb.dataset.slug);
  if (!slugs.length) { $("#plan-output").textContent = "先勾选要立即发布的行"; return; }
  publishNow(slugs);
};

/* ---------------- 分类/摘要 弹窗 ---------------- */
let fdIdx = -1;
let catCache = null;   // [[id, 名称]]
async function ensureCategories() {
  const sel = $("#fd-category");
  if (catCache) return;
  try {
    const list = JSON.parse(await invoke("native_categories"));
    catCache = list;
    sel.innerHTML = list.map(([id, name]) =>
      `<option value="${id}">${name || id}</option>`).join("");
  } catch (err) {
    catCache = [["6809637773935378440", "人工智能"], ["6809637769959178254", "后端"],
                ["6809637767543259144", "前端"], ["6809637771511070734", "开发工具"],
                ["6809637776263217160", "代码人生"]];
    sel.innerHTML = catCache.map(([id, name]) =>
      `<option value="${id}">${name}</option>`).join("");
  }
}
async function openFieldDialog(i) {
  fdIdx = i;
  await ensureCategories();
  $("#fd-title").textContent = `编辑分类/摘要 — ${schedule[i].name}`;
  const cur = schedule[i].category_id || "6809637773935378440";
  $("#fd-category").value = cur;
  if (!$("#fd-category").value) {           // 文件里的 id 不在列表（旧/罕见）→ 补一项
    $("#fd-category").insertAdjacentHTML("beforeend", `<option value="${cur}">${cur}</option>`);
    $("#fd-category").value = cur;
  }
  $("#fd-desc").value = schedule[i].description || "";
  $("#field-dialog").showModal();
}
$("#fd-cancel").onclick = () => $("#field-dialog").close();
$("#fd-save").onclick = async () => {
  const s = schedule[fdIdx];
  const cat = $("#fd-category").value.trim();
  const desc = $("#fd-desc").value.trim();
  const out = $("#plan-output");
  out.textContent = "";
  try {
    if (cat && cat !== s.category_id) {
      await invoke("native_set_field", { file: s.file, key: "category_id", value: cat });
      s.category_id = cat; out.textContent = `✓ 分类已写回 ${s.name}`;
    }
    if (desc && desc !== s.description) {
      await invoke("native_set_field", { file: s.file, key: "description", value: desc });
      s.description = desc; out.textContent += `\n✓ 摘要已写回 ${s.name}`;
    }
    $("#field-dialog").close();
    if (!out.textContent) out.textContent = "（无变更）";
  } catch (err) { out.textContent = `✗ ${err}`; }
};

/* ---------------- UI / Code 双视图 ---------------- */
$$(".vt").forEach(b => b.onclick = async () => {
  $$(".vt").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  const code = b.dataset.view === "code";
  $("#view-code").style.display = code ? "" : "none";
  $("#view-ui").style.display = code ? "none" : "";
  if (code) await refreshYamlView();
});
async function refreshYamlView() {
  try {
    const d = await invoke("get_dashboard");
    $("#plan-yaml").textContent = d.plan_raw || "（尚无计划——在 UI 视图编排后生成）";
    $("#yaml-path").textContent = "%APPDATA%\\publisher-agent\\plan.yaml";
  } catch (err) { $("#plan-yaml").textContent = `✗ ${err}`; }
}
$("#btn-refresh-yaml").onclick = refreshYamlView;
$("#btn-copy-yaml").onclick = async () => {
  try {
    await navigator.clipboard.writeText($("#plan-yaml").textContent);
    $("#plan-output").textContent = "✓ YAML 已复制";
  } catch (err) { $("#plan-output").textContent = `✗ 复制失败：${err}`; }
};

/** 启动时把磁盘上的现有计划载入编排页（App 重开不丢排期显示）。 */
async function loadPlan() {
  try {
    const d = await invoke("get_dashboard");
    schedule = (d.rows || []).map(r => ({
      id: r.slug,
      file: r.file,
      name: (r.file || "").split(/[\\/]/).pop().replace(/\.md$/, ""),
      title_juejin: r.title_juejin || "",
      tags: r.tags || "", category_id: r.category_id || "", description: r.description || "",
      column: r.column || (r.file || "").split(/[\\/]/).slice(-2, -1)[0] || "",
      juejin_at: (r.juejin_at || "").replace(" ", "T"),
      status: r.status,
    }));
    renderSchedule();
  } catch (err) { /* 计划文件缺失等，编排页空表即可 */ }
}

$("#btn-apply-tpl").onclick = () => {
  const start = $("#tpl-start").value;
  const mode = $("#tpl-mode").value;
  const n = Math.max(1, Math.min(7, +$("#tpl-n").value || 1));
  const baseTime = $("#tpl-daily").value || "09:15";
  if (!start) { $("#plan-output").textContent = "先选批量模板的起始时间"; return; }
  // 天数偏移：每天 n 篇 → 同一天放 n 个条目（时间相同）；每周 n 篇 → 均匀分布 7 天。
  // 不做任何自动错开——同日多篇的具体时间由用户在时间列手动指定（2026-09-19 用户要求）
  const [hh, mm] = ($("#tpl-daily").value || "09:15").split(":").map(Number);
  schedule.forEach((s, i) => {
    const dayOff = mode === "daily" ? Math.floor(i / n) : Math.floor(i * 7 / n);
    const d = new Date(start.replace("T", " ") + ":00");
    d.setDate(d.getDate() + dayOff);
    d.setHours(hh || 9, mm || 15, 0, 0);
    s.juejin_at = fmtDT(d);
  });
  renderSchedule();
  const label = mode === "daily" ? `每天 ${n} 篇` : `每周 ${n} 篇`;
  $("#plan-output").textContent = `✓ 已套用「${label}」到 ${schedule.length} 篇——同日多篇的时间请在时间列自行调整`;
};
const fmtDT = (d) => {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

$("#btn-save-plan").onclick = async () => {
  if (!schedule.length) { $("#plan-output").textContent = "编排列表为空"; return; }
  const spec = {
    cadence: { juejin: { min_gap_h: 0 } },
    queue: schedule.map(s => ({
      id: s.id, file: s.file,
      juejin: Object.assign(s.juejin_at ? { at: s.juejin_at.replace("T", " ") } : {},
                            s.column ? { column: s.column } : {}),
    })),
  };
  $("#plan-output").textContent = "校验并写入中…";
  try {
    $("#plan-output").textContent = await invoke("save_plan", { spec: JSON.stringify(spec) });
  } catch (err) { $("#plan-output").textContent = `✗ ${err}`; }
};

$("#btn-deploy").onclick = async () => {
  $("#plan-output").textContent = "注册原生心跳（exe --tick，纯 Rust 无 Python）…";
  try { $("#plan-output").textContent = await invoke("native_deploy"); }
  catch (err) { $("#plan-output").textContent = `✗ ${err}`; }
  refreshHeartbeatState();
};
$("#btn-dry").onclick = async () => {
  $("#plan-output").textContent = "Rust 原生干跑中…";
  try { $("#plan-output").textContent = await invoke("native_tick", { dry: true }); }
  catch (err) { $("#plan-output").textContent = `✗ ${err}`; }
};

async function refreshHeartbeatState() {
  try {
    const d = await invoke("get_dashboard");
    const s = d.heartbeat_task;
    const el = $("#hb-state");
    const deployBtn = $("#btn-deploy");
    const stopBtn = $("#btn-undeploy");
    if (s === "running") {
      el.textContent = "✓ 心跳在跑——只管生成计划，无需重复部署（停止点右侧按钮，计划会保留）";
      el.style.color = "var(--ok)";
      deployBtn.style.display = "none";
      stopBtn.style.display = "";
    } else if (s === "disabled") {
      el.textContent = "⏸ 心跳已停用——计划与状态都保留，需要执行时重新部署";
      el.style.color = "var(--warn)";
      deployBtn.style.display = "";
      stopBtn.style.display = "none";
    } else {
      el.textContent = "⚠ 尚未部署心跳：计划生成了也没人定时执行";
      el.style.color = "var(--bad)";
      deployBtn.style.display = "";
      stopBtn.style.display = "none";
    }
  } catch (err) { $("#hb-state").textContent = ""; }
}
$("#btn-undeploy").onclick = async () => {
  if (!confirm("停止每小时心跳？计划与状态全部保留，随时可重新部署恢复。")) return;
  try { $("#plan-output").textContent = await invoke("native_undeploy"); }
  catch (err) { $("#plan-output").textContent = `✗ ${err}`; }
  refreshHeartbeatState();
};
loadPlan();
refreshHeartbeatState();

async function runDriver(cmd, args = []) {
  $("#plan-output").textContent = `运行中：${cmd} ${args.join(" ")} …`;
  try {
    const out = await invoke("run_driver", { cmd, args });
    $("#plan-output").textContent = out || "（无输出）";
  } catch (err) { $("#plan-output").textContent = `✗ ${err}`; }
}

/* ---------------- ③ 日志（备查） ---------------- */
async function refreshLog() {
  try {
    const d = await invoke("get_dashboard");
    const filter = $("#log-filter").value.trim();
    const ev = $("#events");
    ev.innerHTML = "";
    for (const e of d.events) {
      const line = `${e.ts}  ${e.type}  ${[e.slug, e.platform, e.gate, e.article_id, e.status, e.reason].filter(Boolean).join(" · ")}`;
      if (filter && !line.includes(filter)) continue;
      const type = String(e.type || "");
      const cls = /ok|passed|alive|approved/.test(type) ? "ok" : /fail|reject|held|stale|error/.test(type) ? "bad" : "";
      ev.innerHTML += `<li class="${cls}">${line}</li>`;
    }
    $("#log-meta").textContent = `心跳 ${d.heartbeat}｜上次流水线 ${d.pipeline?.name || "—"}｜队列 ${d.rows.length} 条`;
  } catch (err) { $("#log-meta").textContent = String(err); }
}
$("#log-filter").oninput = refreshLog;

/* ---------------- 顶栏 ---------------- */
$("#btn-doctor").onclick = async () => {
  $("#plan-output").textContent = "Rust 原生自检中…";
  try { $("#plan-output").textContent = await invoke("native_doctor"); }
  catch (err) { $("#plan-output").textContent = `✗ ${err}`; }
};
$("#btn-tick").onclick = async () => {
  if (!confirm("立即执行默认流水线（Rust 原生，可能发布文章）？")) return;
  const btn = $("#btn-tick");
  btn.disabled = true;
  const dots = setInterval(() => {
    $("#plan-output").textContent = "⏳ 执行中" + ".".repeat(1 + (Date.now() / 400 | 0) % 3);
  }, 400);
  $("#plan-output").textContent = "⏳ 执行中";
  try {
    const out = await invoke("native_tick", { dry: false });
    $("#plan-output").textContent = out || "（无输出）";
  } catch (err) { $("#plan-output").textContent = `✗ ${err}`; }
  clearInterval(dots);
  btn.disabled = false;
  refreshLog(); refreshHeartbeatState(); loadPlan();   // 表格状态同步刷新
};
$("#btn-login").onclick = async () => {
  $("#plan-output").textContent = "后台拉起内嵌 Chromium 扫码窗口（掘金，最长 5 分钟）……";
  try {
    await invoke("run_driver", { cmd: "login", args: ["juejin"] });
  } catch (err) { $("#plan-output").textContent = `✗ ${err}`; }
};

// 模板起始时间默认=当前时间（用户要求）
(function initTplDefault() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const v = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  const el = $("#tpl-start");
  if (el && !el.value) el.value = v;
})();

refreshLog();

/* 状态栏：单击展开/收起完整输出 */
$("#plan-output").onclick = () => $("#plan-output").classList.toggle("expanded");
