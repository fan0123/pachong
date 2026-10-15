"""统一数据模型：所有平台的帖子都规范化成 Post。"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

FIELDS = [
    "platform", "platform_label", "post_id", "title", "content", "url",
    "author", "author_id", "publish_time", "likes", "comments", "shares",
    "collects", "views", "keyword", "engagement", "fetched_at", "extra",
    "layer", "layer_reason", "actor", "actor_reason", "sentiment", "sentiment_reason",
]


@dataclass
class Post:
    platform: str
    platform_label: str
    post_id: str
    title: str = ""
    content: str = ""
    url: str = ""
    author: str = ""
    author_id: str = ""
    publish_time: str = ""       # YYYY-MM-DD 或 YYYY-MM-DD HH:MM
    likes: int = 0               # 点赞
    comments: int = 0            # 评论数
    shares: int = 0              # 转发/分享
    collects: int = 0            # 收藏
    views: int = 0               # 播放/阅读
    keyword: str = ""
    engagement: int = 0          # 互动量 = 赞 + 2×评论 + 3×转发 + 2×收藏
    fetched_at: str = ""
    extra: dict = field(default_factory=dict)
    layer: str = ""              # 清洗分层：A_UGC负反馈 / B_媒体负面报道 / C_中性事件报道 / D_噪音 / E_正面反馈
    layer_reason: str = ""       # 判定依据，便于人工复核
    actor: str = ""              # 主体：官方机构/官方媒体/新闻媒体/平台企业号/大V博主/自媒体号/普通用户
    actor_reason: str = ""
    sentiment: str = ""          # 情感：负面 / 中性 / 正面
    sentiment_reason: str = ""

    def finalize(self) -> "Post":
        self.engagement = (self.likes + self.comments * 2 + self.shares * 3
                           + self.collects * 2)
        self.fetched_at = self.fetched_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.title = clean_text(self.title)[:300]
        self.content = clean_text(self.content)[:4000]
        return self

    def row(self) -> dict:
        d = asdict(self)
        d["extra"] = json.dumps(d["extra"], ensure_ascii=False)
        return d


def clean_text(text) -> str:
    if text is None:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))
    return re.sub(r"[\r\n\t\u200b]+", " ", text).strip()


def to_int(value) -> int:
    """把 '1.2万' / '3562' / '1,024' / None 统一成 int。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace(",", "").replace("+", "")
    if not s or s in {"-", "赞", "评论", "分享", "收藏"}:
        return 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*([万亿wW])?", s)
    if not m:
        return 0
    num = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in {"万", "w"}:
        num *= 10000
    elif unit == "亿":
        num *= 100000000
    return int(num)


def ts_to_date(ts, with_time: bool = True) -> str:
    """秒级/毫秒级时间戳 -> 字符串。"""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return ""
    if ts > 1e12:
        ts /= 1000
    if ts <= 0:
        return ""
    fmt = "%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d"
    return datetime.fromtimestamp(ts).strftime(fmt)


def in_range(publish_time: str, since: str = "", until: str = "") -> bool:
    if not publish_time:
        return True
    day = publish_time[:10]
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


def in_window(publish_time: str, since: str = "", until: str = "") -> bool:
    """严格版：给了时间范围就必须有发布时间且落在范围内。

    in_range 对"没解析出日期"的条目是放过的（宁可多收），但看板要"不超出所选区间"，
    所以采集侧统一用这个：拿不到日期的条目在指定区间时直接丢，避免混进区间外内容。
    """
    if since or until:
        if not publish_time:
            return False
    return in_range(publish_time, since, until)
