"""百家号 / 头条号 / 微信公众号 适配器（图文类，均免登录起步）。

三个平台的共同特点：没有稳定的公开搜索接口，靠搜索引擎入口 + 详情页补指标。
互动数能拿到多少写在各自 METRICS / NOTE 里，不夸大。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

from .. import config
from ..models import in_window, to_int, ts_to_date
from .base import BasePlatform, register

BAIDU_RESULT_JS = """() => Array.from(document.querySelectorAll('div.result, div.c-container'))
  .map(el => {
    const a = el.querySelector('h3 a, a.c-title-text');
    if (!a) return null;
    const mu = el.getAttribute('mu') || '';
    const labels = Array.from(el.querySelectorAll('[class*="color-gray"],[class*="source"]'))
      .map(x => (x.innerText || '').trim()).filter(Boolean);
    return {
      title: (a.innerText || '').trim(),
      url: mu || a.href || '',
      text: (el.innerText || '').replace(/\\s+/g, ' ').trim(),
      source: labels[0] || ''
    };
  }).filter(Boolean)"""


def parse_loose_date(text: str, ref: date | None = None) -> str:
    ref = ref or date.today()
    m = re.search(r"(20\d\d)[年\-/.](\d{1,2})[月\-/.](\d{1,2})", text or "")
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text or "")
    if m:
        try:
            return date(ref.year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return ""
    m = re.search(r"(\d+)\s*天前", text or "")
    if m:
        return (ref - timedelta(days=int(m.group(1)))).isoformat()
    if re.search(r"\d+\s*(小时|分钟)前|刚刚", text or ""):
        return ref.isoformat()
    if "昨天" in (text or ""):
        return (ref - timedelta(days=1)).isoformat()
    return ""


def baidu_time_filter(since: str, until: str) -> str:
    if not (since or until):
        return ""
    def ts(day: str, end: bool) -> int:
        d = datetime.strptime(day, "%Y-%m-%d")
        if end:
            d = d.replace(hour=23, minute=59, second=59)
        return int(d.timestamp())
    a = ts(since or "2020-01-01", False)
    b = ts(until or date.today().isoformat(), True)
    return "&gpc=" + quote(f"stf={a},{b}|stftype=1")


def week_slices(since: str, until: str, span: int = 7, cap: int = 26) -> list[tuple[str, str]]:
    """把时间区间按 span 天切片，从最近的一段往回排。

    为什么要切：百度对一个 query 只放出前 5~7 页，一个 3 个月的大区间查完只剩头部结果。
    按周切等于把同一个 query 拆成多次小窗口查询，覆盖率成倍上升，也顺带保证"新的先拿到"。
    区间给不全就退回单切片（此时 gpc 参数也无从构造）。
    """
    if not (since and until):
        return [(since, until)]
    try:
        a = date.fromisoformat(since)
        b = date.fromisoformat(until)
    except ValueError:
        return [(since, until)]
    if b < a:
        a, b = b, a
    out, end = [], b
    while end >= a and len(out) < cap:
        start = max(a, end - timedelta(days=span - 1))
        out.append((start.isoformat(), end.isoformat()))
        end = start - timedelta(days=1)
    return out


def baijiahao_id(url: str) -> str:
    """百家号文章 URL 形如 /s?id=1869976698746009926&wfr=...，唯一标识在 id 参数里。

    不能按路径末段取（那样每篇都会得到 "s"，入库时互相覆盖）。
    """
    m = re.search(r"[?&]id=(\d+)", url or "")
    if m:
        return m.group(1)
    return (url or "").rstrip("/").split("/")[-1].split("?")[0]


@register
class Baijiahao(BasePlatform):
    name = "baijiahao"
    label = "百家号"
    LOGIN_URL = "https://baijiahao.baidu.com/"
    NEEDS_LOGIN = False
    METRICS = ("（网页侧无互动数）",)
    NOTE = "入口走百度搜索 site: 限定（支持时间区间）；搜索面板与文章页都不给点赞/评论/阅读数，需登录作者后台才有。"

    def is_logged_in(self, page) -> bool:
        return self.has_cookie(page, "BDUSS", 20)

    def search(self, ctx, keyword: str, limit: int, since: str, until: str):
        """百度 site: 限定 + 按周切片翻页（切片理由见 week_slices）。"""
        page = ctx.new_page()
        posts, seen = [], set()
        try:
            for a, b in week_slices(since, until):
                if len(posts) >= limit:
                    break
                for pn in range(0, 60, 20):
                    url = (f"https://www.baidu.com/s?wd={quote(keyword + ' site:baijiahao.baidu.com')}"
                           f"&rn=20&pn={pn}{baidu_time_filter(a, b)}")
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(2200)
                    rows = page.evaluate(BAIDU_RESULT_JS)
                    if not rows:
                        break
                    fresh = 0
                    for r in rows:
                        if "baijiahao" not in r["url"]:
                            continue
                        pid = baijiahao_id(r["url"])
                        if pid in seen:
                            continue
                        seen.add(pid)
                        fresh += 1
                        publish = parse_loose_date(r["text"], ref=date.fromisoformat(b)
                                                   if b else None)
                        if not in_window(publish, since, until):
                            continue
                        posts.append(self.new_post(
                            post_id=pid,
                            title=r["title"], content=r["text"][:800], url=r["url"],
                            author=r["source"], publish_time=publish, keyword=keyword,
                            extra={"entry": "baidu_site_search", "slice": f"{a}~{b}"},
                        ).finalize())
                        if len(posts) >= limit:
                            return posts
                    if not fresh:
                        break
        finally:
            page.close()
        return posts


TOUTIAO_CARD_JS = """() => Array.from(document.querySelectorAll('div.result-content')).map(el => {
  let p = {};
  try { p = JSON.parse(el.getAttribute('cr-params') || '{}'); } catch (e) {}
  return {
    gid: String(p.gid || ''),
    title: (p.title || '').trim(),
    text: (el.innerText || '').replace(/\\s+/g, ' ').trim()
  };
}).filter(r => r.gid && r.title)"""

# 卡片正文尾部固定是 "…摘要 作者 8月16日 / 7天前 / 2021年11月"
_TT_TAIL = re.compile(r"([^ ]{2,20})\s+((?:20\d\d年)?\d{1,2}[月/-]\d{0,2}[日号]?|\d+\s*(?:天|小时|分钟)前|昨天|刚刚)\s*$")


@register
class Toutiao(BasePlatform):
    name = "toutiao"
    label = "头条号"
    LOGIN_URL = "https://www.toutiao.com/"
    NEEDS_LOGIN = False
    METRICS = ("点赞", "评论", "转发", "阅读")
    NOTE = ("so.toutiao.com 资讯 tab 是服务端渲染，解析 div.result-content 的 cr-params 取 gid/标题；"
            "互动数走 m.toutiao.com/i<gid>/info/ 免登录接口补齐，但那是网页镜像侧口径，"
            "普遍只有个位数，不等于 App 内热度。")

    def is_logged_in(self, page) -> bool:
        return self.has_cookie(page, "sessionid", 10) or self.has_cookie(page, "sid_tt", 10)

    def search(self, ctx, keyword: str, limit: int, since: str, until: str):
        page = ctx.new_page()
        posts, seen = [], set()
        try:
            for pno in range(0, 12):
                url = (f"https://so.toutiao.com/search?keyword={quote(keyword)}"
                       f"&pd=information&offset={pno * 10}&page_num={pno}")
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(2600)
                rows = page.evaluate(TOUTIAO_CARD_JS)
                if not rows:
                    break
                fresh = 0
                for r in rows:
                    if r["gid"] in seen:
                        continue
                    seen.add(r["gid"])
                    fresh += 1
                    author, publish = "", ""
                    m = _TT_TAIL.search(r["text"])
                    if m:
                        author, publish = m.group(1), parse_loose_date(m.group(2))
                    if not in_window(publish, since, until):
                        continue
                    posts.append(self.new_post(
                        post_id=r["gid"], title=r["title"], content=r["text"][:800],
                        url=f"https://www.toutiao.com/article/{r['gid']}/",
                        author=author, publish_time=publish, keyword=keyword,
                        extra={"entry": "so.toutiao.com/pd=information"},
                    ).finalize())
                    if len(posts) >= limit:
                        break
                if not fresh or len(posts) >= limit:
                    break
            self._enrich(page, posts)
        finally:
            page.close()
        return posts

    def _enrich(self, page, posts):
        """补互动数：m.toutiao.com 的 info 接口免登录就给 digg/comment/repost/impression。

        顺带把 publish_time 换成接口里的精确时间戳（比卡片上的"7天前"准）。
        这些数字是网页镜像侧口径，实测普遍个位数，不代表 App 内热度——别当成 App 数据用。
        """
        for p in posts:
            try:
                r = page.request.get(f"https://m.toutiao.com/i{p.post_id}/info/",
                                     headers={"User-Agent": config.UA_MOBILE},
                                     timeout=12000)
                data = (r.json() or {}).get("data") or {}
            except Exception:
                continue
            if not data:
                continue
            p.likes = to_int(data.get("digg_count"))
            p.comments = to_int(data.get("comment_count"))
            p.shares = to_int(data.get("repost_count"))
            p.views = to_int(data.get("impression_count"))
            precise = ts_to_date(data.get("publish_time"))
            if precise:
                p.publish_time = precise
            p.extra["metric_src"] = "m.toutiao.com/info"
            p.finalize()
