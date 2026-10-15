"""微博适配器。

两条路：
1) s.weibo.com 高级搜索（需登录）——按天切片 timescope，能精确控时间区间、拿全互动数；
2) m.weibo.cn 容器接口（getIndex）——结构稳、created_at 精确到分钟，登录态能翻多页，
   未登录只有第 1 页（page>=2 一律 ok=-100，见 _api_search 注释）。

登录态下两条一起用：切片扫历史 + 实时容器补最新，覆盖率和新鲜度都靠这个组合。
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from urllib.parse import quote

from .. import config
from ..models import clean_text, in_window, to_int
from ..session import read_state
from .base import BasePlatform, register


@register
class Weibo(BasePlatform):
    name = "weibo"
    label = "微博"
    LOGIN_URL = "https://s.weibo.com/"
    NEEDS_LOGIN = True
    LOGIN_OPTIONAL = True
    MOBILE_WHEN_ANON = True     # 未登录时整个会话切移动端 UA，m.weibo.cn 才肯下发数据
    METRICS = ("点赞", "评论", "转发")
    NOTE = "登录后按天切片扫 s.weibo.com + m.weibo.cn 综合/实时容器翻页；未登录只有容器接口第 1 页。"

    # m.weibo.cn 搜索容器：type=1 是"综合"（热度混排，补权重高的历史帖），
    # type=61 是"实时"（时间倒序，补最新的）。两个都扫，覆盖率明显好于单接口。
    API_CONTAINERS = (("综合", 1), ("实时", 61))
    API_MAX_PAGE = 12

    def is_logged_in(self, page) -> bool:
        if "passport" in page.url or "login" in page.url:
            return False
        try:
            text = page.locator("body").inner_text(timeout=4000)
        except Exception:
            return False
        if any(m in text[:600] for m in ["扫描二维码登录", "账号登录", "注册微博"]):
            return False
        return self.has_cookie(page, "SUB", 20)

    def verify(self, ctx, page=None) -> tuple[bool, str]:
        """看 s.weibo.com 搜索结果页能不能解析出卡片，能才算真登录。

        旧 SUB cookie 会让 cookie 判定假阳性 → 会话不切移动端 UA → PC 路径静默 0 条，
        所以这里必须功能性探测。返回 False 不影响采集：微博是 LOGIN_OPTIONAL，走移动端降级。
        """
        own = page is None
        page = page or ctx.new_page()
        try:
            page.goto("https://s.weibo.com/weibo?q=" + quote("广告"),
                      wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            cards = page.locator("div.card-wrap[mid]").count()
            if cards:
                return True, f"s.weibo.com 出 {cards} 张卡片，可用高级搜索"
            return False, "未登录：只能走 m.weibo.cn 降级（首页约 5 条，无时间区间）"
        except Exception as exc:
            return False, f"校验异常：{exc}"[:120]
        finally:
            if own:
                page.close()

    def search(self, ctx, keyword: str, limit: int, since: str, until: str):
        """登录态下按天切片扫 s.weibo.com，未登录退 m.weibo.cn 接口。

        为什么要按天切：s.weibo.com 对一个大区间只放出前几页（约 20 条/页、10 页上限），
        3 个月一次查完必然只拿到头部热门。按天切片 + 每天翻页才能把覆盖率和新鲜度做上去，
        这也是 dataabc/weibo-search 的思路（它按天/小时切）。从最新的一天往回扫，先拿到新的。
        """
        page = ctx.new_page()
        posts, seen = [], set()
        days = self._days(since, until)
        try:
            for day in days:
                if len(posts) >= limit:
                    break
                scope = f"&timescope=custom%3A{day}%3A{day}" if day else ""
                for pno in range(1, 11):
                    url = (f"https://s.weibo.com/weibo?q={quote(keyword)}"
                           f"{scope}&page={pno}&xsort=hot")
                    try:
                        page.goto(url, wait_until="domcontentloaded")
                        page.wait_for_timeout(1800)
                        cards = self._parse_cards(page)
                    except Exception:
                        cards = []
                    if not cards:
                        # s.weibo.com 一条卡片都没解析出来 → 整体退容器接口。
                        # anon（要不要清 cookie）只看 state 里的登录结论：不能拿"某一天
                        # 恰好没结果"当"没登录"，那样会把用户扫码换来的登录态直接清掉。
                        if pno == 1 and not posts and day == days[0]:
                            anon = not read_state(self.name).get("logged_in")
                            page.close()
                            page = None
                            return self._api_search(ctx, keyword, limit, since, until,
                                                    anon=anon, seen=seen)
                        break
                    fresh = 0
                    for c in cards:
                        if c["mid"] in seen:
                            continue
                        seen.add(c["mid"])
                        fresh += 1
                        publish = self._card_time(c.get("raw_time", ""))
                        if not in_window(publish, since, until):
                            continue
                        posts.append(self.new_post(
                            post_id=c["mid"], title=c["text"][:80], content=c["text"],
                            url=c["url"] or f"https://weibo.com/detail/{c['mid']}",
                            author=c["author"], author_id=c.get("author_id", ""),
                            publish_time=publish,
                            likes=to_int(c["like"]), comments=to_int(c["comment"]),
                            shares=to_int(c["repost"]), keyword=keyword,
                            extra={"source": "s.weibo.com", "slice": day,
                                   "raw_time": c.get("raw_time", "")},
                        ).finalize())
                        if len(posts) >= limit:
                            return posts
                    if not fresh:
                        break
            # 切片扫完还没到量，再用容器接口补一遍：登录态下它能翻 7 页以上，
            # 且 created_at 精确到分钟，正好补 s.weibo.com 只放热门头部的缺口。
            if len(posts) < limit:
                posts.extend(self._api_search(ctx, keyword, limit - len(posts),
                                              since, until, anon=False, seen=seen))
        finally:
            if page:
                page.close()
        return posts

    @staticmethod
    def _days(since: str, until: str, cap: int = 120) -> list[str]:
        """把区间拆成一天一个切片，从最近的一天往回；没给区间就返回 [""] 走不切片。"""
        if not (since and until):
            return [""]
        try:
            a = date.fromisoformat(since)
            b = date.fromisoformat(until)
        except ValueError:
            return [""]
        if b < a:
            a, b = b, a
        out = []
        cur = b
        while cur >= a and len(out) < cap:
            out.append(cur.isoformat())
            cur -= timedelta(days=1)
        return out

    @staticmethod
    def _parse_cards(page) -> list[dict]:
        """解析 s.weibo.com 搜索卡片。

        时间和永久链接都在 `.from` 里的第一个 a 上：
        href = //weibo.com/<uid>/<bid>?refer_flag=...，text = "08月25日 15:08"。
        注意这个容器是 div.from，早先按 p.from 取会拿到 null，于是时间和链接双双为空。
        """
        return page.evaluate("""() => Array.from(document.querySelectorAll('div.card-wrap[mid]'))
            .map(el => {
              const txtEl = el.querySelector('p.txt[node-type="feed_list_content_full"]')
                         || el.querySelector('p.txt');
              const a = el.querySelector('.from a');
              const nameA = el.querySelector('a.name');
              const acts = Array.from(el.querySelectorAll('.card-act li'))
                                .map(li => (li.innerText || '').replace(/\\s+/g, ' ').trim());
              let href = a ? (a.getAttribute('href') || '') : '';
              if (href.startsWith('//')) href = 'https:' + href;
              return {
                mid: el.getAttribute('mid') || '',
                text: txtEl ? txtEl.innerText.replace(/\\s+/g, ' ').trim() : '',
                author: nameA ? (nameA.getAttribute('nick-name') || nameA.innerText || '').trim() : '',
                author_id: (nameA ? (nameA.getAttribute('href') || '') : '')
                             .replace(/^.*weibo\\.com\\//, '').split('?')[0],
                raw_time: a ? (a.innerText || '').trim() : '',
                url: href.split('?')[0],
                repost: acts[0] || '', comment: acts[1] || '', like: acts[2] || ''
              };
            }).filter(x => x.mid && x.text)""")

    @staticmethod
    def _card_time(raw: str) -> str:
        """把卡片上的时间文本转成 ISO。

        搜索页给的是 "08月25日 15:08"（不带年）、"今天 12:30"、"5分钟前"、
        偶尔是 "2025-06-24 15:08"。不带年的按"不能晚于今天"推年份。
        """
        raw = (raw or "").strip()
        if not raw:
            return ""
        m = re.search(r"(20\d\d)-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", raw)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hm = f" {int(m.group(4) or 0):02d}:{m.group(5) or '00'}" if m.group(4) else ""
            try:
                return date(y, mo, d).isoformat() + hm
            except ValueError:
                return ""
        m = re.search(r"(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?", raw)
        if m:
            mo, d = int(m.group(1)), int(m.group(2))
            hm = f" {int(m.group(3) or 0):02d}:{m.group(4) or '00'}" if m.group(3) else ""
            today = date.today()
            year = today.year
            try:
                if date(year, mo, d) > today:
                    year -= 1
                return date(year, mo, d).isoformat() + hm
            except ValueError:
                return ""
        today = date.today()
        if "今天" in raw or "分钟前" in raw or "秒前" in raw:
            return today.isoformat()
        if "小时前" in raw:
            return today.isoformat()
        if "昨天" in raw:
            return (today - timedelta(days=1)).isoformat()
        return ""

    def _api_search(self, ctx, keyword: str, limit: int, since: str, until: str,
                    anon: bool = True, seen: set | None = None):
        """m.weibo.cn 容器接口，综合 + 实时两个容器各自翻页。

        实测（2026-09，关键词"开屏广告"）：未登录 type=1/61 都只有 page=1 出数据，
        page>=2 一律 ok=-100；登录态下两个容器都能翻到 7 页以上、共 140+ 条。
        也就是说"微博接口免登录随便爬"在现在的风控下不成立，所以这里不假设深度：
        翻到接口不给了就停，并把实际深度打出来，免得静默少数据。
        """
        seen = set() if seen is None else seen
        out = []
        if anon:
            # profile 里残留的过期 SUB/SRT 会让接口直接 ok:-100 要求登录，未登录态清掉无损失
            try:
                ctx.clear_cookies()
            except Exception:
                pass
            page = ctx.new_page()
            try:
                # 必须真实导航一次拿到游客 SUB，只发 request 会被判定无会话
                page.goto("https://m.weibo.cn/", wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
            except Exception:
                pass
            finally:
                page.close()
        for label, ctype in self.API_CONTAINERS:
            cid = quote(f"100103type={ctype}&q={keyword}&t=0")
            hdrs = {
                "User-Agent": config.UA_MOBILE,
                "Referer": f"https://m.weibo.cn/search?containerid={cid}",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            }
            depth, got = 0, 0
            for pno in range(1, self.API_MAX_PAGE + 1):
                api = (f"https://m.weibo.cn/api/container/getIndex?containerid={cid}"
                       f"&page_type=searchall&page={pno}")
                try:
                    data = json.loads(ctx.request.get(api, headers=hdrs).text())
                except Exception:
                    break
                if data.get("ok") != 1:
                    if pno == 1:
                        print(f"  [微博] {label}容器 ok={data.get('ok')}，需要登录态"
                              f"（控制台 login weibo 扫码后重试）")
                    break
                blogs = self._collect_blogs(data)
                if not blogs:
                    break
                depth = pno
                for mb in blogs:
                    post = self._post_from_mblog(mb, keyword, label)
                    if post is None or post.post_id in seen:
                        continue
                    seen.add(post.post_id)
                    if not in_window(post.publish_time, since, until):
                        continue
                    out.append(post)
                    got += 1
                    if len(out) >= limit:
                        print(f"  [微博] {label}容器翻到第 {depth} 页达量，入窗 {got} 条")
                        return out
                time.sleep(0.6)
            print(f"  [微博] {label}容器有效深度 {depth} 页，入窗 {got} 条")
        return out

    @staticmethod
    def _collect_blogs(data: dict) -> list[dict]:
        """容器返回里 mblog 有两种挂法：card.mblog 和 card.card_group[].mblog。"""
        blogs = []
        for card in ((data.get("data") or {}).get("cards") or []):
            if card.get("mblog"):
                blogs.append(card["mblog"])
            for cc in card.get("card_group") or []:
                if cc.get("mblog"):
                    blogs.append(cc["mblog"])
        return blogs

    def _post_from_mblog(self, mb: dict, keyword: str, label: str):
        """mblog → Post。链接优先 weibo.com/<uid>/<bid>（PC 正文页），bid 缺了才退 m 站。"""
        mid = str(mb.get("id") or mb.get("mid") or "")
        text = clean_text(mb.get("text"))
        if not (mid and text):
            return None
        user = mb.get("user") or {}
        uid, bid = str(user.get("id") or ""), mb.get("bid") or ""
        url = (f"https://weibo.com/{uid}/{bid}" if uid and bid
               else f"https://m.weibo.cn/detail/{mid}")
        return self.new_post(
            post_id=mid, title=text[:80], content=text, url=url,
            author=user.get("screen_name", ""), author_id=uid,
            publish_time=self._parse_created(mb.get("created_at", "")),
            likes=to_int(mb.get("attitudes_count")),
            comments=to_int(mb.get("comments_count")),
            shares=to_int(mb.get("reposts_count")), keyword=keyword,
            extra={"source": f"m.weibo.cn/{label}",
                   "followers": user.get("followers_count")},
        ).finalize()

    COMMENTS_SUPPORTED = True

    def fetch_comments(self, ctx, post, limit: int = 30) -> list[dict]:
        """采一条微博的评论，走 m.weibo.cn 的 hotflow JSON 接口（比爬 DOM 稳）。

        用 ctx.request 复用登录态 cookie。mid 就是 post_id。返回字段：
        评论者 screen_name、时间 created_at、正文 text（含 HTML，去标签）、赞数。
        """
        mid = str(getattr(post, "post_id", "") or "")
        if not mid:
            return []
        api = (f"https://m.weibo.cn/comments/hotflow?id={mid}&mid={mid}&max_id_type=0")
        out = []
        try:
            resp = ctx.request.get(api, headers={
                "Referer": f"https://m.weibo.cn/detail/{mid}",
                "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                               "AppleWebKit/605.1.15 Mobile/15E148"),
                "X-Requested-With": "XMLHttpRequest",
                "MWeibo-Pwa": "1",
            }, timeout=12000)
            data = resp.json() if resp.ok else {}
        except Exception:
            return []
        items = ((data.get("data") or {}).get("data")) or []
        for c in items[:limit]:
            user = c.get("user") or {}
            out.append({
                "comment_id": str(c.get("id") or ""),
                "commenter": user.get("screen_name") or "",
                "commenter_id": str(user.get("id") or ""),
                "comment_time": self._parse_created(c.get("created_at", "")),
                "content": clean_text(re.sub(r"<[^>]+>", "", c.get("text") or "")),
                "likes": to_int(c.get("like_count")),
            })
        return [c for c in out if (c.get("content") or "").strip()]

    @staticmethod
    def _parse_created(value: str) -> str:
        from datetime import datetime, timedelta
        try:
            return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        now = datetime.now()
        m = re.search(r"(\d+)\s*分钟前", value)
        if m:
            return (now - timedelta(minutes=int(m.group(1)))).strftime("%Y-%m-%d %H:%M")
        m = re.search(r"(\d+)\s*小时前", value)
        if m:
            return (now - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d %H:%M")
        if "昨天" in value:
            return (now - timedelta(days=1)).strftime("%Y-%m-%d")
        m = re.search(r"(\d{1,2})-(\d{1,2})", value)
        if m:
            return f"{now.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        return ""
