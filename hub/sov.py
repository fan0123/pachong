"""声量测算：把"抓到的互动数"换算成可跨平台相加的声量分。

为什么不能直接用 engagement 相加（库里的实测数据就是理由）：
  微博   有效 422 条，互动量中位数 40
  小红书 有效 200 条，中位数 48
  头条号 有效 130 条，互动量中位数只有 2（网页镜像口径），但阅读数中位数 28
  百家号 221 条、微信公众号 177 条，网页侧**完全没有互动数**，全是 0
直接相加的话，35% 的内容（微信+百家号）声量为 0 —— 而"人民日报公众号发了一篇批评稿"
显然不是零声量。所以要做三件事：换算、插补、标注。

模型（四步，每步都可复算、可审计）：

  1) 取观测量 raw_i：优先用最接近"被多少人看到"的字段。
     微博/小红书 → engagement（赞+2评+3转+2藏）；头条号 → 阅读数 views（它的互动数是镜像
     口径，普遍个位数，用阅读更接近真实到达）；微信/百家号 → 无，进第 2 步插补。
  2) 平台内归一 heat_i = raw_i / B_p。B_p 是**从当前数据里现算的平台中位数**，不是写死的
     系数——这样"一条普通微博"和"一条普通小红书"都等于 1.0，量级差异被抹平。
  3) 缺失插补：无互动数平台按"同主体在有数平台的 heat 中位数"补，并标 basis=imputed。
     不给 0（等于抹掉声量），也不按最高值补（等于虚高），且插补比例要在报表里露出来。
  4) 单条声量 sov_i = (1 + log10(1 + heat_i)) × A_actor。
     log10 压长尾：热度 10 倍只算 ~1.6 倍、100 倍算 ~2.4 倍，避免一条爆款把整段时间的
     声量带偏；常数 1 是"内容存在本身"的底分，所以零互动的内容仍计 1 分。
     A_actor 是主体权重，补的是**我们抓不到的跨渠道扩散**（官媒/监管发声会被大量转载、
     进搜索、上电视），不是拍脑袋加成——它单独乘、单独可调，报表里也单独展示。

不做的事：不给平台再设一层"平台影响力系数"。那个数没法从手里的数据校准，写多少都是主观的，
宁可让 A_actor 一个可解释的旋钮承担主观部分。
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict

# 主体权重：官媒/监管的内容会被跨渠道转载放大，这部分在我们抓的 7 个平台里看不到
ACTOR_WEIGHT = {
    "官方机构": 2.0,      # 网信办/工信部点名，等级最高
    "官方媒体": 1.8,      # 央媒党媒，议程设置能力强
    "新闻媒体": 1.4,      # 持证媒体，可被转载/进搜索
    "大V博主": 1.3,       # 粉丝盘子带来的二次扩散
    "平台企业号": 1.0,
    "自媒体号": 1.0,
    "普通用户": 1.0,      # 基准
    "未判定": 1.0,
}

# 各平台用哪个字段当观测量；'none' 表示网页侧拿不到，走插补
RAW_FIELD = {
    "weibo": "engagement", "xhs": "engagement",
    "douyin": "engagement", "kuaishou": "engagement",
    "toutiao": "views",              # 互动数是镜像口径，阅读数更接近真实到达
    "weixin": "none", "baijiahao": "none",
}
MIN_GROUP = 5          # 分组样本少于这个数就不用它的中位数，退到全局


def _weights() -> dict:
    """主体权重取可配置值（页面「参数配置」可改），默认同 ACTOR_WEIGHT。"""
    from . import settings
    w = dict(ACTOR_WEIGHT)
    w.update(settings.get("actor_weight") or {})
    return w


def _raw(row: dict) -> tuple[float, str]:
    """返回 (观测量, 取值来源)。来源写清楚，报表里能追。"""
    field = RAW_FIELD.get(row.get("platform"), "engagement")
    if field == "none":
        return 0.0, "none"
    val = row.get(field) or 0
    if field == "views" and not val:
        # 头条号偶尔没阅读数，退回互动量（量级偏小，但比 0 好）
        return float(row.get("engagement") or 0), "engagement"
    return float(val), field


def calibrate(rows: list[dict]) -> dict:
    """算平台基线 B_p 与主体 heat 中位数。全部来自传入数据，换个时间段会自动重算。"""
    raws: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        val, src = _raw(r)
        if src != "none":
            raws[r.get("platform")].append(val)
    base = {}
    for plat, vals in raws.items():
        pos = [v for v in vals if v > 0]
        # 用"有观测值的中位数"当基线：一半内容是 0 时中位数会变 0，基线就没意义了
        med = statistics.median(pos) if len(pos) >= MIN_GROUP else (
            statistics.median(vals) if vals else 0)
        base[plat] = max(med, 1.0)

    # 同主体在"有观测量平台"上的 heat 中位数，供无互动数平台插补
    heats: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        val, src = _raw(r)
        if src == "none":
            continue
        b = base.get(r.get("platform")) or 1.0
        heats[r.get("actor") or "未判定"].append(val / b)
    actor_heat = {}
    for actor, vals in heats.items():
        if len(vals) >= MIN_GROUP:
            actor_heat[actor] = round(statistics.median(vals), 4)
    all_heat = [v for vals in heats.values() for v in vals]
    fallback = round(statistics.median(all_heat), 4) if all_heat else 1.0
    return {"platform_base": {k: round(v, 2) for k, v in base.items()},
            "actor_heat": actor_heat, "fallback_heat": fallback,
            "actor_weight": _weights()}


def annotate(rows: list[dict], cal: dict | None = None) -> dict:
    """给每条补 heat / sov / sov_basis，返回校准参数（调用方通常还要 summarize）。"""
    cal = cal or calibrate(rows)
    base, actor_heat = cal["platform_base"], cal["actor_heat"]
    weights = _weights()
    for r in rows:
        actor = r.get("actor") or "未判定"
        val, src = _raw(r)
        if src == "none":
            heat = actor_heat.get(actor, cal["fallback_heat"])
            basis = "imputed"
        else:
            heat = val / (base.get(r.get("platform")) or 1.0)
            basis = "measured"
        weight = weights.get(actor, 1.0)
        r["heat"] = round(heat, 4)
        r["sov_basis"] = basis
        r["sov_raw_field"] = src
        r["sov_base"] = round(1 + math.log10(1 + max(heat, 0)), 4)
        r["sov"] = round(r["sov_base"] * weight, 4)
    return cal


def summarize(rows: list[dict], cal: dict | None = None) -> dict:
    """汇总声量：总量、负面量、按平台/主体/按日，以及置信度（实测占比）。

    负面声量单独出：主口径要的是"负反馈声量"，中性和正面不该混进同一个数。
    """
    cal = annotate(rows, cal)
    total = sum(r["sov"] for r in rows)
    neg = sum(r["sov"] for r in rows if r.get("sentiment") == "负面")
    measured = sum(1 for r in rows if r["sov_basis"] == "measured")
    by_plat: dict[str, dict] = {}
    by_actor: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    for r in rows:
        label = r.get("platform_label") or r.get("platform")
        a = by_plat.setdefault(label, {"name": label, "sov": 0.0, "neg_sov": 0.0,
                                       "posts": 0, "measured": 0})
        a["sov"] += r["sov"]
        a["posts"] += 1
        a["measured"] += 1 if r["sov_basis"] == "measured" else 0
        if r.get("sentiment") == "负面":
            a["neg_sov"] += r["sov"]
        b = by_actor.setdefault(r.get("actor") or "未判定",
                                {"name": r.get("actor") or "未判定", "sov": 0.0, "posts": 0})
        b["sov"] += r["sov"]
        b["posts"] += 1
        day = (r.get("publish_time") or "")[:10]
        if len(day) == 10:
            d = by_day.setdefault(day, {"date": day, "sov": 0.0, "neg_sov": 0.0})
            d["sov"] += r["sov"]
            if r.get("sentiment") == "负面":
                d["neg_sov"] += r["sov"]
    for d in (*by_plat.values(), *by_actor.values(), *by_day.values()):
        for k in ("sov", "neg_sov"):
            if k in d:
                d[k] = round(d[k], 1)
    days = len(by_day) or 1
    return {
        "total_sov": round(total, 1),
        "neg_sov": round(neg, 1),
        "neg_share": round(neg / total * 100, 1) if total else 0,
        "daily_avg": round(total / days, 1),
        "posts": len(rows),
        "measured": measured,
        "imputed": len(rows) - measured,
        "measured_ratio": round(measured / len(rows) * 100) if rows else 0,
        "by_platform": sorted(by_plat.values(), key=lambda x: -x["sov"]),
        "by_actor": sorted(by_actor.values(), key=lambda x: -x["sov"]),
        "trend": [by_day[k] for k in sorted(by_day)],
        "calibration": cal,
    }


# 报表/前端要展示的口径说明，集中放这里，避免各处各写一套解释
FORMULA_NOTE = (
    "单条声量 = (1 + log10(1 + 热度)) × 主体权重；"
    "热度 = 该条观测量 ÷ 同平台观测量中位数（微博/小红书取互动量，头条号取阅读数）；"
    "微信公众号、百家号网页侧无互动数，按「同主体在有数平台的热度中位数」插补并标记为估算；"
    "log10 用于压缩爆款长尾，常数 1 是内容存在本身的底分，因此零互动内容仍计 1 分。"
)
