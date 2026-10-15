"""微信公众号适配器。

诚实说明能力边界：
  1) 免登录：搜狗微信搜索 → 标题 / 公众号 / 发布日期 / 链接（sogou 跳转链接，有时效）
  2) 免登录：百度 site:mp.weixin.qq.com（支持时间区间，拿永久链接 + 公众号名）
  3) 阅读数 / 点赞 / 在看：微信不在网页侧下发，必须用 PC/移动端微信 + MITM 代理抓包
     （参考 GitHub：striver-ing/wechat-spider、lqqyt2423/wechat_spider、
      wnma3mz/wechat_articles_spider）。本平台预留 attach_mitm_metrics() 挂载点，
     不内置代理链路，避免给出"能拿到阅读点赞"的错误承诺。
"""
from __future__ import annotations

from datetime import date
from urllib.parse import quote

from ..models import in_window, to_int
from .article import (BAIDU_RESULT_JS, baidu_time_filter, parse_loose_date,
                      week_slices)
from .base import BasePlatform, register

SOGOU_JS = """() => Array.from(document.querySelectorAll('li[id^="sogou_vr_"], .news-box li'))
  .map(el => {
    const a = el.querySelector('h3 a');
    if (!a) return null;
    const acc = el.querySelector('.account, .s-p a');
    const dt = el.querySelector('.s2, .s-p');
    return {
      title: (a.innerText || '').trim(),
      url: a.href || '',
      account: acc ? (acc.innerText || '').trim() : '',
      dateText: dt ? (dt.innerText || '').trim() : '',
      text: (el.innerText || '').replace(/\\s+/g, ' ').trim()
    };
  }).filter(Boolean)"""


@register
class Weixin(BasePlatform):
    name = "weixin"
    label = "微信公众号"
    LOGIN_URL = "https://mp.weixin.qq.com/"
    NEEDS_LOGIN = False
    METRICS = ("（网页侧无互动数）",)
    NOTE = ("标题/公众号/日期/链接可稳定拿到；阅读数、点赞、在看需 PC 端微信 + MITM 代理，"
            "见 attach_mitm_metrics() 注释。登录 mp 后台可额外按公众号拉历史文章。")

    def is_logged_in(self, page) -> bool:
        # 登录 mp 后台是可选增强项：登录后 URL 会带 token
        return "token=" in page.url or self.has_cookie(page, "slave_sid", 10)

    def search(self, ctx, keyword: str, limit: int, since: str, until: str):
        posts = self._baidu_channel(ctx, keyword, limit, since, until)
        if len(posts) < limit:
            posts += self._sogou_channel(ctx, keyword, limit - len(posts), since, until)
        seen, out = set(), []
        for p in posts:
            if p.post_id in seen:
                continue
            seen.add(p.post_id)
            out.append(p)
        return out[:limit]

    def _baidu_channel(self, ctx, keyword: str, limit: int, since: str, until: str):
        """百度 site: 限定：拿永久链接，支持精确时间区间，并按周切片提覆盖率。"""
        page = ctx.new_page()
        posts, seen = [], set()
        try:
            for a, b in week_slices(since, until):
                if len(posts) >= limit:
                    break
                for pn in range(0, 60, 20):
                    url = (f"https://www.baidu.com/s?wd="
                           f"{quote(keyword + ' site:mp.weixin.qq.com')}"
                           f"&rn=20&pn={pn}{baidu_time_filter(a, b)}")
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(2200)
                    rows = page.evaluate(BAIDU_RESULT_JS)
                    if not rows:
                        break
                    fresh = 0
                    for r in rows:
                        if "mp.weixin.qq.com" not in r["url"]:
                            continue
                        pid = self._biz_key(r["url"])
                        if pid in seen:
                            continue
                        seen.add(pid)
                        fresh += 1
                        publish = parse_loose_date(
                            r["text"], ref=date.fromisoformat(b) if b else None)
                        if not in_window(publish, since, until):
                            continue
                        posts.append(self.new_post(
                            post_id=pid, title=r["title"],
                            content=r["text"][:800], url=r["url"], author=r["source"],
                            publish_time=publish, keyword=keyword,
                            extra={"entry": "baidu_site_search", "slice": f"{a}~{b}"},
                        ).finalize())
                        if len(posts) >= limit:
                            return posts
                    if not fresh:
                        break
        finally:
            page.close()
        return posts

    def _sogou_channel(self, ctx, keyword: str, limit: int, since: str, until: str):
        """搜狗微信：无时间过滤、按相关度排序，多翻页会触发验证码，只取前几页。"""
        page = ctx.new_page()
        posts = []
        try:
            for pno in range(1, 4):
                page.goto(f"https://weixin.sogou.com/weixin?type=2&query={quote(keyword)}"
                          f"&page={pno}&ie=utf8", wait_until="domcontentloaded")
                page.wait_for_timeout(2400)
                body = page.locator("body").inner_text(timeout=6000)
                if "请输入验证码" in body:
                    print("  [微信公众号] 搜狗触发验证码，停止该通道")
                    break
                rows = page.evaluate(SOGOU_JS)
                if not rows:
                    break
                for r in rows:
                    publish = parse_loose_date(r["dateText"] or r["text"])
                    if not in_window(publish, since, until):
                        continue
                    posts.append(self.new_post(
                        post_id=self._biz_key(r["url"]) or r["title"][:40],
                        title=r["title"], content=r["text"][:800], url=r["url"],
                        author=r["account"], publish_time=publish, keyword=keyword,
                        extra={"entry": "sogou_weixin", "url_is_temporary": True},
                    ).finalize())
                    if len(posts) >= limit:
                        return posts
        finally:
            page.close()
        return posts

    @staticmethod
    def _biz_key(url: str) -> str:
        """用 __biz+mid+idx 作为唯一键（公众号文章的身份全在 query 里）。"""
        import re
        parts = []
        for k in ("__biz", "mid", "idx", "sn"):
            m = re.search(rf"{k}=([^&]+)", url or "")
            if m:
                parts.append(m.group(1))
        return "_".join(parts) if parts else (url or "").split("?")[0]

    # ------------------------------------------------------------------
    def attach_mitm_metrics(self, posts, source="mitm"):
        """挂载点：把 MITM 抓到的 {url: {read_num, like_num, comment_num}} 合并进来。

        实现路径（三选一，都需要额外环境，不在本平台内置）：
          A. mitmproxy/AnyProxy 代理 + PC 微信打开文章，截 getappmsgext 返回
          B. 已有公众号后台 cookie+token，按 __biz 拉 appmsg 列表（只有标题链接）
          C. 第三方数据服务（新榜/清博）API
        """
        raise NotImplementedError(
            "阅读/点赞需接入 MITM 代理链路，参考 README『微信公众号互动数』一节")
