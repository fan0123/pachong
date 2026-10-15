"""抖音 + 快手适配器：都靠拦截页面自身的搜索接口，绕开 a_bogus / graphql 签名。"""
from __future__ import annotations

from urllib.parse import quote

from ..intercept import ResponseCollector, find_records, walk_dicts
from ..models import in_window, to_int, ts_to_date
from .base import BasePlatform, register


@register
class Douyin(BasePlatform):
    name = "douyin"
    label = "抖音"
    LOGIN_URL = "https://www.douyin.com/"
    NEEDS_LOGIN = True
    METRICS = ("点赞", "评论", "分享", "收藏")
    NOTE = ("搜索必须走「首页搜索框输入 + 回车」，直接 goto /search/ 只会渲染导航外壳、接口一次都不发；"
            "点赞/评论/分享/收藏齐全，但**播放量 web 侧拿不到**（列表和详情接口都是 play_count=0，"
            "只有 App 内才有），所以不列进可采指标。")

    SEARCH_HINTS = ("/aweme/v1/web/general/search/single/",
                    "/aweme/v1/web/general/search/item/",
                    "/aweme/v1/web/search/item/")
    SEARCH_INPUT = "input[data-e2e='searchbar-input']"

    def is_logged_in(self, page) -> bool:
        if self.has_cookie(page, "sessionid", 10) or self.has_cookie(page, "sessionid_ss", 10):
            return True
        try:
            text = page.locator("body").inner_text(timeout=4000)
        except Exception:
            return False
        return "登录后免费畅享" not in text and "扫码登录" not in text[:400]

    def verify(self, ctx, page=None) -> tuple[bool, str]:
        """看搜索接口到底有没有被触发——DOM 会变，接口是硬信号。

        注意顺序：必须**先挂监听再搜索**。之前是搜完再 wait_api，而接口在输入回车后的
        等待里就已经返回了，wait_api 只等"新的"响应，于是登录态明明可用也被判成失效。
        """
        own = page is None
        page = page or ctx.new_page()
        collector = ResponseCollector(page, list(self.SEARCH_HINTS))
        try:
            if not self._do_search(page, "广告"):
                if self.has_cookie(page, "SEARCH_UN_LOGIN_PV_CURR_DAY", 1):
                    return False, "抖音标记为未登录访问（SEARCH_UN_LOGIN_PV_CURR_DAY），需扫码"
                return False, "搜索框没出现（登录墙/验证码），需扫码"
            for _ in range(10):                       # 最多再等 10 秒
                if collector.count:
                    return True, f"搜索接口已触发（{collector.count} 次响应），登录态可用"
                page.wait_for_timeout(1000)
            if self.has_cookie(page, "SEARCH_UN_LOGIN_PV_CURR_DAY", 1):
                return False, "抖音标记为未登录访问（SEARCH_UN_LOGIN_PV_CURR_DAY），需扫码"
            return False, "搜索接口没触发（登录墙或验证码），需扫码"
        except Exception as exc:
            return False, f"校验异常：{exc}"[:120]
        finally:
            if own:
                page.close()

    def _do_search(self, page, keyword: str) -> bool:
        """在首页搜索框里输入并回车。

        为什么不能直接 `goto("/search/<kw>")`：实测那样只会渲染「精选/推荐/关注」导航外壳，
        `general/search/single` 一次都不发（页面把搜索当成需要用户交互的路由）。
        走 UI 输入后 URL 会变成 /jingxuan/search/<kw>?aid=...，接口才正常返回。
        """
        try:
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
            page.wait_for_timeout(4500)
            box = page.locator(self.SEARCH_INPUT).first
            if not box.count():
                box = page.locator("input[placeholder*='搜索']").first
            if not box.count():
                return False
            box.click(timeout=8000)
            box.fill(keyword)
            page.keyboard.press("Enter")
            page.wait_for_timeout(6000)
            return True
        except Exception:
            return False

    def search(self, ctx, keyword: str, limit: int, since: str, until: str):
        page = ctx.new_page()
        collector = ResponseCollector(page, list(self.SEARCH_HINTS))
        posts = []
        try:
            if not self._do_search(page, keyword):
                print("  [抖音] 没找到搜索框（可能是登录墙/验证码），本次跳过")
                return posts
            self.scroll_until(page, collector, limit, max_scrolls=25,
                              required_keys=("aweme_id", "statistics"))
            records = find_records(collector.datas(), ("aweme_id", "statistics"))
            print(f"  [抖音] 搜索接口返回 {len(records)} 个视频记录")
            for rec in records:
                st = rec.get("statistics") or {}
                author = rec.get("author") or {}
                publish = ts_to_date(rec.get("create_time"))
                if not in_window(publish, since, until):
                    continue
                desc = rec.get("desc") or ""
                posts.append(self.new_post(
                    post_id=str(rec.get("aweme_id")), title=desc[:80], content=desc,
                    url=f"https://www.douyin.com/video/{rec.get('aweme_id')}",
                    author=author.get("nickname", ""),
                    author_id=str(author.get("sec_uid") or author.get("uid") or ""),
                    publish_time=publish,
                    likes=to_int(st.get("digg_count")),
                    comments=to_int(st.get("comment_count")),
                    shares=to_int(st.get("share_count")),
                    collects=to_int(st.get("collect_count")),
                    views=to_int(st.get("play_count")),
                    keyword=keyword,
                    extra={"followers": (author.get("follower_count")
                                         or (author.get("original_author") or {}).get("follower_count")),
                           "entry": "general/search/single"},
                ).finalize())
                if len(posts) >= limit:
                    break
            self._enrich_details(page, posts)
        finally:
            page.close()
        return posts

    def _enrich_details(self, page, posts, max_n: int = 12):
        """逐条打开视频详情页，用 aweme/detail 接口复核互动数（用户说的"点进去才能拿到"）。

        实测结论：详情接口的 `play_count` 也是 0——抖音 web 侧根本不下发播放量，
        所以这一步只能复核赞/评/转/藏，收益有限、耗时明显（每条要开一个页面）。
        因此默认关闭，需要时在「参数配置」里把 crawl.douyin_detail 打开。
        """
        from .. import settings

        if not settings.get("crawl", "douyin_detail"):
            return
        need = posts[:max_n]
        if not need:
            return
        print(f"  [抖音] 逐条开详情页复核互动数：{len(need)} 条（play_count web 侧恒为 0）")
        detail = ResponseCollector(page, ["/aweme/v1/web/aweme/detail/"])
        for p in need:
            detail.clear()
            try:
                page.goto(p.url, wait_until="domcontentloaded")
                self.wait_api(page, "/aweme/v1/web/aweme/detail/", 12000)
                page.wait_for_timeout(1200)
            except Exception:
                continue
            for rec in find_records(detail.datas(), ("aweme_id", "statistics")):
                if str(rec.get("aweme_id")) != p.post_id:
                    continue
                st = rec.get("statistics") or {}
                p.views = to_int(st.get("play_count")) or p.views
                p.likes = to_int(st.get("digg_count")) or p.likes
                p.comments = to_int(st.get("comment_count")) or p.comments
                p.shares = to_int(st.get("share_count")) or p.shares
                p.collects = to_int(st.get("collect_count")) or p.collects
                p.extra["metric_src"] = "aweme/detail"
                p.finalize()
                break


@register
class Kuaishou(BasePlatform):
    name = "kuaishou"
    label = "快手"
    LOGIN_URL = "https://www.kuaishou.com/"
    NEEDS_LOGIN = True
    METRICS = ("点赞", "收藏", "播放")
    NOTE = ("搜索走 /rest/v/search/feed（老版本是 /graphql 的 visionSearchPhoto，"
            "站点已切到 REST，两个都拦以兼容），需登录 cookie。")
    # 站点接口换过一次：老版 graphql，新版 rest/v/search/feed。只拦老的会一直判成"未登录"。
    SEARCH_HINTS = ("/rest/v/search/feed", "/graphql")

    def is_logged_in(self, page) -> bool:
        # 快手的登录 cookie 名字带日期后缀（webday7_st / webday30_st 之类），会随策略变，
        # 所以按前缀匹配，别写死具体名字——写死过一次，换了名字就一直显示未登录。
        try:
            ctx = getattr(page, "context", page)
            for c in ctx.cookies():
                nm = str(c.get("name") or "")
                if len(str(c.get("value") or "")) < 10:
                    continue
                if nm == "passToken" or nm.startswith("kuaishou.server.web"):
                    return True
        except Exception:
            pass
        try:
            text = page.locator("body").inner_text(timeout=4000)
        except Exception:
            return False
        return "扫码登录" not in text[:400]

    def verify(self, ctx, page=None) -> tuple[bool, str]:
        """搜索接口发不出来才是没登录。

        两处坑都在这里：
        1) **先挂监听再导航**。之前 goto 之后才 wait_api，而接口在 domcontentloaded 前后
           就返回了，监听挂晚了收不到，于是能正常搜索也被判成"需扫码"。
        2) 站点把搜索接口从 /graphql 换成了 /rest/v/search/feed，只拦 graphql 永远拦不到。
        """
        own = page is None
        page = page or ctx.new_page()
        collector = ResponseCollector(page, list(self.SEARCH_HINTS))
        try:
            page.goto(f"https://www.kuaishou.com/search/video?searchKey={quote('广告')}",
                      wait_until="domcontentloaded")
            for _ in range(16):                     # 最多等 16 秒
                if collector.count:
                    break
                page.wait_for_timeout(1000)
            # 接口是硬信号：拦到了就说明搜索真的跑通了，不再拿页面文案去否定它
            # （页面上「登录后…」这类引导文案在已登录时也可能常驻，之前就是被它误伤的）
            if collector.count:
                return True, f"搜索接口已触发（{collector.count} 次响应），登录态可用"
            text = ""
            try:
                text = page.locator("body").inner_text(timeout=5000)
            except Exception:
                pass
            if "扫码登录" in text[:400]:
                return False, "搜索页弹登录框，需扫码"
            return False, "搜索接口没触发，需扫码"
        except Exception as exc:
            return False, f"校验异常：{exc}"[:120]
        finally:
            if own:
                page.close()

    def search(self, ctx, keyword: str, limit: int, since: str, until: str):
        page = ctx.new_page()
        collector = ResponseCollector(page, list(self.SEARCH_HINTS))
        posts = []
        try:
            page.goto(f"https://www.kuaishou.com/search/video?searchKey={quote(keyword)}",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            self.scroll_until(page, collector, limit, max_scrolls=25,
                              required_keys=("photo", "author"))
            seen = set()
            for data in collector.datas():
                for node in walk_dicts(data):
                    photo = node.get("photo")
                    if not isinstance(photo, dict):
                        continue
                    pid = str(photo.get("id") or photo.get("photoId") or "")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    author = node.get("author") or {}
                    publish = ts_to_date(photo.get("timestamp"))
                    if not in_window(publish, since, until):
                        continue
                    caption = photo.get("caption") or ""
                    posts.append(self.new_post(
                        post_id=pid, title=caption[:80], content=caption,
                        url=f"https://www.kuaishou.com/short-video/{pid}",
                        author=author.get("name", ""), author_id=str(author.get("id") or ""),
                        publish_time=publish,
                        likes=to_int(photo.get("likeCount") or photo.get("realLikeCount")),
                        comments=to_int(photo.get("commentCount")),
                        shares=to_int(photo.get("shareCount")),
                        # 新版 search/feed 不给评论/转发数，但给了收藏数
                        collects=to_int(photo.get("collectCount")),
                        views=to_int(photo.get("viewCount")),
                        keyword=keyword, extra={"duration": photo.get("duration")},
                    ).finalize())
                    if len(posts) >= limit:
                        return posts
        finally:
            page.close()
        return posts
