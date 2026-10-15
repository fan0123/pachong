"""负面影响力打分 + 事件归因。

两件事，都刻意做成"可核对"的：

1. **负面影响力**（impact）——回答"哪条帖子造成的负面影响大"。
   公式：`真实互动量 × 主体权重`，只对情感=负面的内容计分。
   用真实互动量而不是 log 压缩后的声量分，是因为这里要的正是"扩散绝对量"；
   主体权重复用 sov 里那套（官媒/政务的一条顶普通用户几条，因为会被跨渠道转载）。
   高影响判定 = 同时超过「绝对阈值」和「本批 P90」，避免小样本里把第 1 名说成"高影响"。

2. **事件归因**——回答"是什么事件引发的舆论"。
   用**证据词表**把负面内容归到具体触发场景（报警被广告耽误、摇一摇误触、关不掉…），
   每个事件都带命中的证据词、引爆帖、起始日、参与平台。
   归不进任何场景的，退化成"按标题共现短语聚类"，并明确标注是自动聚类而非已知场景。
   不做任何"看起来像事件"的编造：事件名要么来自词表，要么来自数据里真实出现的短语。
"""
from __future__ import annotations

import re
from collections import Counter

from . import sov

# 高影响的绝对门槛：低于这个量级谈"重大负面影响"没意义
IMPACT_FLOOR = 3000


# ---------------------------------------------------------------- 事件词表
# 每个场景：(事件名, 归因说明, 证据词)。证据词命中越多，归属置信度越高。
SCENES: list[tuple[str, str, list[str]]] = [
    ("应急场景被广告阻断",
     "用户在报警/急救/导航等紧急操作时被开屏广告拦住，安全属性让议题迅速升级，是本轮最容易被官媒接力的触发点",
     ["报警", "119", "120", "急救", "火警", "求救", "救命", "导航", "打车", "医院",
      "挂号", "地震预警"]),
    ("摇一摇误触跳转",
     "手机轻微晃动就跳转到第三方 App/落地页，用户认为这是把「误触」当成「点击」，属于交互欺骗类抱怨",
     # 注意：不要把「摇一摇」「开屏广告」这类**搜索词本身**当证据词——
     # 它们在每条数据里都出现，会把整批负面都吸进这一个事件（实测会吸走 348/307 条）
     ["误触", "一摇就", "晃一下", "晃动", "轻轻一摇", "乱跳", "自动跳转", "跳到别的",
      "莫名跳转", "手滑"]),
    ("关不掉 / 关闭按钮难找",
     "关闭按钮过小、倒计时不可跳过、点了关闭还是进落地页，指向「一键关闭」合规要求没有落地",
     ["关不掉", "关不了", "无法关闭", "跳过", "倒计时", "关闭按钮", "叉太小", "找不到关闭",
      "强制观看", "必须看完"]),
    ("监管介入与整治通报",
     "监管部门通报、约谈或专项整治，以及媒体对整治进展的报道——这类内容会把分散的用户抱怨聚成公共议题",
     ["工信部", "网信办", "市场监管", "消协", "通报", "约谈", "整治", "专项行动", "立案",
      "处罚", "点名", "新规", "国家标准"]),
    ("适老化 / 老人受影响",
     "老年用户被广告误导或无法关闭，涉及适老化改造要求，舆论敏感度高于普通体验抱怨",
     ["老人", "老年", "父母", "爷爷", "奶奶", "适老", "长辈"]),
    ("隐私与权限质疑",
     "怀疑广告通过传感器/剪贴板/后台权限采集数据，质疑从「烦人」升级为「不安全」",
     ["隐私", "监听", "偷听", "权限", "传感器", "剪贴板", "个人信息", "偷偷", "上传数据"]),
    ("投诉与维权行动",
     "用户走投诉/举报/诉讼等正式渠道，说明抱怨已经沉淀成可追溯的工单与法律动作",
     ["投诉", "举报", "12315", "黑猫", "起诉", "诉讼", "维权", "工单", "赔偿"]),
    ("屏蔽工具与自助规避",
     "用户互相推荐跳广告工具/系统设置来自救，反向说明官方渠道没解决问题",
     ["李跳跳", "屏蔽", "拦截", "去广告", "跳广告", "root", "magisk", "冰箱", "关闭个性化"]),
]

# 自动聚类时用的停用词：这些词到处都有，拿来当事件名没有信息量
CLUSTER_STOP = set("""广告 开屏 摇一摇 手机 app APP 应用 软件 视频 现在 这个 那个 什么 怎么
为什么 真的 已经 还是 就是 可以 不能 大家 有没有 一个 我们 你们 他们 自己 今天 最近""".split())
CJK_ONLY = re.compile(r"[\u4e00-\u9fa5]+")
# 词云那套停用词直接拿来做"切片首尾不能是虚词"的边界判断，别再维护第二份表
from .wordcloud import STOP as _WC_STOP  # noqa: E402


def _blob(row: dict) -> str:
    return f"{row.get('title') or ''} {row.get('content') or ''}"


def impact_of(row: dict, weights: dict) -> float:
    """单帖负面影响力。非负面内容一律 0——这个指标只用来排负面。"""
    if (row.get("eff_sentiment") or row.get("sentiment") or "") != "负面":
        return 0.0
    w = weights.get(row.get("actor") or "普通用户", 1.0)
    return round((row.get("engagement") or 0) * w, 1)


def annotate_impact(rows: list[dict]) -> dict:
    """给每行写 impact / impact_high，返回门槛说明，供 UI 与报告标注依据。"""
    weights = sov._weights()
    for r in rows:
        r["impact"] = impact_of(r, weights)
    negs = [r for r in rows
            if (r.get("eff_sentiment") or r.get("sentiment") or "") == "负面"]
    scores = sorted((r["impact"] for r in negs if r["impact"] > 0), reverse=True)
    if not scores:
        for r in rows:
            r["impact_high"] = False
        return {"threshold": IMPACT_FLOOR, "p90": 0, "high": 0,
                "negatives": len(negs), "measurable": 0,
                "note": "这批负面内容都来自不公开互动数的平台，无法算影响力排序"}
    # P90：第 10% 分位点（列表已降序，取前 10% 的最后一个）
    p90 = scores[max(0, int(len(scores) * 0.1) - 1)]
    threshold = max(IMPACT_FLOOR, p90)
    high = 0
    for r in rows:
        r["impact_high"] = r["impact"] >= threshold and r["impact"] > 0
        high += 1 if r.get("impact_high") else 0
    return {"threshold": round(threshold, 1), "p90": round(p90, 1),
            "high": high, "negatives": len(negs), "measurable": len(scores),
            "note": f"影响力 = 真实互动量 × 主体权重（只算负面）；高影响门槛 = "
                    f"max(绝对门槛 {IMPACT_FLOOR}, 本批 P90 {round(p90, 1)}) = {round(threshold, 1)}；"
                    f"负面 {len(negs)} 条，其中 {len(scores)} 条平台公开互动数、可参与排序"}


def _match_scene(row: dict) -> tuple[str, str, list[str]] | None:
    text = _blob(row)
    best = None
    for name, why, words in SCENES:
        hits = [w for w in words if w in text]
        if not hits:
            continue
        # 命中词多的场景优先；同样多时按词表顺序（越靠前的场景越"重"）
        if best is None or len(hits) > len(best[2]):
            best = (name, why, hits)
    return best


def _auto_cluster(rows: list[dict], background: set[str],
                  min_size: int = 3) -> list[tuple[str, list[dict]]]:
    """归不进已知场景的负面内容，按标题里真实共现的 2-4 字短语聚一下。

    background 是这批数据的搜索词（如「开屏广告」「摇一摇广告」）。
    搜索词及其片段必须排除：它们在每条里都出现，聚出来的簇名等于没说话。
    """
    grams: Counter = Counter()
    owner: dict[str, list[dict]] = {}
    # 背景词要按"片段"屏蔽：只查 `gram in 背景词` 会漏掉「广告的」这种跨界切片
    banned = {w for w in CLUSTER_STOP if len(w) >= 2}
    for bg in background:
        banned.add(bg)
        for n in (2, 3):
            for i in range(len(bg) - n + 1):
                banned.add(bg[i:i + n])
    for r in rows:
        title = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", " ", r.get("title") or "")
        seen = set()
        for seg in title.split():
            for n in (4, 3, 2):
                for i in range(len(seg) - n + 1):
                    g = seg[i:i + n]
                    # 只用纯中文短语当簇名：英文/数字切出来的片段（#APP → "PP"）没有含义
                    if not CJK_ONLY.fullmatch(g):
                        continue
                    if g in CLUSTER_STOP or g in seen or len(g) < 2:
                        continue
                    if any(b in g for b in banned):
                        continue
                    # 首尾是虚词的切片不是词组（「告的」「的手机」），复用词云那套停用词做边界判断
                    if g[0] in _WC_STOP or g[-1] in _WC_STOP:
                        continue
                    seen.add(g)
                    grams[g] += 1
                    owner.setdefault(g, []).append(r)
    out = []
    used: set[int] = set()
    for gram, cnt in grams.most_common(40):
        if cnt < min_size:
            break
        members = [r for r in owner[gram] if id(r) not in used]
        if len(members) < min_size:
            continue
        for r in members:
            used.add(id(r))
        out.append((gram, members))
        if len(out) >= 6:
            break
    return out


def attribute(rows: list[dict], top: int = 8) -> list[dict]:
    """把负面内容聚成事件并归因。rows 需要先跑过 annotate_impact。"""
    # 用**生效情感**：模型把中性纠成负面的那 768 条，如果这里还读规则列 sentiment，
    # 就进不了事件归因，事件的条数和影响力会明显偏小。
    negatives = [r for r in rows
                 if (r.get("eff_sentiment") or r.get("sentiment") or "") == "负面"]
    if not negatives:
        return []
    # 搜索词当背景词剔掉，避免自动聚类聚出「开屏广告」这种废话簇名
    background = {(r.get("keyword") or "").strip() for r in rows} - {""}
    buckets: dict[str, dict] = {}
    rest = []
    for r in negatives:
        hit = _match_scene(r)
        if not hit:
            rest.append(r)
            continue
        name, why, words = hit
        b = buckets.setdefault(name, {"name": name, "why": why, "kind": "已知场景",
                                      "rows": [], "evidence": Counter()})
        b["rows"].append(r)
        b["evidence"].update(words)
    for gram, members in _auto_cluster(rest, background):
        buckets[f"「{gram}」相关讨论"] = {
            "name": f"「{gram}」相关讨论", "kind": "自动聚类",
            "why": f"这批内容标题里都出现了「{gram}」，未落进已知触发场景，"
                   f"按共现短语自动聚成一簇，建议人工确认是否为同一事件",
            "rows": members, "evidence": Counter({gram: len(members)})}

    events = []
    for b in buckets.values():
        rs = sorted(b["rows"], key=lambda r: -(r.get("impact") or 0))
        days = sorted({(r.get("publish_time") or "")[:10] for r in rs} - {""})
        plats = Counter((r.get("platform_label") or r.get("platform")) for r in rs)
        lead = rs[0]
        events.append({
            "name": b["name"], "kind": b["kind"], "why": b["why"],
            "posts": len(rs),
            "impact": round(sum(r.get("impact") or 0 for r in rs), 1),
            "engagement": sum(r.get("engagement") or 0 for r in rs),
            "high_posts": sum(1 for r in rs if r.get("impact_high")),
            "first_day": days[0] if days else "", "last_day": days[-1] if days else "",
            "platforms": [{"name": k, "posts": v} for k, v in plats.most_common()],
            "evidence": [w for w, _ in b["evidence"].most_common(6)],
            "lead": {"title": lead.get("title") or "", "url": lead.get("url") or "",
                     "platform": lead.get("platform_label") or lead.get("platform"),
                     "author": lead.get("author") or "",
                     "day": (lead.get("publish_time") or "")[:10],
                     "engagement": lead.get("engagement") or 0,
                     "impact": lead.get("impact") or 0},
        })
    events.sort(key=lambda e: -e["impact"])
    return events[:top]


def hot_negative(rows: list[dict], top: int = 12) -> list[dict]:
    """负面影响力榜：给看板"重点关注"和报告用。"""
    negs = [r for r in rows
            if (r.get("eff_sentiment") or r.get("sentiment") or "") == "负面"]
    negs.sort(key=lambda r: -(r.get("impact") or 0))
    out = []
    for r in negs[:top]:
        out.append({
            "platform": r.get("platform"), "platform_label": r.get("platform_label"),
            "post_id": r.get("post_id"), "title": r.get("title") or "",
            "url": r.get("url") or "", "author": r.get("author") or "",
            "publish_time": r.get("publish_time") or "",
            "engagement": r.get("engagement") or 0,
            "impact": r.get("impact") or 0, "high": bool(r.get("impact_high")),
            "actor": r.get("actor") or "", "layer": r.get("layer") or "",
            "reason": r.get("sentiment_reason") or "",
            "scene": (_match_scene(r) or ("未归类", "", []))[0],
        })
    return out

def spikes(rows: list[dict], trend: list[dict], top: int = 6) -> list[dict]:
    """找出声量峰值日，并说清那天是什么内容把量顶上去的。

    看板上原来只有一条趋势曲线，用户看到一个尖峰只能自己去明细里翻那天发生了什么。
    这里把「峰值日 → 当天最高影响力的帖 → 它归属的事件」串起来，直接标在图上。

    峰值判定用「均值 + 1σ」而不是简单取 TopN：数据平缓时不该硬造出峰值，
    真有爆点时也不该因为名额只有 N 个而漏掉。
    """
    if not trend:
        return []
    daily = {}
    for r in trend:
        n = sum(v for k, v in r.items() if k != "date")
        daily[r["date"]] = n
    vals = list(daily.values())
    if not vals:
        return []
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    sd = var ** 0.5
    threshold = mean + sd            # 高于「均值+1σ」才算峰
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        day = (r.get("publish_time") or "")[:10]
        if len(day) == 10:
            by_day.setdefault(day, []).append(r)
    scene_of = {}
    for e in attribute(rows, top=12):
        for d in (e.get("first_day"), e.get("last_day")):
            pass
        scene_of[e["name"]] = e
    out = []
    for day, n in sorted(daily.items(), key=lambda x: -x[1]):
        if n < threshold or len(out) >= top:
            continue
        cands = by_day.get(day) or []
        if not cands:
            continue
        lead = max(cands, key=lambda r: r.get("impact") or r.get("engagement") or 0)
        scene = _match_scene(lead)
        out.append({
            "date": day, "posts": n,
            "ratio": round(n / mean, 1) if mean else 0,
            "scene": scene[0] if scene else "未归类",
            "title": (lead.get("title") or "")[:52],
            "platform": lead.get("platform_label") or "",
            "author": lead.get("author") or "匿名",
            "engagement": lead.get("engagement") or 0,
            "impact": round(lead.get("impact") or 0, 1),
            "url": lead.get("url") or "",
        })
    out.sort(key=lambda x: x["date"])
    return out
