"""平台适配器基类。新增平台只要继承它并实现 is_logged_in + search。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .. import config
from ..models import Post


class BasePlatform(ABC):
    name: str = ""
    label: str = ""
    LOGIN_URL: str = ""
    NEEDS_LOGIN: bool = True
    # True 表示没登录也能跑（能力降级），SilentSession 只告警不拦截
    LOGIN_OPTIONAL: bool = False
    MOBILE: bool = False
    # 该平台能拿到哪些指标，供 CLI/文档展示，避免对外承诺拿不到的数据
    METRICS: tuple[str, ...] = ()
    NOTE: str = ""

    @abstractmethod
    def is_logged_in(self, page) -> bool:
        ...

    @abstractmethod
    def search(self, ctx, keyword: str, limit: int, since: str, until: str) -> list[Post]:
        ...

    # 是否实现了评论采集。默认 False：没实现的平台采集时会明确告知"暂不支持"，
    # 而不是静默返回 0 条让人以为是这条帖子没评论。
    COMMENTS_SUPPORTED: bool = False

    def fetch_comments(self, ctx, post, limit: int = 30) -> list[dict]:
        """采一条帖子下的评论，返回 dict 列表。默认不支持，返回空。

        每条至少给：comment_id / commenter / comment_time / content，可选 likes / commenter_id。
        实现方需自己节流；单条帖子失败不要抛，返回已拿到的部分即可，别拖垮整批采集。
        """
        return []

    def verify(self, ctx, page=None) -> tuple[bool, str]:
        """功能性校验登录态，返回 (是否可用, 人话原因)。

        cookie 存在 ≠ 登录有效：小红书未登录也发 web_session、抖音未登录也有 odin_tt，
        光看 cookie 会把「需扫码」显示成「登录态可用」，然后采集静默返回 0 条。
        能功能性探测的平台都覆写这个方法，直接看搜索结果出不出来。

        page 传进来就复用（扫码轮询时用，避免反复开标签页抢焦点），否则自己开自己关。
        """
        own = page is None
        page = page or ctx.new_page()
        try:
            page.goto(self.LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            ok = bool(self.is_logged_in(page))
            return ok, "登录态可用" if ok else "页面判定为未登录"
        except Exception as exc:
            return False, f"校验异常：{exc}"[:120]
        finally:
            if own:
                page.close()

    @staticmethod
    def wait_api(page, hint: str, timeout_ms: int = 12000) -> bool:
        """等某个接口出现，用来判断"搜索到底跑起来没有"（比猜 DOM 稳）。"""
        seen = []
        page.on("response", lambda r: seen.append(r.url) if hint in r.url else None)
        waited = 0
        while waited < timeout_ms:
            if seen:
                return True
            page.wait_for_timeout(500)
            waited += 500
        return bool(seen)

    # ---------- 共用小工具 ----------

    def new_post(self, **kw) -> Post:
        kw.setdefault("platform", self.name)
        kw.setdefault("platform_label", self.label)
        return Post(**kw)

    @staticmethod
    def has_cookie(ctx_or_page, name: str, min_len: int = 8) -> bool:
        try:
            ctx = getattr(ctx_or_page, "context", ctx_or_page)
            for c in ctx.cookies():
                if c.get("name") == name and len(str(c.get("value") or "")) >= min_len:
                    return True
        except Exception:
            return False
        return False

    @staticmethod
    def scroll_until(page, collector, target: int, max_scrolls: int = 30,
                     required_keys: tuple[str, ...] = ()) -> None:
        """向下滚动直到拦截到足够记录或滚不动为止。"""
        from ..intercept import find_records
        last, stall = 0, 0
        for _ in range(max_scrolls):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(config.SCROLL_PAUSE_MS)
            got = len(find_records(collector.datas(), required_keys)) if required_keys \
                else collector.count
            if got >= target:
                return
            if got == last:
                stall += 1
                if stall >= 4:
                    return
            else:
                stall = 0
            last = got


REGISTRY: dict[str, BasePlatform] = {}


def register(cls):
    inst = cls()
    REGISTRY[inst.name] = inst
    return cls


def get(name: str) -> BasePlatform:
    if name not in REGISTRY:
        raise KeyError(f"未注册的平台：{name}，可用：{', '.join(REGISTRY)}")
    return REGISTRY[name]


def all_platforms() -> list[BasePlatform]:
    return list(REGISTRY.values())
