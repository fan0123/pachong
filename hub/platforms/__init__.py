"""平台注册表：import 即注册。"""
from .base import REGISTRY, all_platforms, get, register  # noqa: F401
from . import xhs, weibo, video, article, weixin, forum  # noqa: F401  触发 @register

PLATFORM_ORDER = ["xhs", "weibo", "douyin", "kuaishou", "baijiahao", "toutiao",
                  "weixin", "zhihu", "bilibili", "heimao"]


def ordered() -> list:
    known = [REGISTRY[n] for n in PLATFORM_ORDER if n in REGISTRY]
    rest = [p for n, p in REGISTRY.items() if n not in PLATFORM_ORDER]
    return known + rest
