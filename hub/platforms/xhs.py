"""小红书适配器。搜索页由页面自己发签名请求，我们拦截 /search/notes 的返回。"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import quote

from .. import config
from ..intercept import ResponseCollector, dig, find_records
from ..models import in_window, to_int, ts_to_date
from .base import BasePlatform, register
from .article import parse_loose_date

# 2026-09 起搜索接口是 so.xiaohongshu.com/api/sns/web/v2/search/notes
# （原来是 edith.xiaohongshu.com 的 v1）。只匹配路径尾巴，别写死主机和版本号。
SEARCH_API = "/search/notes"


def _first_text(scope, selectors: list[str]) -> str:
    """按候选选择器依次取第一个非空文本。DOM 类名会变，多给几个兜底。"""
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if loc.count():
                t = (loc.inner_text(timeout=1500) or "").strip()
                if t:
                    return t
        except Exception:
            continue
    return ""


@register
class Xhs(BasePlatform):
    name = "xhs"
    label = "小红书"
    LOGIN_URL = "https://www.xiaohongshu.com/explore"
    NEEDS_LOGIN = True
    METRICS = ("点赞", "评论", "收藏", "分享")
    NOTE = "搜索接口不返回发布时间，需开详情页补；未登录搜索页强制登录墙。"

    def is_logged_in(self, page) -> bool:
        if "website-login" in page.url or "login" in page.url:
            return False
        try:
            text = page.locator("body").inner_text(timeout=4000)
        except Exception:
            text = ""
        if "登录后查看" in text or ("手机号登录" in text and "获取验证码" in text):
            return False
        return self.has_cookie(page, "web_session", 10) or self.has_cookie(page, "a1", 10)

    def verify(self, ctx, page=None) -> tuple[bool, str]:
        """打开真实搜索页看结果：未登录时这里是"登录后查看搜索结果"的墙。

        不能只看 cookie——未登录状态下 web_session/a1 也在，会把"需扫码"显示成"可用"。
        """
        own = page is None
        page = page or ctx.new_page()
        try:
            page.goto("https://www.xiaohongshu.com/search_result?keyword="
                      + quote("广告") + "&source=web_explore_feed&type=51",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            text = page.locator("body").inner_text(timeout=5000)
            if "登录后查看" in text or "手机号登录" in text:
                return False, "搜索页仍是登录墙，需要扫码"
            if page.locator("section.note-item").count() > 0:
                return True, "搜索页能出笔记，登录态可用"
            return False, "搜索页没出笔记，登录态可能已失效"
        except Exception as exc:
            return False, f"校验异常：{exc}"[:120]
        finally:
            if own:
                page.close()

    def search(self, ctx, keyword: str, limit: int, since: str, until: str):
        """三趟扫：默认「综合」→「最新」→「最多评论」，同一关键词换排序换出不同结果集。

        小红书搜索既没有时间区间参数也没有页码，只能滚动加载，而单一排序滚到底大概
        三十条就没了：综合偏历史爆款（"不够新"）、最新补新帖、最多评论补讨论量大的。
        """
        page = ctx.new_page()
        collector = ResponseCollector(page, [SEARCH_API])
        url = (f"https://www.xiaohongshu.com/search_result?keyword={quote(keyword)}"
               f"&source=web_explore_feed&type=51")
        posts, seen = [], set()
        try:
            for tab in ("综合", "最新", "最多评论"):
                if len(posts) >= limit:
                    break
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                # 先清掉上一趟和本次 goto（还是旧排序）的返回，再切排序：
                # 切排序后接口马上就回，那一份必须留着，所以 clear 不能放在切换之后
                collector.clear()
                if tab != "综合" and not self._sort_by(page, tab):
                    print(f"  [小红书] 没找到「{tab}」排序入口，跳过这一趟")
                    continue
                self.scroll_until(page, collector, limit, max_scrolls=25,
                                  required_keys=("id", "note_card"))
                got = self._from_records(collector, keyword, limit - len(posts),
                                         since, until, seen, tab)
                print(f"  [小红书] {tab}排序新增 {len(got)} 条")
                posts += got
            if not posts:
                posts = self._dom_fallback(page, keyword, limit)
        finally:
            page.close()
        # 搜索接口**不返回 desc**，只能拿到 display_title。不补正文的话入库的 content
        # 就是标题的副本（实测 529 条正文中位数 14 字、100% 等于标题），
        # 下游的情感/意图判定等于只能看一句标题，误判率极高。
        # 所以默认开详情页补一次，代价是每条多一次访问 + 节流。
        from .. import settings
        if settings.get("crawl", "xhs_detail"):
            self.enrich_desc(ctx, posts)
        return posts

    @staticmethod
    def enrich_desc(ctx, posts: list, pause_ms: int = 900) -> int:
        """逐条打开详情页补正文。返回补到的条数。

        `#detail-desc` 是正文容器，实测最稳；`__INITIAL_STATE__` 里的 desc 带
        `[汗颜R]` 这类表情占位符和 `#话题[话题]#` 后缀，反而更脏，所以取 DOM 文本。
        单条失败不影响整批——补不到就保留原来的标题副本。
        """
        if not posts:
            return 0
        page = ctx.new_page()
        ok = 0
        try:
            for i, post in enumerate(posts, 1):
                url = getattr(post, "url", "") or ""
                if not url:
                    continue
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_selector("#detail-desc", timeout=6000)
                    desc = page.locator("#detail-desc").first.inner_text(timeout=3000)
                except Exception:
                    continue
                desc = (desc or "").strip()
                if len(desc) > len(getattr(post, "content", "") or ""):
                    post.content = desc
                    ok += 1
                page.wait_for_timeout(pause_ms)
            print(f"  [小红书] 详情页补正文 {ok}/{len(posts)} 条")
        finally:
            page.close()
        return ok

    COMMENTS_SUPPORTED = True

    def fetch_comments(self, ctx, post, limit: int = 30) -> list[dict]:
        """采一条笔记的评论。评论在详情页 `.comment-item` 里，滚动懒加载。

        DOM 类名小红书会改，所以每个字段都给几个候选选择器兜底，取到就用；
        评论时间是「3天前 广东」这类相对文案，原样存，交给下游需要时再解析。
        """
        url = getattr(post, "url", "") or ""
        if not url:
            return []
        page = ctx.new_page()
        out = []
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            # 评论区靠滚动加载，滚几屏尽量把 limit 条凑够
            for _ in range(8):
                if page.locator(".comment-item, .comment-item-wrapper").count() >= limit:
                    break
                page.mouse.wheel(0, 2600)
                page.wait_for_timeout(900)
            items = page.locator(".comment-item, .comment-item-wrapper")
            n = min(items.count(), limit)
            for i in range(n):
                it = items.nth(i)
                out.append({
                    "comment_id": (it.get_attribute("data-comment-id")
                                   or it.get_attribute("id") or ""),
                    "commenter": _first_text(it, [".author .name", ".name", ".author-wrapper .name"]),
                    "comment_time": _first_text(it, [".date span", ".date", ".time", ".info .date"]),
                    "content": _first_text(it, [".content .note-text", ".content", ".note-text"]),
                    "likes": to_int(_first_text(it, [".like .count", ".like-wrapper .count", ".count"])),
                })
        except Exception:
            pass
        finally:
            page.close()
        return [c for c in out if (c.get("content") or "").strip()]

    @staticmethod
    def _sort_by(page, name: str) -> bool:
        """切换排序。两个坑：
        1) 筛选面板是 **hover** `div.filter` 展开的，点击反而会收起；
        2) `div.tags` 有一份 `aria-hidden="true"` 的副本，点它会被真实节点的 span
           拦掉指针事件（报 "subtree intercepts pointer events"），所以要挑可见那份，
           并且直接点它里面的 span。
        """
        try:
            page.locator("div.filter").first.hover(timeout=4000)
            page.wait_for_timeout(1200)
            tags = page.locator("div.tags").filter(has_text=re.compile(f"^{name}$"))
            for i in range(tags.count()):
                tag = tags.nth(i)
                if tag.get_attribute("aria-hidden") == "true" or not tag.is_visible():
                    continue
                tag.locator("span").first.click(timeout=4000)
                page.wait_for_timeout(3000)
                return True
            return False
        except Exception:
            return False

    def _from_records(self, collector, keyword: str, limit: int,
                      since: str, until: str, seen: set, tab: str):
        posts = []
        for rec in find_records(collector.datas(), ("id", "note_card")):
            card = rec.get("note_card") or {}
            interact = card.get("interact_info") or {}
            user = card.get("user") or {}
            note_id = rec.get("id") or card.get("note_id") or ""
            if not note_id or note_id in seen:
                continue
            # 搜索结果里混着广告位/热搜词卡，它们没有 display_title，只带个 liked_count，
            # 收进来就是标题为空、互动量虚高的脏数据
            if not (card.get("display_title") or "").strip():
                continue
            seen.add(note_id)
            token = rec.get("xsec_token") or card.get("xsec_token") or ""
            publish = (ts_to_date(card.get("time")) or ts_to_date(card.get("last_update_time"))
                       or self._corner_time(card))
            if not in_window(publish, since, until):
                continue
            posts.append(self.new_post(
                post_id=str(note_id),
                title=card.get("display_title") or "",
                content=card.get("desc") or card.get("display_title") or "",
                url=f"https://www.xiaohongshu.com/explore/{note_id}"
                    + (f"?xsec_token={token}&xsec_source=pc_search" if token else ""),
                author=user.get("nickname") or user.get("nick_name") or "",
                author_id=str(user.get("user_id") or ""),
                publish_time=publish,
                likes=to_int(dig(interact, "liked_count")),
                comments=to_int(dig(interact, "comment_count")),
                shares=to_int(dig(interact, "shared_count")),
                collects=to_int(dig(interact, "collected_count")),
                keyword=keyword,
                extra={"note_type": card.get("type"), "xsec_token": token, "sort": tab},
            ).finalize())
            if len(posts) >= limit:
                break
        return posts

    @staticmethod
    def _corner_time(card: dict) -> str:
        """搜索接口不返回 time/desc，发布时间只在角标里：'8小时前' / '06-26' / '2025-11-11'。"""
        for tag in card.get("corner_tag_info") or []:
            if tag.get("type") and tag.get("type") != "publish_time":
                continue
            text = str(tag.get("text") or "")
            if not text:
                continue
            iso = parse_loose_date(text)
            if iso:
                return iso
            # 只有 "06-26" 这种没年份的，按当年补
            if re.fullmatch(r"\d{1,2}-\d{1,2}", text):
                iso = parse_loose_date(f"{date.today().year}-{text}")
                if iso:
                    return iso
        return ""

    def _dom_fallback(self, page, keyword: str, limit: int):
        """接口结构变动时的兜底：直接读搜索卡片 DOM。"""
        items = page.evaluate("""() => Array.from(document.querySelectorAll('section.note-item'))
            .map(el => {
              const a = el.querySelector('a[href*="/explore/"], a[href*="/search_result/"]');
              const t = el.querySelector('.title, span');
              const like = el.querySelector('.like-wrapper .count, .count');
              return {
                href: a ? a.href : '',
                title: t ? t.innerText.trim() : '',
                author: (el.querySelector('.author .name, .name') || {}).innerText || '',
                like: like ? like.innerText.trim() : ''
              };
            }).filter(x => x.href)""")
        out = []
        for it in items[:limit]:
            note_id = it["href"].rstrip("/").split("/")[-1].split("?")[0]
            out.append(self.new_post(
                post_id=note_id, title=it["title"], content=it["title"],
                url=it["href"], author=(it.get("author") or "").strip(),
                likes=to_int(it.get("like")), keyword=keyword,
                extra={"source": "dom_fallback"},
            ).finalize())
        return out
