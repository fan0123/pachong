"""可自行配置的运行参数：主体权重、大V阈值、采集默认值、报告与词云参数。

为什么单独做一层：这些数都是"可以有不同看法"的口径参数（大V算多少粉丝起？官媒权重给多少？
报告喂多少条样本？），写死在代码里等于把判断替用户做了。放到 data/settings.json，
页面上能改、能恢复默认，改完立刻生效（各模块在调用时读，不在导入时读）。

规则：**只有 DEFAULTS 里声明过的键才会被接受**，防止页面塞进来奇怪的键污染配置；
数值类型跟着默认值的类型走，转不动就丢掉这次改动，宁可用默认值也不要让配置把功能弄崩。
"""
from __future__ import annotations

import json
import threading

from . import config

DEFAULTS: dict = {
    # 声量换算里的主体权重（补的是抓不到的跨渠道转载放大）
    "actor_weight": {
        "官方机构": 2.0, "官方媒体": 1.8, "新闻媒体": 1.4, "大V博主": 1.3,
        "平台企业号": 1.0, "自媒体号": 1.0, "普通用户": 1.0,
    },
    # 大V判定阈值：任一项达标即算大V
    "bigv": {"followers": 50000, "likes": 1000, "shares": 300, "engagement": 3000},
    # 采集默认值（页面初始值 + API 未传时的兜底）
    "crawl": {"limit": 50, "strict_match": True, "span_days": 90, "douyin_detail": False,
              # 小红书搜索接口不返回正文，必须开详情页才拿得到。默认开——
              # 关掉的话 content 就是标题副本，情感判定只能看一句标题
              "xhs_detail": True,
              # 评论采集：默认关。开了很慢（每条帖子要多开一次详情页），
              # 且部分平台风控紧，所以做成勾选项而非常开
              "with_comments": False, "comment_limit": 30},
    # 报告参数：reasoning 类模型（如 agnes-2.5-flash）会先烧掉一批 token 想，
    # max_tokens 给小了会只返回思考、正文为空，所以默认给得比普通模型宽
    "report": {"max_tokens": 3600, "temperature": 0.3, "samples": 60},
    # 词云
    "wordcloud": {"top": 70, "min_count": 2},
    # 内容级去重的指纹口径。四要素全严格比对时，跨平台镜像稿会因为"正文截断长度不同、
    # 发布分钟不同"而判不出重复（实测 2899 条里只认出 1 条），所以把这几档做成可调：
    #   body_chars   参与指纹的正文字数，0 = 完全不看正文
    #   time_to_day  发布时间只比到"天"（关掉＝比到分钟，更严格）
    #   with_author  作者参与指纹（关掉＝同题不同号的转载也算重复，会显著减少条数）
    "dedupe": {"body_chars": 120, "time_to_day": True, "with_author": True},
}

_LOCK = threading.Lock()
_CACHE: dict | None = None


def _read() -> dict:
    if not config.SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(config.SETTINGS_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _merge(base: dict, over: dict) -> dict:
    out = {}
    for k, v in base.items():
        if isinstance(v, dict):
            out[k] = _merge(v, (over.get(k) or {}) if isinstance(over.get(k), dict) else {})
        else:
            nv = over.get(k, v)
            try:                      # 类型跟默认值走，转不动就用默认值
                out[k] = type(v)(nv) if not isinstance(nv, type(v)) else nv
            except (TypeError, ValueError):
                out[k] = v
    return out


def all() -> dict:
    """当前生效配置（默认值 + 用户覆盖）。"""
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            _CACHE = _merge(DEFAULTS, _read())
        return _CACHE


def get(section: str, key: str | None = None):
    cur = all().get(section, {})
    return cur if key is None else cur.get(key)


def save(patch: dict) -> dict:
    """只接受 DEFAULTS 里已声明的键，返回生效后的完整配置。"""
    global _CACHE
    clean = {}
    for section, vals in (patch or {}).items():
        if section not in DEFAULTS or not isinstance(vals, dict):
            continue
        keep = {}
        for k, v in vals.items():
            if section == "actor_weight" or k in DEFAULTS[section]:
                keep[k] = v
        if keep:
            clean[section] = keep
    with _LOCK:
        merged = _merge(DEFAULTS, {**_read(), **clean})
        # actor_weight 是"整段替换"语义，避免删了某个主体后旧值还留着
        if "actor_weight" in clean:
            merged["actor_weight"] = {**DEFAULTS["actor_weight"], **clean["actor_weight"]}
        config.SETTINGS_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
        _CACHE = merged
        return merged


def reset() -> dict:
    global _CACHE
    with _LOCK:
        if config.SETTINGS_PATH.exists():
            config.SETTINGS_PATH.unlink()
        _CACHE = _merge(DEFAULTS, {})
        return _CACHE
