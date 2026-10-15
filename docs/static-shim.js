/* GitHub Pages 静态展示版垫片。
 *
 * 这个页面没有后端：采集/登录/改判这些要 Python 的操作在这里跑不了（那些仍然用本地
 * 127.0.0.1:8765）。这里只做一件事——把 app.js 发出的 `/api/...` GET 请求改接到
 * data/ 下导出好的 JSON 快照，让看板、图表、明细、词云在纯静态环境下照常展示。
 *
 * 必须在 app.js 之前加载：它靠改写 window.fetch 生效。
 */
(function () {
  window.__STATIC__ = true;

  // 路径（不含 query）→ 快照文件。scope/日期等筛选在静态版下不切数据，统一回默认快照。
  const MAP = {
    "/api/overview": "data/overview.json",
    "/api/posts": "data/posts.json",
    "/api/wordcloud": "data/wordcloud.json",
    "/api/compare": "data/compare.json",
    "/api/plans": "data/plans.json",
    "/api/platforms": "data/platforms.json",
    "/api/dups": "data/dups.json",
    "/api/deleted": "data/deleted.json",
    "/api/reports": "data/reports.json",
    "/api/llm/models": "data/llm_models.json",
  };
  // report 用的是 scope=all 的那份；按 query 里有没有 scope=all 分流
  function pick(path, qs) {
    if (path === "/api/overview" && /(^|&)scope=all(&|$)/.test(qs)) {
      return "data/overview_all.json";
    }
    return MAP[path] || null;
  }

  const WRITE_MSG = "这是 GitHub 静态展示版，采集 / 登录 / 改判等操作请在本地平台（127.0.0.1:8765）执行。";
  const realFetch = window.fetch.bind(window);

  window.fetch = function (url, opt) {
    const u = String(url);
    if (!u.startsWith("/api/")) return realFetch(url, opt);
    const [path, qs = ""] = u.split("?");
    const method = ((opt && opt.method) || "GET").toUpperCase();

    // 写操作：静态版无后端，返回一个"友好失败"，并提示去本地操作
    if (method !== "GET") {
      if (window.toast) window.toast(WRITE_MSG, "err");
      return Promise.resolve(new Response(
        JSON.stringify({ ok: false, error: WRITE_MSG, msg: WRITE_MSG }),
        { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    const file = pick(path, qs);
    if (file) {
      return realFetch(file).catch(() => new Response("{}", { status: 200 }));
    }
    // 没导出的只读接口（任务轮询等）：返回空，避免页面报错
    const EMPTY = /\/tasks?$/.test(path) ? "[]" : "{}";
    return Promise.resolve(new Response(EMPTY,
      { status: 200, headers: { "Content-Type": "application/json" } }));
  };

  // 页面加载后挂一条醒目的横幅，并把"开始采集/全量采集"按钮禁用，避免误点
  window.addEventListener("DOMContentLoaded", function () {
    const bar = document.createElement("div");
    bar.textContent = "📊 静态展示版（数据快照，只读）· 采集与改判请用本地平台 127.0.0.1:8765";
    bar.style.cssText = "position:sticky;top:0;z-index:99;background:#2f6bff;color:#fff;"
      + "font-size:13px;padding:7px 14px;text-align:center;letter-spacing:.3px";
    document.body.prepend(bar);
    setTimeout(function () {
      ["btn-crawl"].forEach(function (id) {
        const b = document.getElementById(id);
        if (b) { b.disabled = true; b.title = WRITE_MSG; }
      });
    }, 1500);
  });
})();
