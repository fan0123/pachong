/* 声量采集中台前端。单页 + 假路由，状态全在这几个模块级变量里。 */
const $ = (id) => document.getElementById(id);
const api = (p, opt) => fetch(p, opt).then(r => r.json());
const fmt = (n) => (+n || 0).toLocaleString("zh-CN");
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
/* 内联事件处理器里的字符串参数一律走这个：标题/URL 里带单引号会直接把 onclick 写坏。
   JSON.stringify 负责转义引号，再把 HTML 特殊字符转义一遍。 */
const q = (s) => esc(JSON.stringify(String(s == null ? "" : s)));

let PLATS = [];        // 平台清单（含登录态）
/* 官方 logo：simple-icons（CC0，可商用）下载到 web/logos，配各家品牌色；
   头条号 simple-icons 没收录，用文字徽标兜底。抖音用的是同一品牌的 TikTok 标识。
   黑猫投诉是新浪旗下产品、走微博账号 SSO，没有独立开源图标，
   复用 sinaweibo 标识 + 墨黑底色，跟红色的微博区分开。 */
const BRAND = {
  xhs: { c: "#FF2442", logo: 1 },
  weibo: { c: "#E6162D", logo: 1 },
  douyin: { c: "#111111", logo: 1 },
  kuaishou: { c: "#FF4906", logo: 1 },
  baijiahao: { c: "#2932E1", logo: 1 },
  weixin: { c: "#07C160", logo: 1 },
  toutiao: { c: "#F04142", logo: 0, txt: "头" },
  zhihu: { c: "#0084FF", logo: 1 },
  bilibili: { c: "#FB7299", logo: 1 },
  heimao: { c: "#2b2f36", logo: 1, file: "sinaweibo" },
};
function logo(name, big) {
  const b = BRAND[name] || { c: "#59637a", logo: 0, txt: "?" };
  const cls = "logo" + (big ? " lg" : "");
  if (b.logo) return `<i class="${cls}" style="--m:url(logos/${b.file || name}.svg);--c:${b.c}"></i>`;
  return `<i class="${cls} txt" style="background:${b.c}">${b.txt}</i>`;
}
const BY_LABEL = () => Object.fromEntries(PLATS.map(p => [p.label, p.name]));
/* 图表配色：品牌色留给 logo 和覆盖度条；图表里小红书/微博/头条号/快手四家都是红系，
   堆在一起分不出来，所以按平台固定一套色相分得开的颜色，保证跨图一致。 */
const CHART_COLOR = {
  xhs: "#d9453d", weibo: "#e08a1e", douyin: "#1a1f2b", kuaishou: "#7c5cff",
  baijiahao: "#2f6bff", weixin: "#0f9960", toutiao: "#0aa2c0",
  zhihu: "#0084FF", bilibili: "#e0679b", heimao: "#6b5b95",
};
function brandColor(label) {
  return CHART_COLOR[BY_LABEL()[label]] || "#8b95a9";
}
function logoColor(label) {   // 覆盖度条用真品牌色，和左侧 logo 对得上
  const b = BRAND[BY_LABEL()[label]];
  return (b && b.c) || "#8b95a9";
}
let ROWS = [];         // 当前口径的全部帖子
let PAGE = 1;
let SCOPE = "main";
let CUR_TASK = null;   // 正在看的任务 id
let POLL = null;
const SCOPE_NAME = {
  main: "负面主口径", neg_plus: "主口径＋中性", valid: "全部有效",
  positive: "仅正面", all: "全部含噪音", noise: "噪音复核",
};


function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

/* ---------------- 路由 ---------------- */
/* 只留标题，不再挂副标题说明——页面用途看导航就够了 */
const TITLES = {
  dash: "数据看板", data: "帖子明细", report: "分析报告",
  crawl: "采集与任务", plat: "平台与登录", export: "历史与导出",
};
function go(page) {
  document.querySelectorAll(".page").forEach(p => p.classList.toggle("on", p.id === "page-" + page));
  document.querySelectorAll("#nav a").forEach(a => a.classList.toggle("on", a.dataset.page === page));
  $("ttl").textContent = TITLES[page] || "";
  if (page === "export") { loadFiles(); loadLayerStat(); loadRuns(); loadSettings(); }
  if (page === "crawl") loadTasks();
  if (page === "report") { loadModels(); loadReports(); }
  window.scrollTo(0, 0);
}
$("nav").addEventListener("click", e => {
  const a = e.target.closest("a[data-page]");
  if (a) go(a.dataset.page);
});
$("scopeseg").addEventListener("click", e => {
  const b = e.target.closest("button[data-s]");
  if (!b) return;
  SCOPE = b.dataset.s;
  $("scopemore").value = "";
  document.querySelectorAll("#scopeseg button").forEach(x => x.classList.toggle("on", x === b));
  loadData();
});
$("quickdate").addEventListener("click", e => {
  const b = e.target.closest("button[data-d]");
  if (!b) return;
  document.querySelectorAll("#quickdate button").forEach(x => x.classList.toggle("on", x === b));
  const iso = d => d.toISOString().slice(0, 10);
  if (b.dataset.d === "q3") { $("since").value = "2026-06-01"; $("until").value = "2026-08-31"; }
  else {
    const n = +b.dataset.d, now = new Date();
    $("until").value = iso(now);
    $("since").value = iso(new Date(now.getTime() - n * 864e5));
  }
  loadData();
});

/* ---------------- 平台与登录 ---------------- */
async function loadPlats() {
  PLATS = await api("/api/platforms");
  const ready = PLATS.filter(p => !p.needs_login || p.logged_in).length;
  $("nav-plat").textContent = `${ready}/${PLATS.length}`;
  $("plats").innerHTML = PLATS.map(p => {
    const tag = !p.needs_login ? '<span class="tag free">免登录</span>'
      : p.logged_in ? '<span class="tag ok">可用</span>'
        : '<span class="tag need">需扫码</span>';
    const opt = p.login_optional ? '<span class="tag warn">可降级</span>' : "";
    // 卡上只留"能不能用、能拿什么、什么时候校验的"三件事；
    // 平台实现细节（NOTE）和校验结论都收进 hover 提示，卡片高度才压得住
    return `<div class="plat" title="${esc(p.note)}">
      <div class="hd">${logo(p.name, true)}<b>${esc(p.label)}</b>${tag}${opt}</div>
      <div class="mx" title="${esc(p.note)}">${p.metrics.join(" / ") || "网页侧无互动数"}</div>
      <div class="lc" title="校验结论：${esc(p.reason) || "未校验"}">
        ${p.last_check ? esc(p.last_check.slice(5, 16)) + " 校验" : "未校验"}</div>
      <div class="act">
        <button class="btn sm ${p.needs_login && !p.logged_in ? "" : "ghost"}" onclick="login('${p.name}')">
          ${p.needs_login ? (p.logged_in ? "重新登录" : "扫码登录") : "可选登录"}</button>
        <button class="btn ghost sm" onclick="checkOne('${p.name}')">体检</button>
      </div></div>`;
  }).join("");
  $("fplat").innerHTML = '<option value="">全部平台</option>' +
    PLATS.map(p => `<option value="${esc(p.label)}">${esc(p.label)}</option>`).join("");
  $("pchk").innerHTML = PLATS.map(p => {
    const ok = !p.needs_login || p.logged_in;
    return `<label class="chk" title="${esc(p.reason)}">
      <input type="checkbox" value="${p.name}" ${ok ? "checked" : ""}>
      ${logo(p.name)}${esc(p.label)}${ok ? "" : ' <span class="tag need">需登录</span>'}</label>`;
  }).join("");
  renderLoginBadge();
}

function pickAll(on) {
  document.querySelectorAll("#pchk input").forEach(i => { i.checked = !!on; });
}
function pickReady() {
  const ready = new Set(PLATS.filter(p => !p.needs_login || p.logged_in).map(p => p.name));
  document.querySelectorAll("#pchk input").forEach(i => { i.checked = ready.has(i.value); });
}
function picked() {
  return [...document.querySelectorAll("#pchk input:checked")].map(i => i.value);
}
function applyPreset() {
  const v = $("kwpreset").value;
  if (v) $("kw").value = v;
}

async function login(name) {
  const r = await api("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ platform: name })
  });
  toast("登录窗口正在打开，请在窗口里扫码；进度看任务日志");
  go("crawl"); watch(r.task);
}
async function checkOne(name) {
  const r = await api("/api/check", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ platforms: [name] })
  });
  go("crawl"); watch(r.task);
}
async function checkAll() {
  const r = await api("/api/check", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ platforms: PLATS.filter(p => p.needs_login).map(p => p.name) })
  });
  toast("正在无头校验各平台登录态");
  go("crawl"); watch(r.task);
}

/* ---------------- 采集与任务 ---------------- */
$("cquick").addEventListener("click", e => {
  const b = e.target.closest("button[data-d]");
  if (!b) return;
  document.querySelectorAll("#cquick button").forEach(x => x.classList.toggle("on", x === b));
  const iso = d => d.toISOString().slice(0, 10);
  if (b.dataset.d === "sync") {
    $("csince").value = $("since").value; $("cuntil").value = $("until").value;
  } else {
    const n = +b.dataset.d, now = new Date();
    $("cuntil").value = iso(now);
    $("csince").value = iso(new Date(now.getTime() - n * 864e5));
  }
});

async function crawl() {
  if ($("csince").value === "" || $("cuntil").value === "") {
    $("csince").value = $("since").value; $("cuntil").value = $("until").value;
  }
  const body = {
    keywords: $("kw").value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
    platforms: picked(),
    limit: +$("limit").value || 50,
    since: $("csince").value, until: $("cuntil").value,
    strict_match: $("strict").checked,
    // 增量采集：后端会按「渠道 × 关键词 × 天」核对采集流水，整段抓过的渠道直接跳过
    gap_only: $("gaponly").checked,
    // 评论采集：默认不采，勾了才采
    with_comments: $("withcomments").checked,
    comment_limit: +$("climit").value || 30,
  };
  if (!body.platforms.length) { toast("至少勾一个平台", "err"); return; }
  const r = await api("/api/crawl", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (r.error) { toast(r.error, "err"); return; }
  $("crawlsum").textContent =
    `已提交：${body.platforms.length} 平台 × ${body.keywords.length} 词，`
    + `区间 ${body.since}~${body.until}，上限 ${body.limit}`;
  toast("采集任务已提交，进度看下面的任务日志");
  watch(r.task);
}

/* 全量采集：所有可用平台 × 库内已有的全部关键词，一次跑完，日志末尾出完整性台账 */
async function crawlAll() {
  const kws = [...$("kwfilter").options].map(o => o.value).filter(Boolean);
  const input = $("kw").value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
  const keywords = [...new Set([...input, ...kws])];
  if (!keywords.length) { toast("先填关键词", "err"); return; }
  if ($("csince").value === "" || $("cuntil").value === "") {
    $("csince").value = $("since").value; $("cuntil").value = $("until").value;
  }
  pickReady();
  const plats = picked();
  if (!plats.length) { toast("没有可用平台，先在右上角登录态里扫码", "err"); return; }
  if (!confirm(`将采集 ${plats.length} 个平台 × ${keywords.length} 个关键词 = `
    + `${plats.length * keywords.length} 个组合，区间 ${$("csince").value} ~ ${$("cuntil").value}。\n`
    + `每个组合几十秒到几分钟，整体可能要跑很久，期间可以关页面（任务在后台）。继续？`)) return;
  $("kw").value = keywords.join(",");
  const r = await api("/api/crawl", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      keywords, platforms: plats, limit: +$("limit").value || 50,
      since: $("csince").value, until: $("cuntil").value,
      strict_match: $("strict").checked,
      with_comments: $("withcomments").checked,
      comment_limit: +$("climit").value || 30,
    })
  });
  if (r.error) { toast(r.error, "err"); return; }
  $("crawlsum").textContent =
    `全量采集已提交：${plats.length} 平台 × ${keywords.length} 词 = ${plats.length * keywords.length} 组合`;
  toast("全量采集已提交，结束后日志末尾会逐条列出每个组合的结果");
  watch(r.task);
}

async function loadTasks() {
  const ts = await api("/api/tasks");
  $("nav-task").textContent = ts.filter(t => t.state === "running").length || ts.length;
  if (!ts.length) { $("tasks").innerHTML = '<div class="empty">还没有任务</div>'; return; }
  $("tasks").innerHTML = ts.map(t => {
    const icon = t.state === "running" ? '<span class="spin"></span>'
      : t.state === "done" ? '<span class="tag ok">完成</span>' : '<span class="tag need">出错</span>';
    return `<div class="taskitem ${t.id === CUR_TASK ? "on" : ""}" onclick="watch('${t.id}')">
      ${icon}<span>${esc(t.title)}</span>
      <span class="sp">${esc(t.started)}${t.result && t.result.saved != null ? " · 入库 " + t.result.saved + " 条" : ""}</span>
    </div>`;
  }).join("");
}

function watch(tid) {
  CUR_TASK = tid;
  clearInterval(POLL);
  const tick = async () => {
    const t = await api("/api/task?id=" + tid);
    $("logttl").textContent = `${t.title || tid} · ${t.state === "running" ? "进行中" : t.state === "done" ? "已完成" : "出错"}`;
    const box = $("log");
    box.textContent = (t.log || []).join("\n") || "任务已提交，等待输出…";
    if ($("autoscroll").checked) box.scrollTop = box.scrollHeight;
    renderProgress(t);
    renderTaskCtl(t);
    loadTasks();
    if (t.state !== "running") {
      clearInterval(POLL);
      loadPlats();
      loadData();
      renderTaskCtl(null);
      if (t.kind === "crawl") {
        const d = t.result || {};
        toast(`${d.cancelled ? "任务已取消" : "采集完成"}，入库 ${d.saved || 0} 条`
          + (d.dropped ? `，相关性闸门剔除 ${d.dropped} 条` : "")
          + (d.combos ? `；组合 ${d.combos} 个，无数据 ${d.missing} 个` : ""), "ok");
        loadRuns();
      }
      if (t.kind === "login") toast(t.result.logged_in ? "登录成功，登录态已落盘" : "未登录成功，看日志原因",
        t.result.logged_in ? "ok" : "err");
      if (t.kind === "report") {
        if (t.result && t.result.file) {
          toast("报告已生成", "ok");
          loadReports().then(() => openReport(t.result.file));
        } else {
          toast("报告生成失败，看日志", "err");
        }
      }
    }
  };
  tick();
  POLL = setInterval(tick, 1500);
}

/* 采集任务遥控：暂停 / 继续 / 取消。都在"段边界"生效——
   不会把正在跑的那一段浏览器操作硬切断，所以不会留下半截数据。 */
async function taskControl(action) {
  if (!CUR_TASK) return;
  if (action === "cancel" && !confirm("取消这个采集任务？\n\n已抓到的部分会保留在库里，未执行的段标记为「已取消」。")) return;
  const r = await api("/api/task/control", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: CUR_TASK, action })
  });
  if (!r.ok) { toast(r.msg || "操作失败", "err"); return; }
  toast({ pause: "已暂停，当前这段跑完就停", resume: "已继续", cancel: "已取消，正在收尾" }[action], "ok");
}

function renderTaskCtl(t) {
  const box = $("tctl");
  if (!box) return;
  if (!t || t.state !== "running" || t.kind !== "crawl") { box.innerHTML = ""; return; }
  const paused = t.control === "pause";
  box.innerHTML = paused
    ? `<button class="btn sm" onclick="taskControl('resume')">▶ 继续</button>
       <button class="btn ghost sm" onclick="taskControl('cancel')">取消任务</button>
       <span class="tag warn">已暂停</span>`
    : `<button class="btn ghost sm" onclick="taskControl('pause')">⏸ 暂停</button>
       <button class="btn ghost sm" onclick="taskControl('cancel')">取消任务</button>`;
}

/* ---------------- 采集进度（按渠道打勾） ---------------- */
/* 日志是给排查用的流水，进度是给"现在跑到哪了"用的状态机，两者分开。
   渠道为一组：组头显示 完成/总数 与聚合状态，组内每个关键词×窗口一行带勾/叉。 */
const P_ICON = { wait: "○", running: "◐", ok: "✓", empty: "✓", fail: "✕", skip: "⤼" };
const P_TEXT = { wait: "等待", running: "进行中", ok: "完成", empty: "无结果", fail: "失败", skip: "已覆盖·跳过" };

function renderProgress(t) {
  const pg = t.progress;
  const box = $("progress");
  if (!pg || !pg.total) { box.innerHTML = ""; box.classList.remove("on"); return; }
  box.classList.add("on");
  const pct = Math.round(pg.done / pg.total * 100);
  const groups = {};
  pg.items.forEach(it => (groups[it.platform] = groups[it.platform] || []).push(it));
  const gstate = (list) => {
    if (list.some(x => x.state === "running")) return "running";
    if (list.every(x => x.state === "skip")) return "skip";
    if (list.some(x => x.state === "fail")) return "fail";
    if (list.every(x => x.state !== "wait" && x.state !== "running")) return "ok";
    return "wait";
  };
  box.innerHTML = `
    <div class="pgtop">
      <div class="pgnum"><b>${pg.done}</b><span>/ ${pg.total} 段</span></div>
      <div class="pgbar"><i style="width:${pct}%" class="${t.state === "running" ? "run" : ""}"></i></div>
      <div class="pgmeta">
        ${t.state === "running"
          ? `<span class="spin"></span>正在跑 <b>${esc(pg.cur || "…")}</b>`
          : `<span class="tag ok">已结束</span>`}
        · 入库 <b>${fmt(pg.saved)}</b> 条
        ${pg.fail ? `· <span style="color:var(--danger)">失败 ${pg.fail} 段</span>` : ""}
      </div>
    </div>
    <div class="pglist">
      ${Object.entries(groups).map(([name, list]) => {
        const st = gstate(list);
        const done = list.filter(x => x.state !== "wait" && x.state !== "running").length;
        return `<details class="pggrp ${st}" ${st === "running" ? "open" : ""}>
          <summary>
            <span class="ic">${P_ICON[st]}</span>
            ${logo(name)}<b>${esc(list[0].platform_label)}</b>
            <span class="ct">${done}/${list.length}</span>
            <span class="sp muted">${P_TEXT[st]}</span>
          </summary>
          ${list.map(it => `<div class="pgrow ${it.state}">
            <span class="ic">${P_ICON[it.state]}</span>
            <span class="kw">${esc(it.keyword)}</span>
            <span class="win">${esc(it.window)}</span>
            <span class="msg">${esc(it.msg || P_TEXT[it.state])}</span>
          </div>`).join("")}
        </details>`;
      }).join("")}
    </div>`;
}

/* ---------------- 明细表 ---------------- */
/* 筛选下拉的选项与计数**必须**来自当前已加载的 ROWS，不能来自 overview。
   overview 里的 by_actor / by_sentiment 是在「剔噪后全量」上统计的，
   而表格筛的是「当前口径」——两者口径不同，就会出现
   "情感=正面（2）但表格 0 条"（主口径本身不含正面层）这种对不上的情况。 */
function fillFilters() {
  const count = (key, fallback) => {
    const m = new Map();
    ROWS.forEach(r => {
      const v = r[key] || fallback;
      m.set(v, (m.get(v) || 0) + 1);
    });
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  };
  const fill = (id, label, key, fallback, tidy = (x) => x) => {
    const el = $(id), cur = el.value;
    const opts = count(key, fallback);
    el.innerHTML = `<option value="">${label}（${ROWS.length}）</option>` +
      opts.map(([v, n]) => `<option value="${esc(v)}">${esc(tidy(v))}（${n}）</option>`).join("");
    // 原来选中的值如果在新口径里没有了，回到"全部"，而不是留一个筛不出东西的死选项
    el.value = opts.some(([v]) => v === cur) ? cur : "";
  };
  fill("fplat", "全部平台", "platform_label", "未知");
  fill("flayer", "全部分层", "eff_layer", "未分层", v => v.replace(/^._/, ""));
  fill("factor", "全部主体", "actor", "未判定");
  fill("fsent", "全部情感", "sentiment", "未判定");
  fill("ftopic", "全部类型", "topic_kind", "未判定");
}

function filtered() {
  const plat = $("fplat").value, layer = $("flayer").value, sort = $("fsort").value;
  const actor = $("factor").value, sent = $("fsent").value;
  const topic = ($("ftopic") || {}).value || "";
  let rows = ROWS.filter(r => (!plat || (r.platform_label || "未知") === plat)
    && (!layer || (r.eff_layer || r.layer || "未分层") === layer)
    && (!actor || (r.actor || "未判定") === actor)
    && (!sent || (r.sentiment || "未判定") === sent)
    && (!topic || (r.topic_kind || "未判定") === topic));
  if (sort === "new") rows = rows.slice().sort((a, b) => (b.publish_time || "").localeCompare(a.publish_time || ""));
  if (sort === "old") rows = rows.slice().sort((a, b) => (a.publish_time || "").localeCompare(b.publish_time || ""));
  if (sort === "impact") rows = rows.slice().sort((a, b) => (b.impact || 0) - (a.impact || 0));
  return rows;
}

/* 人工改判 / 删除。改完直接重跑 loadData()——用户要的是"打标后对应数据联动更新"，
   所以走一次完整刷新，看板 KPI、声量、事件归因、词云全部跟着变，而不是只改这一行的样式。 */
/* 查看某条帖子采到的评论。评论是可选采集的，没采就不会有 💬 角标。 */
async function viewComments(platform, postId, title) {
  const rows = await api(`/api/comments?platform=${encodeURIComponent(platform)}`
    + `&post_id=${encodeURIComponent(postId)}`);
  const box = $("cmtmodal");
  const body = rows && rows.length
    ? rows.map(c => `<div class="cmtrow">
        <div class="cmthd"><b>${esc(c.commenter || "匿名")}</b>
          <span class="muted">${esc(c.comment_time || "")}</span>
          ${c.likes ? `<span class="muted">· 赞 ${fmt(c.likes)}</span>` : ""}</div>
        <div class="cmttx">${esc(c.content || "")}</div></div>`).join("")
    : '<div class="empty">这条帖子还没有采到评论</div>';
  box.querySelector(".cmttitle").textContent = `评论 · ${title || postId}（${rows ? rows.length : 0} 条）`;
  box.querySelector(".cmtlist").innerHTML = body;
  box.style.display = "flex";
}

function closeComments() { $("cmtmodal").style.display = "none"; }

async function tagPost(platform, postId, sentiment) {
  const r = await api("/api/post/tag", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ platform, post_id: postId, sentiment })
  });
  if (!r.ok) { toast(r.msg || "改判失败", "err"); return; }
  toast(sentiment ? `已改判为「${sentiment}」，分层同步为 ${r.layer || "—"}，全站口径已更新`
    : "已撤销人工改判，回到规则判定", "ok");
  await loadData();
}

async function delPost(platform, postId, title) {
  if (!confirm(`删除「${title}…」？\n\n软删除：这条会从所有口径与导出里消失，重新采集也不会复活。`)) return;
  const r = await api("/api/post/delete", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ platform, post_id: postId, deleted: true })
  });
  if (!r.ok) { toast("删除失败：没找到这条", "err"); return; }
  toast("已删除，看板与声量已重算", "ok");
  await loadData();
}

function renderTable() {
  const rows = filtered();
  const size = +$("psize").value || 50;
  const max = Math.max(1, Math.ceil(rows.length / size));
  PAGE = Math.min(Math.max(1, PAGE), max);
  const slice = rows.slice((PAGE - 1) * size, PAGE * size);
  const body = document.querySelector("#tbl tbody");
  if (!slice.length) {
    body.innerHTML = '<tr><td colspan="16"><div class="empty">当前筛选没有数据，换个口径或日期看看</div></td></tr>';
  } else {
    const map = BY_LABEL();
    body.innerHTML = slice.map(r => `<tr class="${r.impact_high ? "hi" : ""}">
      <td><span class="pcell">${logo(map[r.platform_label])}${esc(r.platform_label)}</span></td>
      <td class="tm" title="${esc(r.publish_time)}">${esc((r.publish_time || "").slice(5, 16)) || '<span class="muted">未知</span>'}</td>
      <td class="au" title="${esc(r.author)}">${esc(r.author)}</td>
      <td><span class="chip" style="--cc:${ACTOR_COLOR[r.actor] || "#8b95a9"}"
          title="${esc(r.actor_reason)}">${esc(r.actor || "未判定")}</span></td>
      <td>${sentCell(r)}</td>
      <td>${topicCell(r)}</td>
      <td class="ti">${r.impact_high ? '<span class="lay lay-A">重点</span> ' : ""}<a
          href="${esc(r.url)}" target="_blank" rel="noreferrer"
          title="${esc(r.title)}&#10;&#10;判定依据：${esc(r.eff_layer || r.layer_reason)}">${esc(r.title || "（无标题）")}</a>${
        r.comment_n ? ` <button class="cmtbtn" title="查看采到的 ${r.comment_n} 条评论"
          onclick="viewComments(${q(r.platform)},${q(r.post_id)},${q((r.title||'').slice(0,24))})">💬 ${r.comment_n}</button>` : ""}</td>
      <td><span class="lay lay-${esc((r.layer || "?").slice(0, 1))}" title="${esc(r.eff_layer || r.layer_reason)}">${esc((r.eff_layer || r.layer || "未分层").replace(/^._/, ""))}</span></td>
      <td class="num">${fmt(r.likes)}</td><td class="num">${fmt(r.comments)}</td>
      <td class="num">${fmt(r.shares)}</td><td class="num">${fmt(r.collects)}</td>
      <td class="num">${fmt(r.views)}</td><td class="num"><b>${fmt(r.engagement)}</b></td>
      <td class="num" title="负面影响力＝互动量 × 主体权重，只算负面">${r.impact ? fmt(r.impact) : "—"}</td>
      <td class="kwc" title="${esc(r.keyword)}">${esc(r.keyword)}</td>
      <td class="acts"><div class="rowact">
        ${["负面", "中性", "正面"].map(s => `<button class="tg ${r.sentiment === s ? "on" : ""}"
            title="改判为${s}" onclick="tagPost(${q(r.platform)},${q(r.post_id)},${q(s)})">${s[0]}</button>`).join("")}
        ${r.manual_sentiment ? `<button class="tg" title="撤销人工改判，回到规则判定"
            onclick="tagPost(${q(r.platform)},${q(r.post_id)},'')">↺</button>` : ""}
        <button class="tg del" title="删除这条（软删除，可在「历史与导出 › 回收站」恢复）"
            onclick="delPost(${q(r.platform)},${q(r.post_id)},${q((r.title || '').slice(0, 24))})">✕</button>
      </div></td></tr>`).join("");
  }
  $("pageinfo").textContent = `第 ${PAGE} / ${max} 页`;
  $("prev").disabled = PAGE <= 1;
  $("next").disabled = PAGE >= max;
  $("tblinfo").textContent = `共 ${rows.length} 条 / 当前口径 ${ROWS.length} 条`;
  $("nav-data").textContent = ROWS.length;
}

function turnPage(step) {
  PAGE += step;
  renderTable();
}

/* ---------------- 图表 ---------------- */
/* hideOverlap：轴标签挤在一起时自动隐藏一部分，而不是叠成一团黑（趋势图的日期最明显） */
const AXIS = {
  axisLabel: { color: "#59637a", hideOverlap: true },
  axisLine: { lineStyle: { color: "#e6e9f0" } }
};
const SPLIT = { splitLine: { lineStyle: { color: "#eef0f5" } } };
const TITLE = (t) => ({ text: t, left: 4, top: 2, textStyle: { color: "#1a1f2b", fontSize: 13, fontWeight: 600 } });
/* 图例统一走"单行可滚动"：10 个平台的图例原来会折成两行，
   把 x 轴日期压在下面互相盖住。scroll 型图例永远只占一行，多的用箭头翻。 */
const LEGEND = (extra = {}) => Object.assign({
  type: "scroll", bottom: 0, left: "center",
  textStyle: { color: "#59637a", fontSize: 11 }, itemHeight: 9, itemWidth: 13,
  pageIconSize: 9, pageTextStyle: { color: "#8b95a9", fontSize: 10 },
}, extra);
const PALETTE = ["#2f6bff", "#0f9960", "#b7791f", "#d9453d", "#7c5cff", "#0aa2c0", "#8b95a9"];
let CH = {};
let RO = null;
/* 统一用 SVG renderer：
   1) canvas 在部分机器/缩放比下会画出整块空白（本机 headless 正常、真机偶发），SVG 是纯 DOM，不吃 GPU；
   2) 缩放和高分屏下线条更锐利，导出截图也清楚。数据量都是几百个点，SVG 完全够。
   同时挂一个 ResizeObserver：容器宽高一变（窗口缩放、页面切换、侧栏折叠）就重画，
   否则 echarts 会一直按初始化时的尺寸渲染，看起来就是"卡片是空的"。 */
function chart(id) {
  const el = $(id);
  if (!el) return { setOption() {}, resize() {} };
  if (!CH[id]) {
    CH[id] = echarts.init(el, null, { renderer: "svg" });
    if (!RO && window.ResizeObserver) {
      RO = new ResizeObserver(entries => {
        entries.forEach(e => {
          const c = CH[e.target.id];
          if (c && e.contentRect.width > 0) c.resize();
        });
      });
    }
    if (RO) RO.observe(el);
  }
  return CH[id];
}

/* 每块图各自 try 一下：以前只要有一块数据结构变了整函数就抛，
   后面所有卡片全成白板——这正是"看板一片空白"的根因之一。 */
function safely(name, fn) {
  try {
    fn();
  } catch (err) {
    console.error(`[图表] ${name} 渲染失败`, err);
    toast(`「${name}」渲染失败：${err.message}`, "err");
  }
}

/* 事件时间甘特：回答"这段时间里舆论是被哪几件事撑起来的、各自烧了多久"。
   趋势图只能看到某天有个尖峰，看不出一件事是一天爆完还是连烧两周——
   这两张图要一起看，所以峰值日在这里也画成竖线，横竖一对齐就知道尖峰归谁。

   用 custom 系列而不是"两根 bar 叠加"的老写法：后者要塞一根透明柱子当偏移量，
   图例和 tooltip 都得跟着做特例；custom 直接按像素画矩形，反而更好维护。 */
function renderGantt(ov, spikes) {
  const evs = (ov.events || []).filter(e => e.first_day && e.last_day)
    .slice(0, 12)
    .sort((a, b) => (a.first_day < b.first_day ? 1 : -1));
  const box = document.getElementById("c-gantt");
  if (!evs.length) {
    if (box) box.innerHTML = `<div class="empty">还没有可归因的事件——先采集数据并跑一次归因</div>`;
    return;
  }
  const day = (s) => new Date(s + "T00:00:00").getTime();
  const DAY = 86400000;
  const maxImpact = Math.max(...evs.map(e => e.impact || 0), 1);
  const data = evs.map((e, i) => ({
    name: e.name,
    value: [i, day(e.first_day), day(e.last_day) + DAY, e],
    itemStyle: {
      // 影响力越大越深，但保底 .28 的透明度，否则小事件条几乎看不见
      color: `rgba(232,69,60,${(0.28 + 0.62 * (e.impact || 0) / maxImpact).toFixed(2)})`,
      borderRadius: 3
    }
  }));
  chart("c-gantt").setOption({
    title: TITLE("热点事件时间甘特（条长＝持续天数，色深＝影响力）"),
    tooltip: {
      formatter: (p) => {
        const e = p.value[3];
        const days = Math.round((p.value[2] - p.value[1]) / DAY);
        return `<b>${esc(e.name)}</b>　<span style="color:#8b95a9">${esc(e.kind || "")}</span><br/>`
          + `${e.first_day} ~ ${e.last_day}（持续 ${days} 天）<br/>`
          + `帖子 ${fmt(e.posts)} 条　影响力 ${fmt(e.impact)}　高影响 ${fmt(e.high_posts || 0)} 条<br/>`
          + (e.why ? `归因：${esc(e.why)}` : "");
      }
    },
    grid: { left: 168, right: 28, top: 44, bottom: 44 },
    xAxis: {
      type: "time", ...AXIS, ...SPLIT,
      axisLabel: { ...(AXIS.axisLabel || {}), formatter: (v) => new Date(v).toISOString().slice(5, 10) }
    },
    yAxis: {
      type: "category", data: evs.map(e => e.name), ...AXIS,
      axisLabel: {
        ...(AXIS.axisLabel || {}), width: 150, overflow: "truncate",
        formatter: (v) => v.replace(/^「|」相关讨论$/g, "")
      }
    },
    series: [{
      type: "custom", data,
      renderItem: (params, api) => {
        const i = api.value(0);
        const start = api.coord([api.value(1), i]);
        const end = api.coord([api.value(2), i]);
        const h = Math.max(10, api.size([0, 1])[1] * 0.52);
        const rect = echarts.graphic.clipRectByRect(
          { x: start[0], y: start[1] - h / 2, width: Math.max(3, end[0] - start[0]), height: h },
          { x: params.coordSys.x, y: params.coordSys.y, width: params.coordSys.width, height: params.coordSys.height });
        return rect && { type: "rect", shape: rect, style: api.style() };
      },
      encode: { x: [1, 2], y: 0 },
      // 峰值日竖线：跟趋势图上的红点是同一批日子，两张图靠它对齐
      markLine: {
        silent: true, symbol: "none",
        lineStyle: { type: "dashed", color: "rgba(232,69,60,.45)" },
        // 不给竖线挂标签：峰值日常常只隔两三天（实测 08-10/08-12、08-21/08-25/08-28），
        // 文字必然互相压，还会跟 x 轴日期撞。日期在上方趋势图的销针上已经有了，
        // 这里两张图 x 轴同宽，肉眼一对就知道是哪天。
        label: { show: false },
        data: (spikes || []).map(s => ({ xAxis: day(s.date) }))
      }
    }]
  }, true);
}

function charts(ov) {
  const labels = ov.labels;
  const total = ov.trend.map(r => labels.reduce((s, l) => s + (r[l] || 0), 0));
  let acc = 0;
  const cum = total.map(v => (acc += v));
  /* 峰值日标注：光有一条曲线，用户看到尖峰只能自己去明细里翻那天发生了什么。
     后端 events.spikes() 已经把「峰值日 → 当天影响力最高的帖 → 归属场景」串好了，
     这里把它挂成 markPoint + markLine，鼠标移上去能看到是哪条帖子引爆的。
     标在"累计"线上会飘到图外，所以挂在第一个堆叠系列上、按当天总量定位。 */
  const spikes = (ov.spikes || []).filter(s => ov.trend.some(r => r.date === s.date));
  const spikeMarks = spikes.map(s => ({
    name: s.date, xAxis: s.date, yAxis: s.posts,
    value: s.posts,
    itemStyle: { color: "#e8453c" },
    // 标签放在 pin 内部（默认位置）。试过 position:"top" 把数字顶到销针上方，
    // 相邻峰值日一挤就互相压字，而且最高那个会被图表上边缘裁掉。
    label: { color: "#fff", fontSize: 10, fontWeight: 600, formatter: `${s.posts}` },
    spike: s
  }));
  safely("按日声量趋势", () => chart("c-trend").setOption({
    title: TITLE("按日声量趋势（平台堆叠 + 累计，红点为热点事件日）"),
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    legend: LEGEND(),
    // top 留 58：销针要画在当天柱子的顶端之上，留窄了最高那个会被裁掉
    grid: { left: 50, right: 50, top: 58, bottom: 52 },
    xAxis: { type: "category", data: ov.trend.map(r => r.date), ...AXIS },
    yAxis: [{ type: "value", ...AXIS, ...SPLIT },
    { type: "value", name: "累计", nameTextStyle: { color: "#8b95a9" }, ...AXIS, splitLine: { show: false } }],
    series: labels.map((l, i) => ({
      name: l, type: "bar", stack: "s", barMaxWidth: 22,
      itemStyle: { color: brandColor(l) }, data: ov.trend.map(r => r[l] || 0),
      // markPoint 只挂在第一个系列上，挂多份会重叠成一团
      ...(i === 0 ? {
        markPoint: {
          symbol: "pin", symbolSize: 26, data: spikeMarks,
          tooltip: {
            formatter: p => {
              const s = (p.data || {}).spike || {};
              return `<b>${s.date}　${s.posts} 条</b>（约均值的 ${s.ratio} 倍）<br/>`
                + `场景：${esc(s.scene || "未归类")}<br/>`
                + `引爆帖：${esc(s.title || "-")}<br/>`
                + `${esc(s.platform || "")}　${esc(s.author || "")}　互动 ${fmt(s.engagement || 0)}`;
            }
          }
        },
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { type: "dashed", color: "rgba(232,69,60,.35)" },
          label: { show: false },
          data: spikes.map(s => ({ xAxis: s.date }))
        }
      } : {})
    })).concat([{
      name: "累计", type: "line", yAxisIndex: 1, smooth: true, symbol: "none",
      lineStyle: { width: 2, color: "#2f6bff" },
      areaStyle: { color: "rgba(47,107,255,.08)" }, data: cum
    }])
  }, true));

  safely("热点事件时间甘特", () => renderGantt(ov, spikes));

  safely("各平台帖子数 / 互动量", () => chart("c-plat").setOption({
    title: TITLE("各平台帖子数 / 互动量"),
    tooltip: { trigger: "axis" },
    legend: LEGEND(),
    grid: { left: 56, right: 58, top: 42, bottom: 52 },
    xAxis: { type: "category", data: ov.by_platform.map(r => r["平台"]), ...AXIS },
    // 双轴：互动量常常是帖子数的两三个数量级，共用一根轴的话帖子数柱子会被压成一条线
    // 左轴不写 name（会和标题挤在一起），哪根是哪根看图例
    yAxis: [{ type: "value", ...AXIS, ...SPLIT },
    { type: "value", name: "互动量", nameTextStyle: { color: "#8b95a9" }, ...AXIS, splitLine: { show: false } }],
    series: [
      {
        name: "帖子数", type: "bar", barMaxWidth: 22,
        data: ov.by_platform.map(r => ({ value: r["帖子数"], itemStyle: { color: brandColor(r["平台"]) } }))
      },
      {
        name: "互动量", type: "bar", yAxisIndex: 1, barMaxWidth: 22, itemStyle: { color: "#c7d0e4" },
        data: ov.by_platform.map(r => r["互动量"])
      }
    ]
  }, true));

  safely("日历热力", () => calendar(ov, total));
  safely("清洗漏斗 / 分层", () => layerChart(ov));
  safely("主体 × 情感", () => actorCharts(ov));
  safely("声量换算", () => sovCharts(ov));
  safely("TOP 话题 / 作者", () => topCharts(ov));
  safely("平台覆盖与新鲜度", () => coverage(ov));
}

/* 声量换算：柱子按平台，实测和估算分色标出来——估算的量必须一眼能看见，不能混在一起 */
function sovCharts(ov) {
  const s = ov.sov || {};
  const plats = s.by_platform || [];
  $("sovnote").textContent = ov.sov_note || "";
  $("sovconf").textContent =
    `有效内容 ${s.posts || 0} 条，总声量 ${s.total_sov || 0} 分（负面 ${s.neg_sov || 0} 分，`
    + `占 ${s.neg_share || 0}%），日均 ${s.daily_avg || 0} 分；`
    + `其中实测 ${s.measured || 0} 条、估算 ${s.imputed || 0} 条（实测占比 ${s.measured_ratio || 0}%，`
    + `估算全部来自微信公众号 / 百家号——网页侧不给互动数，但不代表没有声量）。`;

  chart("c-sov").setOption({
    title: TITLE("各平台声量分（负面 / 其他 · 虚线为条数）"),
    tooltip: {
      trigger: "axis",
      formatter: p => {
        const r = plats[p[0].dataIndex] || {};
        return `${r.name}<br>声量 ${r.sov}（负面 ${r.neg_sov}）<br>`
          + `条数 ${r.posts}｜实测 ${r.measured} / 估算 ${r.posts - r.measured}`;
      }
    },
    legend: LEGEND(),
    grid: { left: 50, right: 50, top: 42, bottom: 52 },
    xAxis: { type: "category", data: plats.map(p => p.name), ...AXIS },
    yAxis: [{ type: "value", ...AXIS, ...SPLIT },
    { type: "value", ...AXIS, splitLine: { show: false } }],
    series: [
      { name: "负面声量", type: "bar", stack: "s", barMaxWidth: 26,
        itemStyle: { color: SENT_COLOR["负面"] }, data: plats.map(p => p.neg_sov) },
      { name: "其他声量", type: "bar", stack: "s", barMaxWidth: 26,
        itemStyle: { color: "#c7d0e4" },
        data: plats.map(p => Math.round((p.sov - p.neg_sov) * 10) / 10) },
      { name: "条数", type: "line", yAxisIndex: 1, symbol: "circle", symbolSize: 6,
        lineStyle: { type: "dashed", width: 1.4, color: "#59637a" },
        itemStyle: { color: "#59637a" }, data: plats.map(p => p.posts) },
    ]
  }, true);

  const tr = s.trend || [];
  chart("c-sovtrend").setOption({
    title: TITLE("声量趋势（按日 · 换算后）"),
    tooltip: { trigger: "axis" },
    legend: LEGEND(),
    grid: { left: 48, right: 18, top: 42, bottom: 52 },
    xAxis: { type: "category", data: tr.map(r => r.date), ...AXIS },
    yAxis: { type: "value", ...AXIS, ...SPLIT },
    series: [
      { name: "总声量", type: "line", smooth: true, symbol: "none",
        lineStyle: { width: 1.6, color: "#8b95a9" }, data: tr.map(r => r.sov) },
      { name: "负面声量", type: "line", smooth: true, symbol: "none",
        areaStyle: { opacity: .16, color: SENT_COLOR["负面"] },
        lineStyle: { width: 2, color: SENT_COLOR["负面"] }, data: tr.map(r => r.neg_sov) },
    ]
  }, true);
}

/* 主体与情感：颜色固定，跨图一致。政务/官媒用深色（权重高），普通用户用中性灰蓝 */
const ACTOR_COLOR = {
  "官方机构": "#1f3a93", "官方媒体": "#c0392b", "新闻媒体": "#e08a1e",
  "平台企业号": "#0aa2c0", "大V博主": "#7c5cff", "自媒体号": "#0f9960",
  "普通用户": "#59637a", "未判定": "#c7d0e4",
};
const SENT_COLOR = { "负面": "#d9453d", "中性": "#8b95a9", "正面": "#0f9960", "未判定": "#dfe4ee" };
const TOPIC_COLOR = { "广告反馈": "#3d5fd9", "明星应援": "#e0457b", "营销活动": "#e08a1e",
                      "产品其他问题": "#0aa2c0", "修辞误召回": "#8b95a9", "无关": "#8b95a9" };

/* 情感格显示的是**生效值**：人工 ✎ > 模型 AI > 规则。
   来源必须标出来——三套判定混在一个格子里而不标来源，人就没法判断该不该信它。 */
function sentCell(r) {
  // 显示后端算好的生效情感，跟口径筛选同一套值；来源标记只用来告诉人"谁判的"
  let val = r.eff_sentiment || r.sentiment || "未判定", src = "", tip = r.sentiment_reason || "";
  if (r.ai_sentiment) {
    val = r.ai_sentiment; src = "AI";
    tip = `模型判定：${r.ai_reason || "-"}｜规则原判：${r.sentiment || "未判定"}（${r.sentiment_reason || "-"}）`;
  }
  if (r.manual_sentiment) {
    val = r.manual_sentiment; src = "✎";
    tip = `人工改判｜之前：${r.ai_sentiment || r.sentiment || "未判定"}`;
  }
  return `<span class="chip ${r.manual_sentiment ? "man" : ""}" style="--cc:${SENT_COLOR[val] || "#8b95a9"}"
      title="${esc(tip)}">${esc(val)}${src ? " " + src : ""}</span>`;
}

/* 内容类型：模型的类型判定比规则细（能分出"产品其他问题"），有就优先显示 */
function topicCell(r) {
  const val = r.ai_topic || r.topic_kind || "";
  const thin = r.text_thin
    ? `<span class="tag warn" style="margin-left:4px" title="${esc(r.thin_reason || "文本信息不足")}">图?</span>`
    : "";
  if (!val) return '<span class="muted">—</span>' + thin;
  const comp = r.ai_competitor ? '<span class="tag need" style="margin-left:4px">竞品投放</span>' : "";
  return `<span class="chip" style="--cc:${TOPIC_COLOR[val] || "#8b95a9"}"
      title="${esc(r.topic_reason || "")}">${esc(val)}</span>${comp}${thin}`;
}

function actorCharts(ov) {
  const acts = ov.by_actor || [];
  chart("c-actor").setOption({
    title: TITLE("发声主体分布（条数 / 互动量）"),
    tooltip: { trigger: "axis" },
    legend: LEGEND(),
    grid: { left: 96, right: 46, top: 40, bottom: 42 },
    // 双轴：互动量量级远大于条数，共用一根轴会把条数压成看不见的一条线
    xAxis: [{ type: "value", ...AXIS, ...SPLIT },
    { type: "value", position: "top", ...AXIS, splitLine: { show: false } }],
    yAxis: { type: "category", data: acts.map(a => a.name).reverse(), ...AXIS },
    series: [
      {
        name: "条数", type: "bar", barMaxWidth: 14,
        data: acts.slice().reverse().map(a => ({
          value: a.posts, itemStyle: { color: ACTOR_COLOR[a.name] || "#8b95a9" }
        }))
      },
      {
        name: "互动量", type: "bar", xAxisIndex: 1, barMaxWidth: 14,
        itemStyle: { color: "#c7d0e4" },
        data: acts.slice().reverse().map(a => a.engagement)
      }
    ]
  }, true);

  const sents = ov.by_sentiment || [];
  const neg = (sents.find(s => s.name === "负面") || {}).posts || 0;
  const sum = sents.reduce((n, s) => n + s.posts, 0) || 1;
  chart("c-sent").setOption({
    title: TITLE("情感倾向占比（剔噪后全量）"),
    tooltip: { trigger: "item", formatter: p => `${p.name}：${fmt(p.value)} 条（${p.percent}%）` },
    graphic: [{
      type: "text", left: "center", top: "52%",
      style: { text: `负面占比\n${(neg / sum * 100).toFixed(0)}%`, textAlign: "center",
               fill: "#1a1f2b", fontSize: 15, fontWeight: 600, lineHeight: 20 }
    }],
    legend: LEGEND(),
    series: [{
      type: "pie", radius: ["48%", "68%"], center: ["50%", "56%"],
      itemStyle: { borderColor: "#fff", borderWidth: 2 }, label: { show: false },
      data: sents.map(s => ({ name: s.name, value: s.posts,
                              itemStyle: { color: SENT_COLOR[s.name] || "#8b95a9" } }))
    }]
  }, true);

  const mat = ov.actor_sentiment || {};
  const rows = Object.keys(mat).sort((a, b) =>
    (mat[b]["负面"] || 0) - (mat[a]["负面"] || 0));
  chart("c-cross").setOption({
    title: TITLE("谁在说负面（主体 × 情感）"),
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    legend: LEGEND(),
    grid: { left: 96, right: 24, top: 40, bottom: 42 },
    xAxis: { type: "value", ...AXIS, ...SPLIT },
    yAxis: { type: "category", data: rows.slice().reverse(), ...AXIS },
    series: ["负面", "中性", "正面"].map(s => ({
      name: s, type: "bar", stack: "c", barMaxWidth: 16,
      itemStyle: { color: SENT_COLOR[s] },
      data: rows.slice().reverse().map(a => mat[a][s] || 0)
    }))
  }, true);

  const st = ov.sentiment_trend || [];
  chart("c-senttrend").setOption({
    title: TITLE("情感趋势（按日）"),
    tooltip: { trigger: "axis" },
    legend: LEGEND(),
    grid: { left: 48, right: 18, top: 42, bottom: 52 },
    xAxis: { type: "category", data: st.map(r => r.date), ...AXIS },
    yAxis: { type: "value", ...AXIS, ...SPLIT },
    series: ["负面", "中性", "正面"].map(s => ({
      // 不堆叠：堆叠后"正面"的线跑到最上面，看起来像正面最多，容易读错
      name: s, type: "line", smooth: true, symbol: "none",
      areaStyle: s === "负面" ? { opacity: .16, color: SENT_COLOR[s] } : undefined,
      lineStyle: { width: s === "负面" ? 2 : 1.4, color: SENT_COLOR[s],
                   type: s === "正面" ? "dashed" : "solid" },
      itemStyle: { color: SENT_COLOR[s] }, data: st.map(r => r[s] || 0)
    }))
  }, true);
}

/* 日历热力：一眼看出哪些日子根本没数据（切片没扫到 / 平台没给时间），比折线直观 */
function calendar(ov, total) {
  const data = ov.trend.map((r, i) => [r.date, total[i]]);
  const max = Math.max(1, ...total);
  chart("c-cal").setOption({
    title: TITLE("日历热力（每日入口径条数 · 空白=当天无数据）"),
    tooltip: { formatter: p => `${p.value[0]}：${p.value[1]} 条` },
    visualMap: {
      min: 0, max, right: 8, top: 6, orient: "horizontal", itemWidth: 10, itemHeight: 60,
      text: [max, 0], textStyle: { color: "#8b95a9", fontSize: 11 },
      inRange: { color: ["#e8edf9", "#9db6ff", "#2f6bff", "#123a9e"] }
    },
    calendar: {
      top: 52, left: 42, right: 24, cellSize: ["auto", 16],
      range: [$("since").value, $("until").value],
      splitLine: { lineStyle: { color: "#dfe4ee" } },
      itemStyle: { color: "#fff", borderColor: "#eef0f5", borderWidth: 1 },
      yearLabel: { show: false },
      dayLabel: { color: "#8b95a9", fontSize: 10, nameMap: "ZH" },
      monthLabel: { color: "#59637a", fontSize: 11, nameMap: "ZH" }
    },
    series: [{ type: "heatmap", coordinateSystem: "calendar", data }]
  }, true);
}

const LAYER_COLOR = {
  "A_UGC负反馈": "#d9453d", "B_媒体负面报道": "#e08a1e",
  "C_中性事件报道": "#2f6bff", "D_噪音": "#aab3c4", "未分层": "#dfe4ee"
};
/* 平台 × 分层占比：看哪个平台噪音最多，判断清洗规则有没有偏 */
function layerChart(ov) {
  const mat = ov.layer_by_platform || {};
  const plats = Object.keys(mat).sort((a, b) =>
    Object.values(mat[b]).reduce((x, y) => x + y, 0) - Object.values(mat[a]).reduce((x, y) => x + y, 0));
  const keys = Object.keys(LAYER_COLOR).filter(k => plats.some(p => mat[p][k]));
  chart("c-layer").setOption({
    title: TITLE("平台 × 清洗分层（入库全量占比）"),
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    legend: LEGEND({ top: 2, bottom: null, right: 0, left: null }),
    grid: { left: 96, right: 24, top: 46, bottom: 26 },
    xAxis: { type: "value", ...AXIS, ...SPLIT },
    yAxis: { type: "category", data: plats.slice().reverse(), ...AXIS },
    series: keys.map(k => ({
      name: k.replace(/^._/, ""), type: "bar", stack: "l", barMaxWidth: 18,
      itemStyle: { color: LAYER_COLOR[k] },
      data: plats.slice().reverse().map(p => mat[p][k] || 0)
    }))
  }, true);
}

function topCharts(ov) {
  const fun = Object.entries(ov.funnel).sort((a, b) => b[1] - a[1]);
  chart("c-funnel").setOption({
    title: TITLE("清洗漏斗（全量入库按分层拆解）"),
    tooltip: { trigger: "item" },
    series: [{
      type: "pie", radius: ["44%", "64%"], center: ["50%", "56%"],
      itemStyle: { borderColor: "#fff", borderWidth: 2 },
      label: { formatter: "{b}\n{c} 条 ({d}%)", fontSize: 11, color: "#59637a" },
      data: fun.map(([k, v]) => ({ name: k, value: v, itemStyle: { color: LAYER_COLOR[k] } }))
    }]
  }, true);

  const top = filtered().slice(0, 10).reverse();
  chart("c-top").setOption({
    title: TITLE("互动量 TOP10 帖子（点条形打开原帖）"),
    tooltip: {
      trigger: "axis", formatter: p => `${esc(top[p[0].dataIndex].platform_label)} · `
        + `${esc(top[p[0].dataIndex].author || "-")}<br>${esc(p[0].name)}<br>互动量 ${fmt(p[0].value)}`
    },
    grid: { left: 158, right: 30, top: 44, bottom: 24 },
    xAxis: { type: "value", ...AXIS, ...SPLIT },
    yAxis: {
      type: "category", data: top.map(r => (r.title || "").slice(0, 14)),
      axisLabel: { color: "#59637a", fontSize: 11 }, axisLine: { lineStyle: { color: "#e6e9f0" } }
    },
    series: [{
      type: "bar", barMaxWidth: 13,
      data: top.map(r => ({ value: r.engagement, itemStyle: { color: brandColor(r.platform_label) } }))
    }]
  }, true);
  chart("c-top").off("click");
  chart("c-top").on("click", p => {
    const r = top[p.dataIndex];
    if (r && r.url) window.open(r.url, "_blank", "noreferrer");
  });

  const au = (ov.top_authors || []).slice(0, 10).reverse();
  chart("c-author").setOption({
    title: TITLE("TOP 作者（按互动量 · 颜色为平台）"),
    tooltip: {
      trigger: "axis", formatter: p => `${esc(au[p[0].dataIndex].platform_label)} · `
        + `${esc(au[p[0].dataIndex].author)}<br>${au[p[0].dataIndex].posts} 条 · 互动量 ${fmt(p[0].value)}`
    },
    grid: { left: 120, right: 30, top: 44, bottom: 24 },
    xAxis: { type: "value", ...AXIS, ...SPLIT },
    yAxis: {
      type: "category", data: au.map(r => (r.author || "").slice(0, 11)),
      axisLabel: { color: "#59637a", fontSize: 11 }, axisLine: { lineStyle: { color: "#e6e9f0" } }
    },
    series: [{
      type: "bar", barMaxWidth: 13,
      data: au.map(r => ({ value: r.engagement, itemStyle: { color: brandColor(r.platform_label) } }))
    }]
  }, true);
}

/* 覆盖与新鲜度表：把"够不够全、够不够新"落成可核对的数字 */
function coverage(ov) {
  const rows = ov.coverage || [];
  const span = Math.max(1, Math.round(
    (new Date($("until").value) - new Date($("since").value)) / 864e5) + 1);
  const body = document.querySelector("#cov tbody");
  const map = BY_LABEL();
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="8"><div class="empty">还没有入库数据</div></td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => {
    const pct = Math.min(100, Math.round(r["覆盖天数"] / span * 100));
    return `<tr>
      <td><span class="pcell">${logo(map[r["平台"]])}${esc(r["平台"])}</span></td>
      <td class="num">${fmt(r["入库"])}</td><td class="num">${fmt(r["当前口径"])}</td>
      <td class="num">${r["有时间占比"]}%</td><td class="num">${r["覆盖天数"]} / ${span}</td>
      <td>${esc(r["最新发布"]) || '<span class="muted">无</span>'}</td>
      <td>${esc((r["最近采集"] || "").slice(0, 16)) || "-"}</td>
      <td><div class="bar" title="${pct}%"><i style="width:${pct}%;background:${logoColor(r["平台"])}"></i></div></td>
    </tr>`;
  }).join("");
}
window.addEventListener("resize", () => Object.values(CH).forEach(c => c.resize()));

/* ---------------- 关键词云 ---------------- */
/* 不引 echarts-wordcloud 插件（多一个 CDN 依赖），自己做紧凑排布：
   字号按词频用 sqrt 分档（线性会让长尾全挤成一样大），颜色按排名分档，
   位置用"螺旋线找空位 + 矩形碰撞检测"——核心词自然落在中间且字号最大，
   长尾往外圈铺，不再是从左到右平铺的离散标签。 */
const CLOUD_TIERS = [
  { at: 0.00, color: "#e8452f", weight: 800 },   // 榜首：最大最红
  { at: 0.06, color: "#f08519", weight: 750 },
  { at: 0.14, color: "#2f8f6b", weight: 700 },
  { at: 0.26, color: "#3d5fd9", weight: 650 },
  { at: 0.42, color: "#7c5cff", weight: 600 },
  { at: 0.62, color: "#7b8598", weight: 500 },
  { at: 0.82, color: "#a6adbb", weight: 500 },
];
let CLOUD_WORDS = [];

function cloudTier(rank) {
  let hit = CLOUD_TIERS[0];
  for (const t of CLOUD_TIERS) if (rank >= t.at) hit = t;
  return hit;
}

async function loadCloud() {
  CLOUD_WORDS = await api("/api/wordcloud?" + params({ top: 80 }));
  renderCloud();
}

function renderCloud() {
  const box = $("c-cloud");
  if (!box) return;
  const words = CLOUD_WORDS || [];
  if (!words.length) {
    box.innerHTML = '<div class="empty">当前口径没有可提取的词</div>';
    return;
  }
  const W = box.clientWidth || 900, H = box.clientHeight || 340;
  const max = words[0].value, min = words[words.length - 1].value;
  // sqrt 压缩：让第一名突出，同时长尾之间还能看出差别
  const norm = v => Math.sqrt(Math.max(0, v - min) / Math.max(1, max - min));
  const big = Math.min(64, Math.max(34, H * 0.19));
  const cv = document.createElement("canvas").getContext("2d");
  const placed = [];
  const hit = (a) => placed.some(b => a.x < b.x + b.w && a.x + a.w > b.x
    && a.y < b.y + b.h && a.y + a.h > b.y);
  const html = [];
  words.forEach((w, i) => {
    const r = norm(w.value), rank = i / words.length;
    const size = Math.round(13 + r * (big - 13));
    const tier = cloudTier(rank);
    cv.font = `${tier.weight} ${size}px -apple-system,"PingFang SC",sans-serif`;
    const bw = cv.measureText(w.name).width + 8, bh = size * 1.22 + 4;
    // 螺旋找空位：步长跟字号走，大词先放（words 已按频次降序）
    let x = 0, y = 0, ok = false;
    for (let t = 0; t < 2600; t++) {
      const ang = t * 0.35, rad = 3 + t * 0.55;
      x = W / 2 + Math.cos(ang) * rad * 1.5 - bw / 2;
      y = H / 2 + Math.sin(ang) * rad * 0.62 - bh / 2;
      if (x < 2 || y < 2 || x + bw > W - 2 || y + bh > H - 2) continue;
      if (!hit({ x, y, w: bw, h: bh })) { ok = true; break; }
    }
    if (!ok) return;                       // 放不进就不画，宁可少几个长尾词也不重叠
    placed.push({ x, y, w: bw, h: bh });
    html.push(`<span class="cw" style="left:${x.toFixed(1)}px;top:${y.toFixed(1)}px;
      font-size:${size}px;font-weight:${tier.weight};color:${tier.color}"
      title="出现 ${w.value} 次 · 命中 ${w.posts} 帖" onclick="pickWord(${q(w.name)})"
      >${esc(w.name)}</span>`);
  });
  box.innerHTML = `<div class="cloudwrap">${html.join("")}</div>`;
}

function pickWord(word) {
  $("q").value = word;
  go("data");
  loadData();
}

/* ---------------- 采集历史（按采集计划归组） ---------------- */
/* 用户的原话：「按当次进行展示，有对应时间、关键词等内容，点击就直接跳转到首页
   进行对应数据展示，而不是一个个渠道记录的逻辑，渠道记录可以归到对应爬取计划里面」。
   所以主体是计划卡片，渠道流水折进计划里，点计划整卡＝把看板切到这一批数据。 */
let PLANS = [];
let RUNFILTER = "";

async function loadRuns() {
  PLANS = await api("/api/plans?limit=800");
  renderRunStat();
  renderRuns();
}

function renderRunStat() {
  const saved = PLANS.reduce((n, p) => n + p.saved, 0);
  const combos = PLANS.reduce((n, p) => n + p.combos, 0);
  const okCombos = PLANS.reduce((n, p) => n + p.ok_combos, 0);
  const last = PLANS[0] || {};
  const cards = [
    { c: "#3d5fd9", n: fmt(PLANS.length), t: "采集计划次数",
      d: `共 ${fmt(combos)} 个平台×关键词组合` },
    { c: "#178a5f", n: fmt(saved), t: "累计入库条数",
      d: `有效组合 ${okCombos} / ${combos}（${combos ? Math.round(okCombos / combos * 100) : 0}%）` },
    { c: "#e08a1e", n: last.keywords ? last.keywords.length : 0, t: "最近一次关键词数",
      d: last.keywords ? last.keywords.join(" / ").slice(0, 26) : "暂无记录" },
    { c: "#0aa2c0", n: (last.finished_at || "—").slice(5, 16), t: "最近一次完成",
      d: last.platforms ? `${last.platforms.length} 个平台 · 入库 ${fmt(last.saved)}` : "暂无记录" },
  ];
  $("runstat").innerHTML = cards.map(x => `<div class="kpi">
    <div class="top"><div class="ico" style="background:linear-gradient(140deg,${x.c},${x.c}bb)"></div>
      <div style="min-width:0"><div class="n">${x.n}</div><div class="t">${esc(x.t)}</div></div></div>
    <div class="d">${esc(x.d)}</div></div>`).join("");
}

function renderRuns() {
  const kw = ($("runq").value || "").trim();
  let rows = PLANS;
  if (kw) {
    rows = rows.filter(p => [...p.keywords, ...p.platforms, p.window, p.id]
      .some(v => String(v || "").includes(kw)));
  }
  if (RUNFILTER === "ok") rows = rows.filter(p => p.saved > 0);
  if (RUNFILTER === "fail") rows = rows.filter(p => !p.saved || p.failed_combos);
  $("runinfo").textContent = `显示 ${rows.length} / ${PLANS.length} 次采集计划`;
  if (!rows.length) {
    $("runs").innerHTML = '<div class="empty">没有符合条件的采集计划</div>';
    return;
  }
  const days = {};
  rows.forEach(p => {
    const d = (p.finished_at || "").slice(0, 10) || "未知日期";
    (days[d] = days[d] || []).push(p);
  });
  $("runs").innerHTML = Object.entries(days).map(([day, list]) => `
    <div class="runday">
      <div class="dayhd"><span class="dot"></span>${esc(day)}
        <span class="muted">${list.length} 次计划 · 入库 ${fmt(list.reduce((n, p) => n + p.saved, 0))} 条</span></div>
      ${list.map(p => planCard(p)).join("")}
    </div>`).join("");
}

function planCard(p) {
  const bad = !p.saved;
  const map = BY_LABEL();
  const start = (p.started_at || "").slice(11, 16);
  const end = (p.finished_at || "").slice(11, 16);
  return `<div class="planitem ${bad ? "bad" : ""}">
    <div class="phd" onclick="viewPlan(${q(p.id)})" title="点击把看板切到这次采集的数据">
      <div class="ptime"><b>${esc(end || "—")}</b><span>${esc(start)} 起</span></div>
      <div class="pmid">
        <div class="tt">
          ${p.keywords.length
            ? p.keywords.map(k => `<span class="kw">${esc(k)}</span>`).join("")
            : '<span class="muted">无关键词</span>'}
          ${bad ? '<span class="tag need">未入库</span>'
            : `<span class="tag ok">入库 ${fmt(p.saved)}</span>`}
          ${p.legacy ? '<span class="tag free" title="旧记录没有计划号，这里按「同一小时+同一时间窗」推断归组">推断归组</span>' : ""}
        </div>
        <div class="sub">
          ${p.platforms.length} 个平台 · ${p.combos} 个组合（有效 ${p.ok_combos}${p.failed_combos ? `、失败 ${p.failed_combos}` : ""}）
          · 抓到 ${fmt(p.got)} / 入库 ${fmt(p.saved)}
          · 发布时间窗 ${esc(p.window || "未限定")}${p.limit_n ? ` · 每词上限 ${p.limit_n}` : ""}
        </div>
        <div class="plogos">${p.platforms.map(l => `<span class="pcell" title="${esc(l)}">${logo(map[l])}</span>`).join("")}</div>
      </div>
      <div class="pact">
        <button class="btn sm" onclick="event.stopPropagation();viewPlan(${q(p.id)})">看板查看</button>
        <button class="btn ghost sm" onclick="event.stopPropagation();togglePlan(${q(p.id)})">渠道明细</button>
        <span class="expgrp" title="只导这一次采集到的帖子，不用去看板上调日期区间">
          <button class="btn ghost sm" ${p.saved ? "" : "disabled"}
            onclick="event.stopPropagation();exportPlan(${q(p.id)},'xlsx')">导 Excel</button>
          <button class="btn ghost sm" ${p.saved ? "" : "disabled"}
            onclick="event.stopPropagation();exportPlan(${q(p.id)},'csv')">CSV</button>
          <button class="btn ghost sm" ${p.saved ? "" : "disabled"}
            onclick="event.stopPropagation();exportPlan(${q(p.id)},'jsonl')">JSONL</button>
        </span>
        <button class="btn ghost sm del" title="删除这条计划的采集流水（库里的帖子数据不受影响）"
          onclick="event.stopPropagation();delPlan(${q(p.id)},${q(p.keywords.join("、"))},${p.combos})">删除</button>
      </div>
    </div>
    <div class="pchan" id="chan-${esc(p.id)}">
      <table><thead><tr><th>平台</th><th>关键词</th><th class="num">抓到</th><th class="num">入库</th>
        <th>完成</th><th>备注</th></tr></thead><tbody>
        ${p.channels.map(c => `<tr class="${c.failed ? "bad" : ""}">
          <td><span class="pcell">${logo(c.platform)}${esc(c.platform_label)}</span></td>
          <td>${esc(c.keyword)}</td><td class="num">${fmt(c.got)}</td>
          <td class="num"><b>${fmt(c.saved)}</b></td><td>${esc(c.at)}</td>
          <td class="muted">${esc(c.note)}</td></tr>`).join("")}
      </tbody></table>
    </div>
  </div>`;
}

/* 按采集计划导出：点哪一批导哪一批，不经过看板的日期筛选。
   导完直接把浏览器指向下载地址——多一步"去导出文件列表里找"没有必要，
   文件本身也还留在列表里，需要重下再去那儿拿。
   形参故意叫 kind 不叫 fmt：fmt 是全局的千分位格式化函数，
   同名会把它遮掉，下面 fmt(r.rows) 就变成"拿字符串当函数调"。 */
async function exportPlan(id, kind) {
  toast("正在生成，稍等…", "ok");
  const r = await api(`/api/plan/export?id=${encodeURIComponent(id)}&fmt=${kind}`);
  if (!r.ok) { toast(r.msg || "导出失败", "err"); return; }
  toast(`已导出 ${fmt(r.rows)} 条 → ${r.file}`, "ok");
  location.href = "/api/download?file=" + encodeURIComponent(r.file);
  if (typeof loadFiles === "function") loadFiles();
}

/* 删除采集计划＝删这批渠道流水（台账），**不删帖子**。
   posts 里没有批次归属字段，按「平台×关键词×时间窗」反查会连带删掉别的计划抓到的同一批内容，
   所以这里只清台账，数据要删走「帖子明细」的逐条删除或回收站。 */
async function delPlan(id, kws, combos) {
  const tip = [`删除这条采集计划的记录？`, ``,
    `计划：${kws}（${combos} 条渠道流水）`, ``,
    "只删台账记录，库里的帖子数据不受影响。",
    "注意：删掉后这段时间会被判成「没采集过」，周期对比里会重新变成缺口。"].join("\n");
  if (!confirm(tip)) return;
  const r = await api("/api/plan/delete", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ batch_id: id })
  });
  if (!r.ok) { toast(r.msg || "删除失败", "err"); return; }
  toast(r.msg, "ok");
  await loadRuns();
  loadData();
}

function togglePlan(id) {
  const el = document.getElementById("chan-" + id);
  if (el) el.classList.toggle("on");
}

/* 点计划 → 跳看板并把数据换成这一批：关键词集合 + 平台集合 + 发布时间窗一起套上 */
function viewPlan(id) {
  const p = PLANS.find(x => x.id === id);
  if (!p) return;
  const byLabel = BY_LABEL();
  PLAN = {
    id: p.id, keywords: p.keywords,
    plat_names: p.platforms.map(l => byLabel[l]).filter(Boolean),
    label: `${(p.finished_at || "").slice(5, 16)}｜${p.keywords.join("、") || "无关键词"}`
      + `｜${p.platforms.length} 平台`,
  };
  if (p.since && p.until) {
    $("since").value = p.since; $("until").value = p.until;
    document.querySelectorAll("#quickdate button").forEach(x => x.classList.remove("on"));
  }
  $("kwfilter").value = "";
  go("dash");
  loadData();
  toast(`看板已切到这次采集：${PLAN.label}`, "ok");
}

/* 从历史记录一键重跑：把参数填回采集表单，用户确认后自己点开始（不直接触发浏览器任务） */
function replayPlan(id) {
  const p = PLANS.find(x => x.id === id);
  if (!p) return;
  const byLabel = BY_LABEL();
  go("crawl");
  $("kw").value = p.keywords.join(",");
  const names = new Set(p.platforms.map(l => byLabel[l]));
  document.querySelectorAll("#pchk input").forEach(i => { i.checked = names.has(i.value); });
  if (p.since) { $("csince").value = p.since; $("cuntil").value = p.until; }
  if (p.limit_n) $("limit").value = p.limit_n;
  toast("参数已填回采集表单，确认后点「开始采集」");
}
$("runfilter").addEventListener("click", e => {
  const b = e.target.closest("button[data-f]");
  if (!b) return;
  RUNFILTER = b.dataset.f;
  document.querySelectorAll("#runfilter button").forEach(x => x.classList.toggle("on", x === b));
  renderRuns();
});

/* 导出：跟着当前口径 + 区间 + 计划筛选一起走，导出的就是屏幕上看到的这批 */
async function exportRuns() {
  const r = await api("/api/export?" + params({ jsonl: "0" }));
  if (!r.ok) { toast(r.msg, "err"); return; }
  toast(`已按当前口径与区间导出 ${r.rows} 条`, "ok");
  loadFiles();
}

/* ---------------- 事件归因 + 重点负面 ---------------- */
/* 事件卡：每张卡都必须能回答"这是什么事件、凭什么这么归、谁引爆的"，
   所以卡上同时写：归因说明、命中的证据词、条数/影响力、起止日、参与平台、引爆帖。
   已知场景来自词表，自动聚类来自数据里真实出现的短语——两种都标出来，不混着当结论。 */
function renderEvents(ov) {
  const evs = ov.events || [];
  const im = ov.impact || {};
  $("eventnote").textContent = im.note || "";
  if (!evs.length) {
    $("events").innerHTML = '<div class="empty">当前口径没有负面内容，无法做事件归因</div>';
    return;
  }
  const max = evs[0].impact || 1;
  const map = BY_LABEL();
  $("events").innerHTML = evs.map((e, i) => `<div class="evt ${e.kind === "已知场景" ? "known" : "auto"}">
    <div class="ehd">
      <span class="rank">${i + 1}</span>
      <span class="etitle">
        <b>${esc(e.name)}</b>
        <span class="tag ${e.kind === "已知场景" ? "brand" : "free"}">${esc(e.kind)}</span>
      </span>
      <span class="sp muted">${esc(e.first_day)} ~ ${esc(e.last_day)}</span>
    </div>
    <div class="ebar"><i style="width:${Math.max(3, e.impact / max * 100).toFixed(1)}%"></i></div>
    <div class="emeta">
      <span><b>${fmt(e.posts)}</b> 条</span>
      <span>影响力 <b>${fmt(e.impact)}</b></span>
      <span>互动量 <b>${fmt(e.engagement)}</b></span>
      ${e.high_posts ? `<span class="hi">含 <b>${e.high_posts}</b> 条重点负面</span>` : ""}
      <span class="plats">${e.platforms.slice(0, 6).map(p =>
        `<span class="pcell" title="${esc(p.name)}：${p.posts} 条">${logo(map[p.name])}${p.posts}</span>`).join("")}</span>
    </div>
    <div class="ewhy">${esc(e.why)}</div>
    <div class="eev">证据词：${e.evidence.map(w => `<code>${esc(w)}</code>`).join(" ") || "—"}</div>
    <div class="elead" onclick="openPost(${q(e.lead.url)})" title="点开引爆帖原文">
      <span class="tag warn">引爆帖</span>
      ${logo(map[e.lead.platform])}
      <span class="t">${esc(e.lead.title || "（无标题）")}</span>
      <span class="n">${fmt(e.lead.engagement)}</span>
    </div>
  </div>`).join("");
}

function renderHotNegative(ov) {
  const rows = ov.hot_negative || [];
  const map = BY_LABEL();
  if (!rows.length) {
    $("hotneg").innerHTML = '<div class="empty">当前口径没有可排序的负面内容</div>';
    return;
  }
  $("hotneg").innerHTML = `<table><thead><tr>
      <th></th><th>平台</th><th>发布</th><th>标题</th><th>主体</th><th>归因场景</th>
      <th class="num">互动量</th><th class="num">负面影响力</th></tr></thead><tbody>
    ${rows.map((r, i) => `<tr class="${r.high ? "hi" : ""}">
      <td>${r.high ? '<span class="lay lay-A">重点</span>' : `<span class="muted">${i + 1}</span>`}</td>
      <td><span class="pcell">${logo(map[r.platform_label])}${esc(r.platform_label)}</span></td>
      <td>${esc((r.publish_time || "").slice(0, 10))}</td>
      <td><a href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc((r.title || "").slice(0, 46))}</a>
        <span class="t2">${esc(r.author || "匿名")}｜${esc(r.reason || "")}</span></td>
      <td><span class="chip" style="--cc:${ACTOR_COLOR[r.actor] || "#8b95a9"}">${esc(r.actor || "未判定")}</span></td>
      <td>${r.scene === "未归类" ? '<span class="muted">未归类</span>' : esc(r.scene)}</td>
      <td class="num">${fmt(r.engagement)}</td>
      <td class="num"><b>${fmt(r.impact)}</b></td></tr>`).join("")}
  </tbody></table>`;
}

/* ---------------- 重点预警监控 ---------------- */
/* 两条入池通道（后端 alerts.summarize）：内容触线 / 声量大。
   这里只做展示与本地筛选，不再发请求——切类别时不能有等待感。 */
const ALERT_COLOR = {
  porn: "#e0457b", competitor: "#3b82f6", scare: "#8b5cf6",
  fraud: "#ef4444", politics: "#b45309", minor: "#0d9488",
};
let ALERT_FILTER = "all";

function alertFilter(key) {
  ALERT_FILTER = key;
  renderAlertList();
  renderAlertSeg();
}

function renderAlertSeg() {
  const a = (LAST_OV && LAST_OV.alerts) || {};
  const cats = a.cats || [];
  const btn = (k, text, n, color) => `<button class="acseg${ALERT_FILTER === k ? " on" : ""}"
      style="--cc:${color}" onclick="alertFilter(${q(k)})">${esc(text)}<i>${fmt(n)}</i></button>`;
  $("alertseg").innerHTML = [
    btn("all", "全部", a.total || 0, "#6b7280"),
    ...cats.map(c => btn(c.key, c.name, c.posts, ALERT_COLOR[c.key] || "#6b7280")),
    a.big_only ? btn("big", "仅声量大", a.big_only, "#f59e0b") : "",
  ].join("");
}

function renderAlertList() {
  const a = (LAST_OV && LAST_OV.alerts) || {};
  let items = a.items || [];
  if (ALERT_FILTER === "big") items = items.filter(x => x.big && !(x.cats || []).length);
  else if (ALERT_FILTER !== "all") items = items.filter(x => (x.cats || []).some(c => c.key === ALERT_FILTER));
  const map = BY_LABEL();
  if (!items.length) {
    $("alertlist").innerHTML = '<div class="empty">该筛选下没有需要重点盯的反馈</div>';
    return;
  }
  $("alertlist").innerHTML = `<table><thead><tr>
      <th>命中类别</th><th>平台</th><th>发布</th><th>标题</th>
      <th>入选理由（命中词可核对）</th><th class="num">互动量</th><th class="num">影响力</th>
    </tr></thead><tbody>
    ${items.map(x => `<tr class="${x.severity >= 3 ? "hi" : ""}">
      <td>${(x.cats || []).map(c => `<span class="acdot" style="--cc:${ALERT_COLOR[c.key] || "#6b7280"}"
          title="${esc(c.why)}">${esc(c.name)}</span>`).join("")
        || '<span class="lay lay-A">声量大</span>'}</td>
      <td><span class="pcell">${logo(map[x.platform_label])}${esc(x.platform_label || "")}</span></td>
      <td>${esc((x.publish_time || "").slice(0, 10))}</td>
      <td><a href="${esc(x.url)}" target="_blank" rel="noreferrer">${esc((x.title || "（无标题）").slice(0, 44))}</a></td>
      <td class="acwhy">${esc(x.reason || "")}</td>
      <td class="num">${fmt(x.engagement)}</td>
      <td class="num"><b>${fmt(x.impact)}</b></td></tr>`).join("")}
  </tbody></table>`;
}

function renderAlerts(ov) {
  const a = ov.alerts || {};
  const cats = a.cats || [];
  // 表头只写数和一句口径，完整口径挂 title，避免顶栏被长文本挤成省略号
  $("alertnote").textContent = `${fmt(a.total || 0)} 条待盯 · 内容触线 ${fmt(a.risky || 0)}`
    + ` · 仅声量大 ${fmt(a.big_only || 0)}`;
  $("alertnote").title = a.note || "";
  if (!(a.total || 0)) {
    $("alertseg").innerHTML = "";
    $("alertcats").innerHTML = '<div class="empty">当前口径没有触线内容，也没有超过高影响门槛的负面帖</div>';
    $("alertlist").innerHTML = "";
    return;
  }
  const maxP = cats.reduce((m, c) => Math.max(m, c.posts), 1);
  const map = BY_LABEL();
  $("alertcats").innerHTML = cats.map(c => `<div class="acard${ALERT_FILTER === c.key ? " on" : ""}"
      style="--cc:${ALERT_COLOR[c.key] || "#6b7280"}" onclick="alertFilter(${q(c.key)})"
      title="点击只看这一类">
    <div class="achd"><b>${esc(c.name)}</b><span class="tag sev${c.severity}">严重度${esc(c.sev_text)}</span></div>
    <div class="acnum"><b>${fmt(c.posts)}</b> 条 <span class="muted">· 影响力 ${fmt(c.impact)}</span></div>
    <div class="acbar"><i style="width:${Math.max(4, c.posts / maxP * 100).toFixed(1)}%"></i></div>
    <div class="acwords">${c.words.map(w => `<code>${esc(w[0])}<i>${w[1]}</i></code>`).join("") || "—"}</div>
    <div class="acplats">${c.plats.map(p =>
      `<span class="pcell" title="${esc(p[0])}：${p[1]} 条">${logo(map[p[0]])}${p[1]}</span>`).join("")}</div>
    <div class="acwhy">${esc(c.why)}</div>
  </div>`).join("");
  renderAlertSeg();
  renderAlertList();
}

/* 口径与清洗说明抽屉：内容来自后端 clean_rules()，页面上不写第二份说明，避免两处对不上 */
function openScopeDoc() {
  const cr = CLEAN_RULES;
  const cur = (cr.scopes || []).find(s => s.key === SCOPE);
  const dd = LAST_OV.dedupe || {}, syn = LAST_OV.syndication || {};
  const synMax = (syn.top || []).reduce((m, t) => Math.max(m, t.copies), 1);
  $("docbody").innerHTML = `
    <div class="dsec">
      <h4>当前口径：${esc(cur ? cur.name : SCOPE)}</h4>
      <p>${esc(cur ? cur.why : "")}</p>
      <p class="muted">包含分层：${(cur && cur.layers.length ? cur.layers : ["全部"])
        .map(l => `<span class="lay lay-${l[0]}">${esc(l.replace(/^._/, ""))}</span>`).join(" ")}</p>
    </div>
    <div class="dsec">
      <h4>去重与同题转载</h4>
      <p>${esc(dd.note || "—")}</p>
      <p class="muted">全库 <b>${fmt(dd.raw || 0)}</b> 条，其中 <b>${fmt(dd.dup || 0)}</b> 条判为重复、
        已从所有口径与导出中排除。</p>
      ${syn.groups ? `<p style="margin-top:10px"><b>同题转载 ${syn.groups} 组，多出 ${syn.extra_posts} 条</b>
        —— 标题相同但作者不同，是多个号转载同一篇稿。<b>不自动去重</b>：转载本身就是真实扩散，
        去掉会低估声量。如果你要按"一篇稿算一条"统计，去「参数配置 › 去重」把
        「作者参与指纹」关掉，系统会把它们折成一条。</p>
        <div class="synlist">${(syn.top || []).map(t => `<div class="syn">
          <div class="st" title="${esc(t.title)}">${esc(t.title)}</div>
          <div class="sb"><i style="width:${(t.copies / synMax * 100).toFixed(0)}%"></i>
            <span>${t.copies} 条 / ${t.authors} 个号 · ${esc(t.platforms.join("、"))}
              · 互动量 ${fmt(t.engagement)}</span></div>
        </div>`).join("")}</div>` : ""}
    </div>
    <div class="dsec">
      <h4>数据是怎么被剔掉 / 归类的</h4>
      <ol class="steps">${(cr.steps || []).map(s => `<li>
        <b>${esc(s.stage)}</b>
        <div>${esc(s.what)}</div>
        <div class="muted">判定：<code>${esc(s.how)}</code> → 结果：<b>${esc(s.to)}</b></div>
      </li>`).join("")}</ol>
    </div>
    <div class="dsec">
      <h4>五个分层的含义</h4>
      <ul>${Object.entries(cr.layers || {}).map(([k, v]) =>
        `<li><span class="lay lay-${k[0]}">${esc(k.replace(/^._/, ""))}</span> ${esc(v)}</li>`).join("")}</ul>
    </div>
    <div class="dsec">
      <h4>其他口径</h4>
      <ul>${(cr.scopes || []).filter(s => s.key !== SCOPE).map(s =>
        `<li><a onclick="pickScope('${s.key}');closeScopeDoc()"><b>${esc(s.name)}</b></a> — ${esc(s.why)}</li>`).join("")}</ul>
    </div>`;
  $("docdrawer").classList.add("on");
}
function closeScopeDoc() { $("docdrawer").classList.remove("on"); }

/* ---------------- 周期对比 ---------------- */
/* 一个必须守住的原则：**有采集缺口的周期不参与对比**。
   否则"上周声量掉了 60%"可能只是那几天没抓，结论会直接把人带错。
   所以后端给每个周期算 coverage（full/partial/none），这里：
     - 图上：可比周期实色柱，不可比周期灰色 + 标注缺失天数
     - 基准/对比下拉：不可比的周期 disabled（置灰不可选）
     - 提示条：写清哪几个周期有缺口、缺多少天、去哪儿补 */
let CMP = { periods: [], grain: "week" };

$("cmpgrain").addEventListener("click", e => {
  const b = e.target.closest("button[data-g]");
  if (!b) return;
  document.querySelectorAll("#cmpgrain button").forEach(x => x.classList.toggle("on", x === b));
  $("cmpdays").style.display = b.dataset.g === "custom" ? "" : "none";
  loadCompare();
});

function cmpGrain() {
  const on = document.querySelector("#cmpgrain button.on");
  const g = on ? on.dataset.g : "week";
  return g === "custom" ? String(+$("cmpdays").value || 10) : g;
}

async function loadCompare() {
  CMP = await api("/api/compare?" + params({ grain: cmpGrain() }));
  renderCompare();
}

const CMP_LABEL = { posts: "帖子数", sov: "声量分", neg_sov: "负面声量", engagement: "互动量" };

function renderCompare() {
  const ps = CMP.periods || [];
  const key = $("cmpmetric").value;
  const bad = ps.filter(p => !p.comparable);
  // 提示只留"必须知道的一件事"：有几个周期不能比、缺多少天、去哪补。
  // 口径说明（交集/基线标定）挪进 ? 的悬浮提示，不占版面。
  const why = `${CMP.scope_note || ""} ${CMP.sov_note || ""}`.trim();
  const el = $("cmphint");
  if (!ps.length) {
    el.className = "hint";
    el.innerHTML = "当前区间切不出周期，检查一下日期范围。";
  } else if (bad.length) {
    el.className = "hint";
    el.innerHTML = `<b>${CMP.comparable}/${ps.length}</b> 个周期可比；`
      + `<b>${bad.length}</b> 个有采集缺口已置灰、不计环比：`
      + bad.slice(0, 3).map(p => `${esc(p.label)}<span class="muted">缺${p.missing}天</span>`).join("、")
      + (bad.length > 3 ? ` 等 ${bad.length} 个` : "")
      + ` <a onclick="go('crawl');$('gaponly').checked=true;previewGap()">去补缺口 ›</a>`
      + ` <i class="q" title="${esc(why)}">?</i>`;
  } else {
    el.className = "hint info";
    el.innerHTML = `<b>${ps.length}</b> 个周期全部完整采集，可直接对比。`
      + ` <i class="q" title="${esc(why)}">?</i>`;
  }

  renderCmpPair();
  safely("周期对比", () => chart("c-cmp").setOption({
    title: TITLE(`${CMP_LABEL[key]} 周期对比（灰色＝有采集缺口，不可比）`),
    tooltip: {
      trigger: "axis",
      formatter: a => {
        const p = ps[a[0].dataIndex] || {};
        const d = p[key + "_delta"];
        return `${esc(p.label)}（${p.since} ~ ${p.until}）<br>`
          + `${CMP_LABEL[key]} <b>${fmt(p[key])}</b>`
          + (d === null || d === undefined ? "<br>环比：无（相邻周期有缺口）"
            : `<br>环比 ${d > 0 ? "↑" : d < 0 ? "↓" : "—"} ${Math.abs(d).toFixed(1)}%`)
          + `<br>采集覆盖：${p.coverage === "full" ? "完整"
            : p.coverage === "none" ? "整段未采集" : `缺 ${p.missing} / ${p.full_days} 天`}`;
      }
    },
    legend: LEGEND(),
    grid: { left: 52, right: 52, top: 42, bottom: 52 },
    xAxis: { type: "category", data: ps.map(p => p.label), ...AXIS },
    yAxis: [{ type: "value", ...AXIS, ...SPLIT },
    { type: "value", name: "环比%", nameTextStyle: { color: "#8b95a9" }, ...AXIS, splitLine: { show: false } }],
    series: [
      {
        name: CMP_LABEL[key], type: "bar", barMaxWidth: 30,
        data: ps.map(p => ({
          value: p[key],
          // 不可比周期用浅灰，避免把"没抓"画成"没声量"
          itemStyle: { color: p.comparable ? "#2f6bff" : "#dfe4ee",
                       borderColor: p.comparable ? "transparent" : "#c7d0e4",
                       borderType: "dashed", borderWidth: p.comparable ? 0 : 1 },
        })),
      },
      {
        // 环比统一「红涨绿降」：线本身用中性灰，涨跌用点的颜色表达，避免整条线一个色看不出方向
        name: "环比%", type: "line", yAxisIndex: 1, connectNulls: false,
        symbol: "circle", symbolSize: 8,
        lineStyle: { width: 2, color: "#9aa4b8", type: "dashed" },
        data: ps.map(p => {
          const d = p[key + "_delta"];
          const c = d === null || d === undefined ? "#9aa4b8"
            : d > 0.05 ? "#cf4a44" : d < -0.05 ? "#178a5f" : "#9aa4b8";
          return { value: d, itemStyle: { color: c, borderColor: c } };
        }),
      },
    ]
  }, true));

  const body = document.querySelector("#cmptbl tbody");
  body.innerHTML = ps.map(p => `<tr class="${p.comparable ? "" : "dim"}">
    <td><b>${esc(p.label)}</b></td>
    <td class="muted">${esc(p.since)} ~ ${esc(p.until)}</td>
    <td class="num">${fmt(p.posts)}</td><td class="num">${deltaTag(p.posts_delta, false) || "—"}</td>
    <td class="num">${fmt(p.sov)}</td><td class="num">${deltaTag(p.sov_delta, false) || "—"}</td>
    <td class="num">${fmt(p.neg_sov)}</td><td class="num">${deltaTag(p.neg_sov_delta, true) || "—"}</td>
    <td>${p.coverage === "full"
      ? `<span class="tag ok">完整${p.source === "run" ? "（有流水）" : ""}</span>`
      : p.coverage === "none" ? '<span class="tag need">整段未采集</span>'
        : `<span class="tag warn" title="${esc((p.missing_days || []).join("、"))}">缺 ${p.missing} 天</span>`}</td>
  </tr>`).join("");
}

/* 任选两个周期直接对比：不可比的周期在下拉里 disabled，选不了 */
function renderCmpPair() {
  const ps = CMP.periods || [];
  const ok = ps.filter(p => p.comparable);
  const opts = (sel) => ps.map(p => `<option value="${esc(p.label)}"
      ${p.comparable ? "" : "disabled"} ${p.label === sel ? "selected" : ""}
      >${esc(p.label)}${p.comparable ? "" : `（缺 ${p.missing} 天，不可选）`}</option>`).join("");
  const a = (window.CMP_A && ps.some(p => p.label === CMP_A && p.comparable)) ? CMP_A
    : (ok.length > 1 ? ok[ok.length - 2].label : (ok[0] || {}).label);
  const b = (window.CMP_B && ps.some(p => p.label === CMP_B && p.comparable)) ? CMP_B
    : (ok[ok.length - 1] || {}).label;
  window.CMP_A = a; window.CMP_B = b;
  const pa = ps.find(p => p.label === a), pb = ps.find(p => p.label === b);
  const cells = ["posts", "sov", "neg_sov", "engagement"].map(k => {
    if (!pa || !pb) return "";
    const va = pa[k], vb = pb[k];
    const d = va ? (vb - va) / va * 100 : null;
    return `<div class="cmpcell">
      <div class="t">${CMP_LABEL[k]}</div>
      <div class="v">${fmt(va)} <span class="ar">→</span> <b>${fmt(vb)}</b></div>
      <div class="d">${d === null ? '<span class="muted">基准为 0，无法算变化</span>'
        : deltaTag(Math.round(d * 10) / 10, k === "neg_sov")}</div>
    </div>`;
  }).join("");
  $("cmppair").innerHTML = ok.length < 2
    ? `<div class="hint" style="margin-bottom:12px">可比周期不足 2 个，先把缺口补上再做两期对比。</div>`
    : `<div class="cmpsel">
        <span>基准期</span>
        <select onchange="CMP_A=this.value;renderCmpPair()">${opts(a)}</select>
        <span>对比期</span>
        <select onchange="CMP_B=this.value;renderCmpPair()">${opts(b)}</select>
      </div>
      <div class="cmpgrid">${cells}</div>`;
}

/* ---------------- 增量采集：缺口预估 ---------------- */
async function previewGap() {
  const box = $("gapbox");
  if (!$("gaponly").checked) { box.innerHTML = ""; return; }
  const kws = $("kw").value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
  const plats = picked();
  const since = $("csince").value, until = $("cuntil").value;
  if (!kws.length || !plats.length || !since || !until) {
    box.innerHTML = '<div class="hint">要先填关键词、选平台、给出采集起止日，才能算缺口。</div>';
    return;
  }
  box.innerHTML = '<div class="hint info">正在按渠道核对采集流水…</div>';
  const p = await api("/api/gap-plan?" + new URLSearchParams({
    since, until, keywords: kws.join("|"), platforms: plats.join(",")
  }));
  if (!p.combos.length) {
    box.innerHTML = `<div class="hint info"><b>${since} ~ ${until}</b> 内，选中的
      ${plats.length} 个渠道 × ${kws.length} 个关键词都已完整采集过，本次无需抓取。</div>`;
    return;
  }
  const days = p.combos.reduce((n, c) => n + c.missing_days, 0);
  box.innerHTML = `<div class="gaphd">
      本次只抓 <b>${p.combos.length}</b> 个渠道×关键词组合、共 <b>${p.requests}</b> 段日期、
      累计 <b>${days}</b> 个缺失日；跳过 <b>${p.skip.length}</b> 个已完整覆盖的组合
      （判定按采集流水，不靠"有没有数据"猜）
    </div>
    <div class="gaplist">
      ${p.combos.map(c => `<div class="gapitem">
        <span class="pcell">${logo(c.platform)}${esc(c.platform_label)}</span>
        <span class="kw">${esc(c.keyword)}</span>
        <span class="ds">缺 ${c.missing_days}/${c.total_days} 天</span>
        <span class="rg">${c.ranges.map(r => `${r.since.slice(5)}~${r.until.slice(5)}`).join("、")}</span>
        ${c.note ? `<span class="muted">${esc(c.note)}</span>` : ""}
      </div>`).join("")}
      ${p.skip.map(s => `<div class="gapitem skip">
        <span class="pcell">${logo(s.platform)}${esc(s.platform_label)}</span>
        <span class="kw">${esc(s.keyword)}</span>
        <span class="ds">已完整</span>
        <span class="muted">${esc(s.reason)} · 跳过</span>
      </div>`).join("")}
    </div>`;
}

/* ---------------- 数据加载 ---------------- */
/* 当前锁定的采集计划：从「历史与导出」点某个计划进来时设置，
   之后看板/明细/词云/导出全都跟着这一批走。null＝不限定。 */
let PLAN = null;
let CLEAN_RULES = { scopes: [], steps: [], layers: {} };
let LAST_OV = {};      // 最近一次 overview，抽屉等弹层直接读它，不再重复请求

function params(extra = {}) {
  const p = Object.assign({
    since: $("since").value, until: $("until").value, scope: SCOPE,
    keyword: $("kwfilter").value, q: $("q").value.trim()
  }, extra);
  if (PLAN) {
    // 计划维度用 keywords（多词，| 分隔）+ platforms 还原，比单个 keyword 精确
    if (PLAN.keywords && PLAN.keywords.length) p.keywords = PLAN.keywords.join("|");
    if (PLAN.plat_names && PLAN.plat_names.length) p.platforms = PLAN.plat_names.join(",");
  }
  return new URLSearchParams(p);
}

function clearPlan() {
  PLAN = null;
  loadData();
  toast("已取消计划筛选，回到全部数据");
}

/* 口径下拉与三个常用口径互斥；选了下拉里的口径就把 seg 的高亮取消 */
function pickScope(v) {
  if (!v) return;
  SCOPE = v;
  document.querySelectorAll("#scopeseg button").forEach(x => x.classList.remove("on"));
  loadData();
}

/* 面包屑：把"当前看的是哪一批数据"显式写出来，避免多关键词混看时看错 */
function renderCrumb(ov) {
  const kw = $("kwfilter").value;
  const parts = [
    `<span class="crumb" onclick="openScopeDoc()" title="点开看这个口径怎么清洗、剔了什么">
      口径 <b>${esc(SCOPE_NAME[SCOPE] || SCOPE)}</b> <i>?</i></span>`,
    `<span class="crumb">区间 <b>${esc($("since").value)} ~ ${esc($("until").value)}</b></span>`,
  ];
  if (PLAN) {
    parts.push(`<span class="crumb plan">采集计划 <b>${esc(PLAN.label)}</b>
      <i onclick="clearPlan()" title="取消计划筛选">✕</i></span>`);
  } else {
    parts.push(kw
      ? `<span class="crumb">关键词 <b>${esc(kw)}</b> <i onclick="clearKw()" title="清除">✕</i></span>`
      : `<span class="crumb">关键词 <b>全部（${(ov.keywords || []).length} 个）</b></span>`);
  }
  parts.push(`<span class="crumb">命中 <b>${fmt(ov.total || 0)}</b> 条</span>`);
  const dd = ov.dedupe || {};
  if (dd.dup) {
    parts.push(`<span class="crumb ok" onclick="go('export')" title="${esc(dd.note || "")}"
      >已去重 <b>${fmt(dd.dup)}</b> 条 <i>›</i></span>`);
  }
  const syn = ov.syndication || {};
  if (syn.groups) {
    parts.push(`<span class="crumb" onclick="openScopeDoc()"
      title="标题相同但作者不同＝多个号转载同一篇稿。这类不自动去重（转载本身就是真实扩散），点开看清单"
      >同题转载 <b>${syn.groups}</b> 组 / ${syn.extra_posts} 条 <i>?</i></span>`);
  }
  const im = ov.impact || {};
  if (im.high) {
    parts.push(`<span class="crumb warn" onclick="go('data');$('fsort').value='impact';renderTable()"
      title="${esc(im.note || "")}">重点负面 <b>${im.high}</b> 条</span>`);
  }
  $("curfilter").innerHTML = parts.join("");
}

function clearKw() {
  $("kwfilter").value = "";
  loadData();
}

/* 右上角登录态 */
function toggleLogin() {
  $("loginbox").classList.toggle("on");
}
document.addEventListener("click", e => {
  if (!e.target.closest(".logindrop")) $("loginbox").classList.remove("on");
});
function renderLoginBadge() {
  const ready = PLATS.filter(p => !p.needs_login || p.logged_in).length;
  $("loginbadge").textContent = `登录态 ${ready}/${PLATS.length}`;
  $("loginbox").innerHTML = PLATS.map(p => {
    const tag = !p.needs_login ? '<span class="tag free">免登录</span>'
      : p.logged_in ? '<span class="tag ok">可用</span>' : '<span class="tag need">需扫码</span>';
    return `<div class="dropline">${logo(p.name)}${esc(p.label)}<span class="sp">${tag}</span>
      <button class="btn ghost sm" onclick="login('${p.name}')">${p.logged_in ? "重登" : "登录"}</button></div>`;
  }).join("") + `<div class="dropline" style="margin-top:4px">
      <button class="btn ghost sm" onclick="checkAll()">全部体检</button>
      <button class="btn ghost sm" onclick="go('plat')">打开平台页</button></div>`;
}

/* 迷你走势：把一串数字画成 26px 高的 SVG 折线（KPI 卡里那条小线） */
function spark(values, color) {
  const v = (values || []).filter(x => typeof x === "number");
  if (v.length < 2) return "";
  const max = Math.max(...v, 1), min = Math.min(...v, 0), span = (max - min) || 1;
  const w = 120, h = 26;
  const pts = v.map((x, i) => `${(w * i / (v.length - 1)).toFixed(1)},${(h - 2 - (h - 6) * (x - min) / span).toFixed(1)}`);
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="1.6"/>
    <polyline points="0,${h} ${pts.join(" ")} ${w},${h}" fill="${color}" fill-opacity=".10" stroke="none"/>
  </svg>`;
}

/* 环比标签：统一「红涨绿降」。
   原来按指标性质翻转过颜色（负面指标涨=坏用红、正常指标涨=好用绿），结果同一行里
   两个都在涨的数一个红一个绿，读的人得先想这指标是好是坏。现在只表达方向：
   涨=红、跌=绿、基本没动=灰。第二个参数保留是为了不改所有调用点，已不影响颜色。 */
function deltaTag(d, _legacyBadWhenUp = true) {
  if (d === null || d === undefined) return "";
  const up = d > 0, flat = Math.abs(d) < 0.05;
  const cls = flat ? "flat" : (up ? "up" : "down");
  const arrow = flat ? "—" : (up ? "↑" : "↓");
  return `<span class="delta ${cls}">${arrow} ${Math.abs(d).toFixed(1)}%</span>`;
}

/* KPI 卡。左边三张是"当期量级 + 环比 + 走势"，右边一张宽卡是重点关注轮播。
   原来还有一张「声量最大的平台」结论卡，但同样的信息在下面「平台分布」那张图表里
   看得更清楚（还带占比和负面声量），KPI 位不需要再占两列，去掉后这一行不那么挤。 */
function renderKpis(ov, rows) {
  const sv = ov.sov || {}, pv = ov.prev || {};
  const trend = ov.trend || [];
  const labels = ov.labels || [];
  const daily = trend.map(r => labels.reduce((n, l) => n + (r[l] || 0), 0));
  const negDaily = (sv.trend || []).map(r => r.neg_sov);

  const items = [
    { ico: "文", c: "#3d5fd9", v: fmt(ov.total), t: "当前口径帖子数",
      d: deltaTag(pv.total_delta), s: spark(daily, "#3d5fd9"),
      sub: pv.total ? `上期 ${fmt(pv.total)} 条（${pv.window || ""}）` : "上期区间无数据" },
    { ico: "声", c: "#cf4a44", v: fmt(sv.neg_sov || 0), t: "负面声量分",
      d: deltaTag(pv.neg_sov_delta), s: spark(negDaily, "#cf4a44"),
      sub: `占总声量 ${sv.neg_share || 0}%` },
    { ico: "赞", c: "#e08a1e", v: fmt(ov.engagement), t: "互动量合计",
      d: deltaTag(pv.engagement_delta), s: spark(daily, "#e08a1e"),
      sub: "赞+评×2+转×3+藏×2" },
  ];

  const cards = items.map(it => `<div class="kpi">
    <div class="top"><div class="ico" style="background:linear-gradient(140deg,${it.c},${it.c}bb)">${it.ico}</div>
      <div style="min-width:0"><div class="n">${it.v}</div><div class="t">${esc(it.t)}</div></div></div>
    <div class="d">${it.d} ${esc(it.sub)}</div>
    <div class="spark">${it.s}</div></div>`);

  cards.push(`<div class="kpi wide kpicar">
      <div class="chd">
        <div class="ico" style="background:linear-gradient(140deg,#cf4a44,#e98a85)">热</div>
        <div class="t">重点关注<span class="muted">互动量最高的内容 · 自动轮播</span></div>
        <div class="cnav">
          <button class="btn ghost sm" onclick="slide(-1)">‹</button>
          <button class="btn ghost sm" onclick="slide(1)">›</button>
        </div>
      </div>
      <div class="carousel"><div class="ctrack" id="ctrack"></div></div>
      <div class="cdots" id="cdots"></div>
    </div>`);

  $("kpis").innerHTML = cards.join("");
}

/* KPI 上点平台/帖子的快捷动作 */
function focusPlat(label) {
  $("fplat").value = label;
  go("data");
  renderTable();
  toast(`帖子明细已筛到「${label}」`);
}
function openPost(url) {
  if (url) window.open(url, "_blank");
}

function renderPlatDonut(ov) {
  const plats = ov.by_platform || [];
  const total = plats.reduce((n, p) => n + p["帖子数"], 0) || 1;
  const sovBy = {};
  ((ov.sov || {}).by_platform || []).forEach(p => { sovBy[p.name] = p; });
  chart("c-platdonut").setOption({
    tooltip: { trigger: "item", formatter: p => `${p.name}：${fmt(p.value)} 条（${p.percent}%）` },
    graphic: [{ type: "text", left: "center", top: "46%",
      style: { text: `${fmt(total)}\n总计`, textAlign: "center", fill: "#1c212b",
               fontSize: 15, fontWeight: 600, lineHeight: 18 } }],
    series: [{ type: "pie", radius: ["58%", "82%"], center: ["50%", "50%"],
      itemStyle: { borderColor: "#fff", borderWidth: 2 }, label: { show: false },
      data: plats.map(p => ({ name: p["平台"], value: p["帖子数"],
                              itemStyle: { color: brandColor(p["平台"]) } })) }]
  }, true);
  const map = BY_LABEL();
  // 行可点：原来「声量最大的平台」那张 KPI 卡承担的"点平台去看明细"，挪到这张表上
  document.querySelector("#plattable tbody").innerHTML = plats.map(p => `<tr class="clk"
    onclick="focusPlat(${q(p["平台"])})" title="点一下在帖子明细里只看这个平台">
    <td><span class="pcell">${logo(map[p["平台"]])}${esc(p["平台"])}</span></td>
    <td class="num">${fmt(p["帖子数"])}</td>
    <td class="num">${(p["帖子数"] / total * 100).toFixed(1)}%</td>
    <td class="num">${fmt((sovBy[p["平台"]] || {}).neg_sov || 0)}</td></tr>`).join("");
}

function renderRisks(ov) {
  const rs = ov.risks || [];
  const cls = { "高风险": "high", "中风险": "mid", "低风险": "low" };
  $("risklist").innerHTML = rs.length ? rs.map(r => `<div class="risk ${cls[r.level] || "low"}">
    <span class="lv">${esc(r.level)}</span>
    <div class="txt"><b>${esc(r.title)}</b><span>${esc(r.detail)}</span></div>
    <button class="btn ghost sm act" onclick="go('data')">${esc(r.action)}</button>
  </div>`).join("") : '<div class="empty">当前口径没有触发预警规则</div>';
}

/* 顶部进度条：把"我在加载"这件事显式说出来，避免用户以为点击没生效 */
function progress(on) {
  const el = $("nprog");
  if (!el) return;
  if (on) {
    el.classList.remove("done");
    el.style.width = "0";
    // 强制回流一次，否则从 100%→0% 的过渡会被浏览器合并掉、看不到动画
    void el.offsetWidth;
    el.classList.add("on");
  } else {
    el.classList.remove("on");
    el.classList.add("done");
  }
}

async function loadData() {
  const box = $("page-dash");
  box.classList.add("loading");
  progress(true);
  try {
    await loadDataInner();
  } finally {
    box.classList.remove("loading");
    progress(false);
  }
}

async function loadDataInner() {
  // overview 和 posts 并行发：两个请求互不依赖，串行等于白等一个 RTT + 一次服务端计算
  const qs = params(), qsPosts = params({ limit: 5000 });
  const [ov, rows] = await Promise.all([
    api("/api/overview?" + qs),
    api("/api/posts?" + qsPosts),
  ]);
  LAST_OV = ov;
  ROWS = rows;
  const cur = $("kwfilter").value;
  const kws = ov.all_keywords || ov.keywords || [];
  $("kwfilter").innerHTML = '<option value="">全部关键词</option>' +
    kws.map(k => `<option value="${esc(k)}" ${k === cur ? "selected" : ""}>${esc(k)}</option>`).join("");
  renderCrumb(ov);
  renderPlatDonut(ov);
  renderRisks(ov);
  CLEAN_RULES = ov.clean_rules || CLEAN_RULES;
  PAGE = 1;
  // 筛选项/计数一律基于 ROWS 现算，保证"下拉里写的数"和"表里能筛出来的数"永远一致
  fillFilters();
  // KPI 里的轮播要看单帖明细，所以放在 ROWS 到手之后再渲染；
  // #ctrack 是 renderKpis 新建的节点，轮播必须在它之后填
  renderKpis(ov, ROWS);
  renderTable();
  renderCarousel(filtered());
  renderEvents(ov);
  renderAlerts(ov);
  renderHotNegative(ov);
  charts(ov);
  loadCloud();
  loadCompare();
}

/* 重复内容：内容级判重是"只标记不删除"，所以必须有地方能看见判了哪些、按什么判的 */
async function loadDups() {
  const d = await api("/api/dups");
  $("dupnote").textContent = `判重口径：${d.note}｜全库 ${fmt(d.stat.raw)} 条，`
    + `其中 ${fmt(d.stat.dup)} 条被判为重复、不计入任何口径与导出`;
  const body = document.querySelector("#dups tbody");
  if (!d.rows.length) {
    body.innerHTML = '<tr><td colspan="6"><div class="empty">当前口径下没有判出重复内容</div></td></tr>';
    return;
  }
  const map = BY_LABEL();
  body.innerHTML = d.rows.map(r => {
    const s = r.source || {};
    return `<tr>
      <td title="${esc(r.title)}">${esc((r.title || "").slice(0, 40))}
        <span class="t2">${esc(r.author || "匿名")}｜${esc((r.publish_time || "").slice(0, 10))}</span></td>
      <td><span class="pcell">${logo(map[r.platform_label])}${esc(r.platform_label)}</span></td>
      <td>${esc(r.keyword)}</td>
      <td title="${esc(s.title || "")}">${esc((s.title || "（原始条目已不在库）").slice(0, 40))}
        ${s.keyword ? `<span class="t2">搜索词 ${esc(s.keyword)}</span>` : ""}</td>
      <td>${s.platform_label ? `<span class="pcell">${logo(map[s.platform_label])}${esc(s.platform_label)}</span>` : "—"}</td>
      <td class="num">${fmt(r.engagement)} → <b>${fmt(s.engagement || 0)}</b></td></tr>`;
  }).join("");
}

async function redoDedupe() {
  toast("正在按当前口径重算全库指纹并判重…");
  const r = await api("/api/dedupe", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: "{}" });
  toast(`重算 ${fmt(r.rekeyed)} 条指纹，新判重 ${r.marked} 条、放回 ${r.freed} 条，`
    + `当前累计 ${r.total_dup} 条重复`, "ok");
  await loadDups();
  await loadData();
}

/* 回收站：软删除的内容要能看见、能恢复，否则"删除"就是一条不可逆的暗操作 */
async function loadDeleted() {
  const rows = await api("/api/deleted");
  const body = document.querySelector("#trash tbody");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="7"><div class="empty">没有被删除的内容</div></td></tr>';
    return;
  }
  const map = BY_LABEL();
  body.innerHTML = rows.map(r => `<tr>
    <td><span class="pcell">${logo(map[r.platform_label])}${esc(r.platform_label)}</span></td>
    <td>${esc((r.publish_time || "").slice(0, 10))}</td>
    <td>${esc((r.title || "").slice(0, 50))}</td>
    <td>${esc(r.author || "匿名")}</td>
    <td class="num">${fmt(r.engagement)}</td>
    <td>${esc(r.keyword)}</td>
    <td><button class="btn ghost sm"
      onclick="restorePost(${q(r.platform)},${q(r.post_id)})">恢复</button></td></tr>`).join("");
}

async function restorePost(platform, postId) {
  const r = await api("/api/post/delete", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ platform, post_id: postId, deleted: false })
  });
  if (!r.ok) { toast("恢复失败", "err"); return; }
  toast("已恢复，看板与声量已重算", "ok");
  await loadDeleted();
  await loadData();
}

async function loadFiles() {
  const fs = await api("/api/exports");
  const body = document.querySelector("#files tbody");
  if (!fs.length) {
    body.innerHTML = '<tr><td colspan="4"><div class="empty">还没有导出文件</div></td></tr>';
    return;
  }
  body.innerHTML = fs.map(f => `<tr>
    <td>${esc(f.name)}</td><td class="num">${f.size_kb} KB</td><td>${esc(f.mtime)}</td>
    <td><a class="btn ghost sm" href="/api/download?file=${encodeURIComponent(f.name)}">下载</a></td></tr>`).join("");
}

async function loadLayerStat() {
  const ov = await api("/api/overview?" + params({ scope: "all" }));
  const desc = {
    "A_UGC负反馈": "普通用户/大V 吐槽投诉，主口径",
    "B_媒体负面报道": "媒体/机构/平台号负面内容，主口径",
    "C_中性事件报道": "只转述事实，不计负反馈",
    "D_噪音": "教程/软文/赚钱等，已剔除",
    "E_正面反馈": "整治见效、体验变好",
    "未分层": "规则未加载或采集时未分层",
  };
  $("layerstat").innerHTML = Object.entries(ov.funnel).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<div class="kpi" style="flex:1;min-width:190px">
      <div class="n">${fmt(v)}</div><div class="t">${esc(k)}</div>
      <div class="d">${esc(desc[k] || "")}</div></div>`).join("");
}

async function reclean() {
  toast("正在按最新规则重新分层…");
  const r = await api("/api/reclean", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force: true })
  });
  toast(`重新分层完成，处理 ${r.updated} 条`, "ok");
  loadLayerStat();
  loadData();
}

async function exportData(withJsonl) {
  const r = await api("/api/export?" + params({ jsonl: withJsonl ? "1" : "0" }));
  if (!r.ok) { toast(r.msg, "err"); return; }
  toast(`已导出 ${r.rows} 条到 data/exports`, "ok");
  loadFiles();
}

/* ---------------- 分析模型（大模型接入） ---------------- */
let MODELS = { models: [], default: "", presets: [] };
let CUR_REPORT = "";

async function loadModels() {
  MODELS = await api("/api/llm/models");
  $("presets").innerHTML = (MODELS.presets || []).map((p, i) =>
    `<button onclick="fillPreset(${i})" title="${esc(p.note)}">${esc(p.name)}</button>`).join("");
  const list = MODELS.models || [];
  $("mdls").innerHTML = list.length ? list.map(m => `
    <div class="mdl ${m.id === MODELS.default ? "on" : ""}">
      <b>${esc(m.name)} ${m.id === MODELS.default ? '<span class="tag brand">默认</span>' : ""}</b>
      <div class="kv">${esc(m.base_url)}</div>
      <div class="kv">模型 ${esc(m.model)}｜key ${esc(m.api_key)}｜更新 ${esc(m.updated || "-")}</div>
      <div class="act">
        <button class="btn ghost sm" onclick="testModel('${m.id}')">连通测试</button>
        <button class="btn ghost sm" onclick="listModelsOf('${m.id}')">拉取模型名</button>
        <button class="btn ghost sm" onclick="editModel('${m.id}')">编辑</button>
        ${m.id === MODELS.default ? "" :
      `<button class="btn ghost sm" onclick="defaultModel('${m.id}')">设为默认</button>`}
        <button class="btn ghost sm" onclick="delModel('${m.id}')">删除</button>
      </div>
    </div>`).join("")
    : '<div class="empty">还没有接入模型。点上面的预设填好 base_url 与模型名，再粘贴自己的 api_key 保存。</div>';
  $("r-model").innerHTML = list.map(m =>
    `<option value="${m.id}" ${m.id === MODELS.default ? "selected" : ""}>${esc(m.name)}</option>`).join("")
    || '<option value="">（未接入模型）</option>';
  $("nav-report").textContent = (await api("/api/reports")).length;
}

function fillPreset(i) {
  const p = MODELS.presets[i];
  $("m-name").value = p.name;
  $("m-url").value = p.base_url;
  $("m-model").value = p.model;
  $("m-id").value = "";
  toast(`${p.name}：${p.note}`);
}

function clearModelForm() {
  ["m-id", "m-name", "m-url", "m-model", "m-key"].forEach(id => { $(id).value = ""; });
  $("m-insecure").checked = false;
}

function editModel(id) {
  const m = (MODELS.models || []).find(x => x.id === id);
  if (!m) return;
  $("m-id").value = m.id; $("m-name").value = m.name;
  $("m-url").value = m.base_url; $("m-model").value = m.model; $("m-key").value = "";
  $("m-insecure").checked = !!m.insecure;
  toast("已载入配置，api_key 留空表示不修改");
}

async function saveModel() {
  const body = {
    id: $("m-id").value, name: $("m-name").value, base_url: $("m-url").value,
    model: $("m-model").value, api_key: $("m-key").value,
    insecure: $("m-insecure").checked,
  };
  if (!body.base_url || !body.model) { toast("base_url 和模型名必填", "err"); return; }
  await api("/api/llm/save", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  toast("已保存，配置写入 data/llm.json", "ok");
  clearModelForm();
  loadModels();
}

async function delModel(id) {
  await api("/api/llm/delete", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id })
  });
  toast("已删除");
  loadModels();
}

async function defaultModel(id) {
  await api("/api/llm/default", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id })
  });
  loadModels();
}

async function testModel(id) {
  toast("正在测试连通性…");
  const r = await api("/api/llm/test", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id })
  });
  toast(r.msg, r.ok ? "ok" : "err");
}

/* 拉网关真实支持的模型名：模型名写错时报的错很误导
   （内网 one-api 把 gpt5.5 报成「无可用渠道」，其实是漏了连字符） */
async function listModelsOf(id) {
  toast("正在拉取模型列表…");
  const r = await api("/api/llm/models_of", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id })
  });
  if (!r.ok) { toast(r.msg, "err"); return; }
  const m = (MODELS.models || []).find(x => x.id === id) || {};
  $("m-id").value = id;
  $("m-name").value = m.name || "";
  $("m-url").value = m.base_url || "";
  $("m-key").value = "";
  $("presets").innerHTML = r.models.map(name =>
    `<button onclick="$('m-model').value='${esc(name)}';toast('已填入 ${esc(name)}，点保存生效')">${esc(name)}</button>`
  ).join("");
  toast(`共 ${r.count} 个模型，点上面的名字填入后保存`, "ok");
}

/* ---------------- 分析报告 ---------------- */
async function makeReport() {
  if (!(MODELS.models || []).length) { toast("先接入一个模型", "err"); return; }
  const r = await api("/api/llm/report", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model_id: $("r-model").value, note: $("r-note").value, out_fmt: $("r-fmt").value,
      since: $("since").value, until: $("until").value, scope: SCOPE,
    })
  });
  toast("已提交生成任务，通常 30~120 秒");
  watch(r.task);
}

async function loadReports() {
  const list = await api("/api/reports");
  $("nav-report").textContent = list.length;
  const body = document.querySelector("#reports tbody");
  if (!list.length) {
    body.innerHTML = '<tr><td colspan="4"><div class="empty">还没有报告</div></td></tr>';
    return list;
  }
  body.innerHTML = list.map(f => `<tr data-name="${esc(f.name)}">
    <td><a onclick="openReport('${esc(f.name)}',event)">${esc(f.name)}</a></td>
    <td class="num">${f.size_kb} KB</td><td>${esc(f.mtime)}</td>
    <td class="row" style="gap:4px">
      <button class="btn ghost sm" onclick="openReport('${esc(f.name)}',event)">查看</button>
      <button class="btn ghost sm" onclick="exportReport('${esc(f.name)}','html')">HTML</button>
      <button class="btn ghost sm" onclick="exportReport('${esc(f.name)}','pdf')">PDF</button>
      <button class="btn ghost sm" onclick="exportReport('${esc(f.name)}','txt')">TXT</button>
      <a class="btn ghost sm" href="/api/report/download?name=${encodeURIComponent(f.name)}">MD</a>
    </td>
  </tr>`).join("");
  return list;
}

/* 报告多格式导出：html 与 pdf 生成后直接在新标签里打开（不是给一段代码，是能看的页面） */
async function exportReport(name, fmt) {
  toast(`正在生成 ${fmt.toUpperCase()}…`);
  const r = await api("/api/report/export", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, fmt })
  });
  if (!r.ok) { toast(r.msg || "导出失败", "err"); return; }
  const url = "/api/report/download?name=" + encodeURIComponent(r.file)
    + (fmt === "txt" ? "" : "&inline=1");
  if (fmt === "txt") location.href = url; else window.open(url, "_blank");
  toast(`已生成 ${r.file}`, "ok");
}

/* 点「查看」= 就地在这一行下面展开渲染好的 HTML 报告。
   两个刻意的选择：
   1) 一律走 build_rich_html（带 KPI/图表/事件块），不再用 md 文本渲染——报告要能看图，
      纯 markdown 排出来就是一长条文字；
   2) 展开在被点的那一行下方，而不是塞到表格外的固定面板，看哪份就在哪份下面。 */
async function openReport(name, ev) {
  const tbody = document.querySelector("#reports tbody");
  // 没有事件源（比如生成完自动打开）时按文件名找回那一行，展开位置才不会跑到表尾
  let tr = ev && ev.target ? ev.target.closest("tr") : null;
  if (!tr && tbody) {
    tr = [...tbody.querySelectorAll("tr")].find(x => x.dataset.name === name) || null;
  }
  const old = tbody ? tbody.querySelector("tr.rexp") : null;
  const sameRow = old && tr && old.previousElementSibling === tr;
  if (old) old.remove();
  if (tbody) tbody.querySelectorAll("tr.opened").forEach(x => x.classList.remove("opened"));
  if (sameRow) return;                       // 再点一次＝收起
  CUR_REPORT = name;
  $("r-cur").textContent = name;
  $("r-dl").href = "/api/report/download?name=" + encodeURIComponent(name);
  $("r-acts").style.display = "";
  const cols = document.querySelectorAll("#reports thead th").length || 4;
  const row = document.createElement("tr");
  row.className = "rexp";
  row.innerHTML = `<td colspan="${cols}"><div class="rexpbox">
    <div class="empty" style="padding:26px">正在渲染报告…</div></div></td>`;
  if (tr) tr.after(row); else if (tbody) tbody.append(row);
  if (tr) tr.classList.add("opened");
  const r = await api("/api/report/export", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, fmt: "html" })
  });
  const file = (r && r.file) || name.replace(/\.md$/, ".html");
  const box = row.querySelector(".rexpbox");
  if (!r || !r.ok) {
    box.innerHTML = `<div class="empty" style="padding:26px">${esc((r && r.msg) || "报告渲染失败")}</div>`;
    return;
  }
  box.innerHTML = `<iframe class="rframe"
    src="/api/report/download?name=${encodeURIComponent(file)}&inline=1"></iframe>`;
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ---------------- 重点关注轮播 ---------------- */
let CIDX = 0, CTIMER = null, CLEN = 0;

function renderCarousel(rows) {
  const top = rows.slice(0, 8);
  CLEN = top.length;
  const map = BY_LABEL();
  $("ctrack").innerHTML = top.map(r => `
    <div class="cslide"><div class="ccard">
      <div class="hd">${logo(map[r.platform_label])}${esc(r.platform_label)}
        <span class="chip" style="--cc:${ACTOR_COLOR[r.actor] || "#8b95a9"}">${esc(r.actor || "未判定")}</span>
        <span class="chip" style="--cc:${SENT_COLOR[r.sentiment] || "#8b95a9"}">${esc(r.sentiment || "-")}</span>
        <span class="muted">${esc((r.publish_time || "").slice(0, 16))}</span>
      </div>
      <a class="tt" href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc(r.title || "（无标题）")}</a>
      <div class="ft">
        <span>作者 ${esc(r.author || "-")}</span>
        <span>互动量 <b>${fmt(r.engagement)}</b></span>
        <span>赞 ${fmt(r.likes)} · 评 ${fmt(r.comments)} · 转 ${fmt(r.shares)}</span>
        <span style="margin-left:auto">搜索词 ${esc(r.keyword || "-")}</span>
      </div>
    </div></div>`).join("");
  $("cdots").innerHTML = top.map((_, i) =>
    `<i class="${i === 0 ? "on" : ""}" onclick="gotoSlide(${i})"></i>`).join("");
  CIDX = 0;
  applySlide();
  clearInterval(CTIMER);
  if (CLEN > 1) CTIMER = setInterval(() => slide(1), 6000);   // 自动轮播，手动操作后仍继续
}

function applySlide() {
  $("ctrack").style.transform = `translateX(-${CIDX * 100}%)`;
  document.querySelectorAll("#cdots i").forEach((d, i) => d.classList.toggle("on", i === CIDX));
}
function slide(step) {
  if (!CLEN) return;
  CIDX = (CIDX + step + CLEN) % CLEN;
  applySlide();
}
function gotoSlide(i) { CIDX = i; applySlide(); }

/* ---------------- 参数配置（自行调口径） ---------------- */
let SETTINGS = { current: {}, defaults: {} };
const SET_LABEL = {
  followers: "粉丝数 ≥", likes: "点赞 ≥", shares: "转发 ≥", engagement: "互动量 ≥",
  limit: "每平台每词上限", strict_match: "默认开启相关性闸门", span_days: "默认时间跨度（天）",
  max_tokens: "报告 max_tokens", temperature: "temperature", samples: "喂给模型的样本条数",
  top: "词云词条数", min_count: "词最少出现次数",
  body_chars: "参与指纹的正文字数", time_to_day: "发布时间只比到「天」",
  with_author: "作者参与指纹", douyin_detail: "抖音补详情页指标",
};

async function loadSettings() {
  SETTINGS = await api("/api/settings");
  for (const [section, vals] of Object.entries(SETTINGS.current)) {
    const box = $("set-" + section);
    if (!box) continue;
    box.innerHTML = Object.entries(vals).map(([k, v]) => {
      const label = SET_LABEL[k] || k;
      if (typeof v === "boolean") {
        return `<label class="chk"><input type="checkbox" data-s="${section}" data-k="${k}"
          ${v ? "checked" : ""}> ${esc(label)}</label>`;
      }
      const step = String(v).includes(".") ? "0.1" : "1";
      return `<div class="field"><label>${esc(label)}</label>
        <input type="number" step="${step}" data-s="${section}" data-k="${k}" value="${v}"></div>`;
    }).join("");
  }
}

async function saveSettings() {
  const patch = {};
  document.querySelectorAll("[data-s][data-k]").forEach(el => {
    const s = el.dataset.s, k = el.dataset.k;
    patch[s] = patch[s] || {};
    patch[s][k] = el.type === "checkbox" ? el.checked : +el.value;
  });
  const r = await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patch })
  });
  SETTINGS.current = r.current;
  toast("配置已保存到 data/settings.json，已生效", "ok");
  loadData();
}

async function resetSettings() {
  const r = await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reset: true })
  });
  SETTINGS.current = r.current;
  loadSettings();
  toast("已恢复默认参数");
  loadData();
}

async function loadAll() {
  await loadPlats();
  await loadData();
  await loadTasks();
  loadCloud();
}
$("csince").value = $("since").value;
$("cuntil").value = $("until").value;
loadAll();
