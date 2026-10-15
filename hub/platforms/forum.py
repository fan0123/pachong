"""社区与投诉平台：B站、知乎、黑猫投诉。

三家的可采性差别很大，先写清楚免得后面误会：
  B站      免登录可用。搜索页自己发 `/x/web-interface/wbi/search/type`，我们拦它的
            JSON（不逆向 wbi 签名）。返回带 pubdate 秒级时间、播放/弹幕/收藏，
            而且 URL 上支持 `pubtime_begin_s/pubtime_end_s`，能把时间窗直接下推到服务端。
  知乎      必须登录。未登录搜索页只回"未搜索到相关内容"，登录后页面自己发
            `/api/v4/search_v3`，我们拦它的 JSON（同抖音思路，不逆向签名）。
  黑猫投诉  **没有可用的公开搜索接口**：`/search/` 是 SPA 且被风控挡住，
            `api/index/search` 返回的不是 JSON。能用的只有公开的最新投诉流
            `api/index/feed?type=1`，所以这里的做法是扫最近 N 页投诉再本地按关键词过滤，
            命中率取决于关键词热度——这一点在 NOTE 里明说，不假装是搜索。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from urllib.parse import quote

from .. import config
from ..intercept import ResponseCollector, find_records
from ..models import clean_text, in_window, to_int, ts_to_date
from .base import BasePlatform, register


@register
class Bilibili(BasePlatform):
    name = "bilibili"
    label = "B站"
    LOGIN_URL = "https://www.bilibili.com/"
    NEEDS_LOGIN = False
    METRICS = ("播放", "弹幕")
    NOTE = ("免登录可用，而且**搜索结果是服务端渲染**的（页面只发 search/default 这种无关 XHR，"
            "所以拦接口拿不到东西，这里直接解析 .bili-video-card）。"
            "URL 上的 &page=N 真的翻页（第 2 页起会带 &o=30 偏移），"
            "&pubtime_begin_s/&pubtime_end_s 能把时间窗下推到服务端，"
            "&order= 支持 totalrank/pubdate/click/dm。"
            "卡片上只有播放数与弹幕数（没有点赞/收藏），所以互动量由弹幕贡献、播放数单独存 views。")

    ORDERS = (("pubdate", "最新发布"), ("totalrank", "综合排序"), ("click", "最多播放"))
    MAX_PAGE = 12
    CARD = ".bili-video-card"
    # 卡片里的两个统计数字依次是「播放数、弹幕数」；日期有三种写法，交给 Python 侧统一解析
    ROW_JS = """() => Array.from(document.querySelectorAll('.bili-video-card')).map(c => {
      const a = c.querySelector('a[href*="/video/"]');
      const t = c.querySelector('.bili-video-card__info--tit');
      const auth = c.querySelector('.bili-video-card__info--author');
      const date = c.querySelector('.bili-video-card__info--date');
      const dur = c.querySelector('.bili-video-card__stats__duration');
      const stats = Array.from(c.querySelectorAll('.bili-video-card__stats--item span'))
        .map(s => (s.innerText || '').trim());
      const up = c.querySelector('a[href*="space.bilibili.com"]');
      if (!a) return null;
      return {
        url: a.getAttribute('href') || '',
        title: (t ? (t.getAttribute('title') || t.innerText || '') : '').trim(),
        author: (auth ? auth.innerText : '').trim(),
        author_url: up ? (up.getAttribute('href') || '') : '',
        date: (date ? date.innerText : '').replace(/^[·\\s]+/, '').trim(),
        duration: (dur ? dur.innerText : '').trim(),
        play: stats[0] || '', danmaku: stats[1] || ''
      };
    }).filter(Boolean)"""

    def is_logged_in(self, page) -> bool:
        return self.has_cookie(page, "SESSDATA", 10)

    def verify(self, ctx, page=None) -> tuple[bool, str]:
        """免登录平台，这里校验的是"搜索页还出不出结果"，不是登录态。"""
        own = page is None
        page = page or ctx.new_page()
        try:
            page.goto(self._url("广告", 1, "totalrank"), wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            got = len(page.evaluate(self.ROW_JS) or [])
            if got:
                return True, f"搜索页出 {got} 个稿件卡片，免登录可采"
            return False, "搜索页没解析出稿件卡片，可能被风控或页面改版"
        except Exception as exc:
            return False, f"校验异常：{exc}"[:120]
        finally:
            if own:
                page.close()

    @staticmethod
    def _day_to_ts(day: str, end: bool = False) -> int:
        """把 YYYY-MM-DD 转成 B站搜索要的 unix 秒。end=True 取当天 23:59:59。"""
        if not day:
            return 0
        try:
            dt = datetime.strptime(day[:10], "%Y-%m-%d")
        except ValueError:
            return 0
        if end:
            dt = dt.replace(hour=23, minute=59, second=59)
        return int(dt.timestamp())

    @staticmethod
    def _parse_date(raw: str) -> str:
        """卡片日期有四种写法：`8小时前` / `昨天` / `09-04` / `2025-04-27`。

        缺年份的一律按"当年"补，补完如果落在未来（跨年场景，比如 12-30 在 1 月看到）
        就退一年，避免把去年的视频算成明年。
        """
        raw = (raw or "").strip()
        today = datetime.now()
        if not raw:
            return ""
        if "分钟前" in raw or "小时前" in raw or raw == "刚刚":
            return today.strftime("%Y-%m-%d")
        if "昨天" in raw:
            return (today - timedelta(days=1)).strftime("%Y-%m-%d")
        if "天前" in raw:
            n = to_int(raw)
            return (today - timedelta(days=n)).strftime("%Y-%m-%d")
        m = re.search(r"(20\d\d)-(\d{1,2})-(\d{1,2})", raw)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = re.search(r"(\d{1,2})-(\d{1,2})", raw)
        if m:
            mm, dd = int(m.group(1)), int(m.group(2))
            year = today.year
            if (mm, dd) > (today.month, today.day):
                year -= 1
            return f"{year}-{mm:02d}-{dd:02d}"
        return ""

    def _url(self, keyword: str, page_no: int, order: str,
             since: str = "", until: str = "") -> str:
        # /video 这个 tab 只出视频稿件，字段比 /all 的聚合页整齐
        url = (f"https://search.bilibili.com/video?keyword={quote(keyword)}"
               f"&order={order}&page={page_no}")
        if page_no > 1:
            url += f"&o={(page_no - 1) * 30}"      # 站内翻页真正生效的偏移参数
        a, b = self._day_to_ts(since), self._day_to_ts(until, end=True)
        if a and b:
            # 时间窗下推到服务端，比抓回来再本地过滤省好几倍请求
            url += f"&pubtime_begin_s={a}&pubtime_end_s={b}"
        return url

    def search(self, ctx, keyword: str, limit: int, since: str, until: str):
        page = ctx.new_page()
        posts, seen = [], set()
        try:
            for order, order_cn in self.ORDERS:
                if len(posts) >= limit:
                    break
                for pno in range(1, self.MAX_PAGE + 1):
                    page.goto(self._url(keyword, pno, order, since, until),
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(2200)
                    # UP 主名字是懒渲染的，翻页后不滚一下会整页拿到空 author
                    page.mouse.wheel(0, 2600)
                    page.wait_for_timeout(900)
                    rows = page.evaluate(self.ROW_JS) or []
                    if not rows:
                        break
                    fresh = 0
                    for r in rows:
                        bv = re.search(r"/video/(BV[0-9A-Za-z]+)", r["url"] or "")
                        if not bv:
                            continue
                        bvid = bv.group(1)
                        if bvid in seen:
                            continue
                        seen.add(bvid)
                        fresh += 1
                        publish = self._parse_date(r.get("date"))
                        if not in_window(publish, since, until):
                            continue
                        mid = re.search(r"space\.bilibili\.com/(\d+)", r.get("author_url") or "")
                        posts.append(self.new_post(
                            post_id=bvid, title=r["title"], content=r["title"],
                            url=f"https://www.bilibili.com/video/{bvid}",
                            author=r.get("author") or "",
                            author_id=mid.group(1) if mid else "",
                            publish_time=publish,
                            comments=to_int(r.get("danmaku")),   # 弹幕当"评论级"互动
                            views=to_int(r.get("play")),
                            keyword=keyword,
                            extra={"entry": f"search/video?order={order}&page={pno}",
                                   "order_cn": order_cn,
                                   "duration": r.get("duration") or "",
                                   "danmaku": to_int(r.get("danmaku")),
                                   "play": to_int(r.get("play")),
                                   "date_raw": r.get("date") or ""},
                        ).finalize())
                        if len(posts) >= limit:
                            break
                    if len(posts) >= limit or not fresh:
                        break
            print(f"  [B站] {len(self.ORDERS)} 趟排序共入 {len(posts)} 条（去重后 {len(seen)} 个稿件）")
        finally:
            page.close()
        return posts


@register
class Zhihu(BasePlatform):
    name = "zhihu"
    label = "知乎"
    LOGIN_URL = "https://www.zhihu.com/signin"
    NEEDS_LOGIN = True
    METRICS = ("赞同", "评论")
    NOTE = ("必须登录：未登录搜索页只回「未搜索到相关内容」。登录后拦 /api/v4/search_v3，"
            "回答/文章/想法都能拿到赞同数与评论数。")

    SEARCH_HINT = "/api/v4/search_v3"

    def is_logged_in(self, page) -> bool:
        return self.has_cookie(page, "z_c0", 10)

    def verify(self, ctx, page=None) -> tuple[bool, str]:
        own = page is None
        page = page or ctx.new_page()
        collector = ResponseCollector(page, [self.SEARCH_HINT])
        try:
            page.goto(f"https://www.zhihu.com/search?type=content&q={quote('广告')}",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            text = page.locator("body").inner_text(timeout=6000)
            if collector.count:
                return True, f"搜索接口已触发（{collector.count} 次），登录态可用"
            if "登录/注册" in text or "未搜索到相关内容" in text:
                return False, "未登录（搜索页无结果 / 提示登录），需扫码"
            return False, "搜索接口没触发，需扫码"
        except Exception as exc:
            return False, f"校验异常：{exc}"[:120]
        finally:
            if own:
                page.close()

    def search(self, ctx, keyword: str, limit: int, since: str, until: str):
        page = ctx.new_page()
        collector = ResponseCollector(page, [self.SEARCH_HINT])
        posts, seen = [], set()
        try:
            page.goto(f"https://www.zhihu.com/search?type=content&q={quote(keyword)}",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            self.scroll_until(page, collector, limit, max_scrolls=20,
                              required_keys=("type", "object"))
            for rec in find_records(collector.datas(), ("object",)):
                obj = rec.get("object") or {}
                oid = str(obj.get("id") or "")
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                otype = obj.get("type") or ""
                title = clean_text(obj.get("title") or (obj.get("question") or {}).get("name") or "")
                body = clean_text(obj.get("excerpt") or obj.get("content") or "")
                publish = ts_to_date(obj.get("created_time") or obj.get("updated_time"))
                if not in_window(publish, since, until):
                    continue
                author = (obj.get("author") or {}).get("name", "")
                if otype == "answer":
                    url = f"https://www.zhihu.com/answer/{oid}"
                elif otype == "article":
                    url = f"https://zhuanlan.zhihu.com/p/{oid}"
                else:
                    url = obj.get("url") or f"https://www.zhihu.com/search?q={quote(keyword)}"
                posts.append(self.new_post(
                    post_id=oid, title=(title or body)[:80], content=body or title,
                    url=url, author=author,
                    author_id=str((obj.get("author") or {}).get("id") or ""),
                    publish_time=publish,
                    likes=to_int(obj.get("voteup_count")),
                    comments=to_int(obj.get("comment_count")),
                    keyword=keyword,
                    extra={"zhihu_type": otype,
                           "followers": (obj.get("author") or {}).get("follower_count")},
                ).finalize())
                if len(posts) >= limit:
                    break
        finally:
            page.close()
        return posts


@register
class Heimao(BasePlatform):
    name = "heimao"
    label = "黑猫投诉"
    # 黑猫用微博账号（SSO）登录；直接打开带 t=1 的搜索页，扫码后会回跳到搜索结果
    LOGIN_URL = ("https://tousu.sina.com.cn/index/search/"
                 "?keywords=%E5%BC%80%E5%B1%8F%E5%B9%BF%E5%91%8A&t=1")
    NEEDS_LOGIN = True
    LOGIN_OPTIONAL = True          # 没登录也能跑，只是退化成扫最新投诉流
    METRICS = ("（投诉平台无互动数）",)
    NOTE = ("搜索页 /index/search/?keywords=<词>&t=1&page=N 就是站内搜索（t=1 投诉、t=2 商家），"
            "能拿到日期、标题、正文、[投诉对象]、[投诉要求]、处理状态，按 page 翻页。"
            "**但它要登录态**：无痕环境实测（含 &t=1）一律跳 passport.weibo.com；"
            "黑猫用微博账号登录，扫一次码即可。未登录时退化成扫公开投诉流 api/index/feed 本地过滤。"
            "投诉平台没有点赞/转发，互动量恒为 0，声量靠条数与主体权重体现。")

    FEED = ("https://tousu.sina.com.cn/api/index/feed"
            "?type=1&page_size=20&page={page}")
    MAX_PAGE = 25
    SEARCH_URL = ("https://tousu.sina.com.cn/index/search/"
                  "?keywords={kw}&t=1&page={page}")   # t=1 是「投诉」tab，t=2 是「商家」
    MAX_SEARCH_PAGE = 15
    # 投诉条目：标题里带关键词的 a[href*='/complaint/view/']，同一行还有公司名与时间
    ROW_JS = """() => Array.from(document.querySelectorAll("a[href*='/complaint/view/']"))
      .map(a => {
        // 结果卡片结构（登录后）：日期「2026-09-02 于黑猫投诉平台发起」+ 标题 + 正文
        // + [投诉对象] xxx + [投诉要求] xxx + 右上角状态（已回复/已完成/处理中）
        const box = a.closest('div.ts-item, li, div') || a.parentElement;
        const t = ((box ? box.innerText : a.innerText) || '').replace(/\\s+/g, ' ').trim();
        const pick = (re) => { const m = t.match(re); return m ? m[1].trim() : ''; };
        return {
          url: a.getAttribute('href') || '',
          title: (a.innerText || '').trim(),
          block: t.slice(0, 400),
          date: pick(/(20\\d\\d-\\d{2}-\\d{2})/),
          target: pick(/\\[投诉对象\\]\\s*([^\\[]{1,40})/),
          demand: pick(/\\[投诉要求\\]\\s*([^\\[]{1,60})/),
          status: pick(/(已回复|已完成|处理中|已撤诉|驳回)/)
        };
      }).filter(x => x.url && x.title)"""

    def is_logged_in(self, page) -> bool:
        # 黑猫用微博账号登录，登录后会带上 SUB/SUBP
        return self.has_cookie(page, "SUB", 10) or self.has_cookie(page, "SCF", 10)

    def verify(self, ctx, page=None) -> tuple[bool, str]:
        own = page is None
        page = page or ctx.new_page()
        try:
            page.goto(self.SEARCH_URL.format(kw=quote("广告"), page=1),
                      wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            if "passport.weibo.com" in page.url or "扫描二维码登录" in page.locator("body").inner_text(timeout=5000):
                return False, "搜索页跳微博 SSO 登录，需扫码（不登录则只能扫最新投诉流）"
            rows = page.evaluate(self.ROW_JS)
            if rows:
                return True, f"搜索页出 {len(rows)} 条投诉，登录态可用"
            return False, "搜索页没解析出投诉条目，可能被风控或页面改版"
        except Exception as exc:
            return False, f"校验异常：{exc}"[:120]
        finally:
            if own:
                page.close()

    def _search_pages(self, ctx, keyword: str, limit: int, since: str, until: str):
        """登录态下按页翻搜索结果页。翻页参数是 &page=N，一页约 10 条。"""
        page = ctx.new_page()
        posts, seen = [], set()
        try:
            for pno in [1]:
                page.goto(self.SEARCH_URL.format(kw=quote(keyword), page=pno),
                          wait_until="commit", timeout=45000)
                page.wait_for_timeout(3200)
                if "passport.weibo.com" in page.url:
                    print("  [黑猫投诉] 搜索页要求登录，退回投诉流扫描")
                    return None
                # 黑猫搜索结果是 JS 滚动加载：URL 里的 &page=N 是摆设（实测第 2 页返回同一批），
                # 页面上也没有分页链接，只有滚动到底触发签名接口继续加载。
                # 所以这里改成"滚动到不再增长"，条数够了或连续两次没长就停。
                sel = "a[href*='/complaint/view/']"
                last, stall = 0, 0
                for _ in range(30):
                    got = page.locator(sel).count()
                    if got >= limit * 2 or stall >= 3:
                        break
                    stall = stall + 1 if got == last else 0
                    last = got
                    page.mouse.wheel(0, 3200)
                    page.wait_for_timeout(1600)
                print(f"  [黑猫投诉] 滚动加载后页面上有 {page.locator(sel).count()} 条投诉链接")
                if pno == 1:
                    body = page.locator("body").inner_text(timeout=6000)
                    hit = re.search(r"共有(\d+)条投诉中包含搜索词", body)
                    if hit:
                        print(f"  [黑猫投诉] 站内命中 {hit.group(1)} 条投诉，开始按页抓取")
                rows = page.evaluate(self.ROW_JS)
                if not rows:
                    break
                fresh = 0
                for r in rows:
                    cid = re.search(r"/complaint/view/(\d+)", r["url"] or "")
                    pid = cid.group(1) if cid else r["url"][-20:]
                    if pid in seen:
                        continue
                    seen.add(pid)
                    fresh += 1
                    publish = r.get("date") or ""
                    if not in_window(publish, since, until):
                        continue
                    posts.append(self.new_post(
                        post_id=pid, title=r["title"][:80], content=r["block"],
                        url=("https://tousu.sina.com.cn" + r["url"]
                             if r["url"].startswith("/") else r["url"]),
                        author=r.get("target") or "",          # 投诉对象＝被投诉的公司
                        publish_time=publish, keyword=keyword,
                        extra={"entry": f"index/search?t=1&page={pno}",
                               "complaint_target": r.get("target") or "",
                               "demand": r.get("demand") or "",
                               "status": r.get("status") or ""},
                    ).finalize())
                    if len(posts) >= limit:
                        return posts
                if not fresh:
                    break
            print(f"  [黑猫投诉] 搜索页翻页拿到 {len(posts)} 条")
            return posts
        finally:
            page.close()

    def company_stat(self, ctx, keyword: str) -> str:
        """`api/company/search` 是黑猫**唯一可用**的按名字查询接口：返回公司近 30 天投诉量。

        拿不到"某公司的投诉明细"（试过 type=4&couid，服务端忽略 couid、照样返回全站流），
        所以这里只把公司级投诉量打进日志，作为该品牌的投诉热度参考，不伪造成帖子入库。
        """
        try:
            resp = ctx.request.get(
                "https://tousu.sina.com.cn/api/company/search?keywords=" + quote(keyword),
                headers={"User-Agent": config.UA_PC, "Referer": "https://tousu.sina.com.cn/"})
            lists = (((json.loads(resp.text()).get("result") or {}).get("data") or {})
                     .get("lists") or [])
        except Exception:
            return ""
        top = [f"{c.get('title')}（近30天 {c.get('valid30d')} 条投诉）" for c in lists[:3]]
        return "；".join(top)

    def search(self, ctx, keyword: str, limit: int, since: str, until: str):
        got = self._search_pages(ctx, keyword, limit, since, until)
        if got:                      # 登录态搜索页可用，直接返回（带翻页）
            return got
        posts, seen = [], set()
        hdrs = {"User-Agent": config.UA_PC, "Referer": "https://tousu.sina.com.cn/",
                "Accept": "application/json"}
        stat = self.company_stat(ctx, keyword)
        if stat:
            print(f"  [黑猫投诉] 关键词匹配到品牌：{stat}")
        scanned = 0
        for page_no in range(1, self.MAX_PAGE + 1):
            if len(posts) >= limit:
                break
            try:
                resp = ctx.request.get(self.FEED.format(page=page_no), headers=hdrs)
                data = json.loads(resp.text())
            except Exception:
                break
            lists = (((data.get("result") or {}).get("data") or {}).get("lists") or [])
            if not lists:
                break
            for item in lists:
                main = item.get("main") or item
                cid = str(main.get("id") or main.get("sn") or "")
                title = clean_text(main.get("title"))
                summary = clean_text(main.get("summary") or main.get("appeal") or "")
                scanned += 1
                if not cid or cid in seen:
                    continue
                blob = f"{title} {summary}"
                if keyword not in blob and not any(
                        w and w in blob for w in (keyword.replace("广告", ""),)):
                    continue
                seen.add(cid)
                publish = ts_to_date(main.get("timestamp")) or clean_text(main.get("time"))
                if not in_window(publish, since, until):
                    continue
                posts.append(self.new_post(
                    post_id=cid, title=title[:80], content=summary or title,
                    url=f"https://tousu.sina.com.cn/complaint/view/{cid}/",
                    author=clean_text(main.get("author") or ""),
                    publish_time=publish, keyword=keyword,
                    extra={"entry": "index/feed", "company": clean_text(main.get("cotitle") or ""),
                           "status": clean_text(main.get("status_name") or "")},
                ).finalize())
                if len(posts) >= limit:
                    break
        print(f"  [黑猫投诉] 扫了 {scanned} 条最新投诉，命中关键词 {len(posts)} 条")
        return posts
