"""XHR 响应拦截：不做签名逆向，让真实页面自己去请求，我们只截它的 JSON 返回。

这是本平台绕过 x-s / X-Bogus / a_bogus / xsec_token 等签名的核心手段：
MediaCrawler 走的是"注入 JS 复用页面签名函数"，这里更进一步——直接监听
page.on("response")，页面翻页/滚动时接口返回什么就收什么，签名算法完全不用管。
"""
from __future__ import annotations

import json


class ResponseCollector:
    """按 URL 关键字收集 JSON 响应。"""

    def __init__(self, page, url_hints: list[str]):
        self.hints = url_hints
        self.payloads: list[dict] = []
        self.errors: list[str] = []
        page.on("response", self._on_response)

    def _match(self, url: str) -> bool:
        return any(h in url for h in self.hints)

    def _on_response(self, response):
        try:
            if not self._match(response.url):
                return
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype and not response.url.endswith(".json"):
                # 有些接口不带 content-type，仍尝试解析
                pass
            body = response.text()
            if not body or not body.lstrip().startswith(("{", "[")):
                return
            self.payloads.append({"url": response.url, "data": json.loads(body)})
        except Exception as exc:  # 页面跳转导致 body 不可读等，忽略
            self.errors.append(f"{response.url[:80]}: {exc}"[:160])

    @property
    def count(self) -> int:
        return len(self.payloads)

    def datas(self) -> list:
        return [p["data"] for p in self.payloads]

    def clear(self):
        self.payloads.clear()


def dig(obj, *keys, default=None):
    """安全取多层键：dig(d, 'data', 'items', 0, 'title')。"""
    cur = obj
    for k in keys:
        try:
            if isinstance(k, int):
                cur = cur[k]
            else:
                cur = cur.get(k)
        except Exception:
            return default
        if cur is None:
            return default
    return cur


def walk_dicts(obj):
    """深度遍历，产出所有 dict —— 接口结构一变也能兜住。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_dicts(v)


def find_records(payloads: list, required_keys: tuple[str, ...]) -> list[dict]:
    """在任意嵌套结构里，捞出同时含指定键的 dict（去重按 id 优先）。"""
    out, seen = [], set()
    for data in payloads:
        for d in walk_dicts(data):
            if all(k in d for k in required_keys):
                key = str(d.get("id") or d.get("note_id") or d.get("aweme_id")
                          or d.get("photoId") or id(d))
                if key in seen:
                    continue
                seen.add(key)
                out.append(d)
    return out
