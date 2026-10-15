"""重点预警：从周期内的反馈里挑出必须人工盯的内容。

入选有两条独立通道，命中任一条就进预警池：

  1. **声量大**——负面影响力超过本批高影响门槛（events.annotate_impact 算的 impact_high）。
     这类不看内容说了什么，纯看扩散量，属于"再不管就要出圈"。
  2. **内容触线**——用户在反馈里描述的广告内容涉及色情低俗、竞品投放、惊恐画面、
     诈骗博彩、涉政敏感等。这类可能声量很小，但性质比声量重要。

第 2 条的判定必须可核对，所以：
  - 全部走**显式词表**，命中就把命中的词原样回报，人能一眼验证判得对不对；
  - 不做语义推断、不做模型判断——这类定性一旦错，代价比漏判大；
  - 涉政类只标「疑似」并要求人工复核，绝不在系统里下定性结论；
  - 词表分正/负两组：命中 WORDS 且没命中 EXCLUDE 才算，减少明显的误伤
    （比如"反诈宣传""防诈骗提醒"是正面内容，不该判成诈骗广告）。

注意这里判的是**用户反馈里描述的广告内容**，不是判用户本人。数据源是公开平台的
用户吐槽，用途是广告内容安全自查。
"""
from __future__ import annotations

# (key, 名称, 一句话说明, 命中词, 排除词, 严重度 1~3)
CATEGORIES: list[tuple[str, str, str, list[str], list[str], int]] = [
    ("porn", "色情低俗",
     "反馈提到开屏/摇一摇广告推的是擦边、软色情或低俗导流内容，属于内容合规红线",
     # 只放歧义低的词。实测里"开车""污"在本场景大量误伤（"司机开车看开屏广告"
     # 会被判成色情），所以宁可漏也不留这类多义词。
     ["色情", "擦边", "低俗", "露骨", "情色", "黄色广告", "涉黄", "裸露", "软色情",
      "福利姬", "约炮", "衣着暴露", "性暗示", "打野战"],
     ["反色情", "扫黄", "举报色情", "打击"], 3),

    # 竞品这一类**不能靠关键词共现**。用户的原话：「只有反馈为什么百度网盘开屏还投放了
    # 迅雷、夸克等竞品才是竞品，不是提到百度网盘和夸克的关键词就变成竞品关系，
    # 有些人只是说想用成夸克而已」。所以词表只登记竞品名，是否算命中交给
    # _competitor_hit()：要么模型确认了投放关系（ai_competitor），要么规则匹配到
    # 「自家广告位出现竞品」的句式，且排除「我想换成夸克」这类迁移意图。
    ("competitor", "竞品投放",
     "反馈在说【百度网盘自己的广告位投放了竞品广告】，既是竞对动向也是自家渠道被借道的信号",
     ["夸克", "豆包", "文心一言", "通义", "元宝", "kimi", "Kimi", "KIMI", "deepseek",
      "DeepSeek", "即梦", "可灵", "文小言", "智谱", "讯飞星火",
      "阿里云盘", "迅雷", "115网盘", "天翼云盘", "UC浏览器", "夸克浏览器", "QQ浏览器"],
     [], 2),

    ("scare", "惊恐惊悚",
     "广告用突然出现的恐怖/血腥/尖叫音效制造惊吓，对老人和儿童风险更高",
     # 单字"鬼"会命中"鬼才""搞什么鬼"，改成词组
     ["恐怖", "惊悚", "血腥", "吓人", "吓一跳", "尖叫", "鬼片", "闹鬼", "鬼脸",
      "惊吓", "吓死", "毛骨悚然", "阴间", "心脏病都要"],
     ["恐怖片推荐", "恐怖游戏推荐"], 3),

    ("fraud", "诈骗博彩",
     "广告涉赌、涉贷、刷单、虚假中奖等诈骗话术，一旦落地页真实存在就是重大合规事故",
     # "中奖"单独出现多是用户抽奖吐槽，收紧为虚假中奖类词组
     ["诈骗", "赌博", "博彩", "赌场", "刷单", "杀猪盘", "套路贷", "网贷",
      "高利贷", "虚假中奖", "中奖诈骗", "抽奖诈骗", "杀熟", "投资理财骗", "虚假投资",
      "荷官", "菠菜", "0元购", "领现金", "免费领取现金"],
     ["反诈", "防诈骗", "反诈宣传", "举报诈骗", "打击诈骗", "识别诈骗", "警惕诈骗"], 3),

    ("politics", "涉政敏感（疑似）",
     "反馈文本里出现涉政敏感表述。**只标疑似、必须人工复核**，系统不做任何定性",
     ["反动", "颠覆", "境外势力", "邪教", "法轮", "分裂国家", "煽动", "涉政", "敏感政治"],
     [], 3),

    ("minor", "未成年人相关",
     "涉及儿童/学生被广告误导或看到不适内容，监管与舆论敏感度都高于普通体验问题",
     ["未成年", "小孩", "孩子", "儿童", "学生", "小学生", "中学生", "青少年"],
     [], 2),
]

SEVERITY = {3: "高", 2: "中", 1: "低"}
CAT_NAME = {c[0]: c[1] for c in CATEGORIES}


import re

# 自家广告位出现竞品：广告位/投放动作 在竞品名附近
_AD_SLOT = r"(?:开屏|弹窗|广告位|广告|投放|推的|插屏|banner|首页)"
# 用户想迁移到别家——这是**流失倾向**，不是竞品投放，两者价值完全不同
_CHURN = r"(?:换成|换到|改用|转投|投奔|迁移|搬到|不如用|建议用|推荐用|想用|用回|转去|弃用)"


def _competitor_hit(row: dict, names: list[str]) -> tuple[list[str], str]:
    """返回 (命中的竞品名, 依据)。没有投放关系就返回空。"""
    text = _blob(row)
    found = [w for w in names if w in text]
    if not found:
        return [], ""
    # 模型确认过投放关系，直接采信（它能读懂整句意图，比正则准）
    if row.get("ai_competitor"):
        return found[:6], "模型判定为竞品投放关系"
    for w in found:
        # 迁移意图优先排除：「太慢了我想换成夸克」不是投放
        if re.search(_CHURN + r".{0,6}" + re.escape(w), text):
            continue
        if re.search(re.escape(w) + r".{0,8}(?:更|比较|真|挺|好用|香|快)", text):
            continue        # 「夸克比百度网盘好用」是对比评价
        if re.search(_AD_SLOT + r".{0,14}" + re.escape(w), text) or \
           re.search(re.escape(w) + r".{0,10}(?:的)?" + _AD_SLOT, text):
            return [w], f"句式命中投放关系（{w} 出现在广告位描述附近）"
    return [], ""


def churn_signal(row: dict, names: list[str]) -> list[str]:
    """流失倾向：用户明说要换去别家。跟竞品投放分开统计，它是留存问题不是投放问题。"""
    text = _blob(row)
    return [w for w in names
            if w in text and re.search(_CHURN + r".{0,6}" + re.escape(w), text)]


def _blob(row: dict) -> str:
    return f"{row.get('title') or ''} {row.get('content') or ''}"


def scan(row: dict) -> list[dict]:
    """单条内容命中哪些类别，回报命中的词，便于人工核对。"""
    text = _blob(row)
    hits = []
    for key, name, why, words, excl, sev in CATEGORIES:
        if key == "competitor":
            got, basis = _competitor_hit(row, words)
            if got:
                hits.append({"key": key, "name": name, "why": why,
                             "words": got[:6], "severity": sev, "basis": basis})
            continue
        got = [w for w in words if w in text]
        if not got:
            continue
        if any(x in text for x in excl):
            continue          # 明显是"反诈宣传"这类正面内容，不算触线
        hits.append({"key": key, "name": name, "why": why,
                     "words": got[:6], "severity": sev})
    return hits


def summarize(rows: list[dict], top: int = 30) -> dict:
    """预警池汇总。rows 需要先跑过 events.annotate_impact（要用 impact / impact_high）。"""
    pool: list[dict] = []
    by_cat: dict[str, dict] = {}
    for r in rows:
        cats = scan(r)
        big = bool(r.get("impact_high"))
        if not cats and not big:
            continue
        sev = max([c["severity"] for c in cats], default=0)
        # 排序权重：触线的严重度优先，其次影响力。声量大但没触线的排在触线内容之后，
        # 因为"性质问题"比"量大"更需要先处理
        rank = sev * 10_000_000 + (r.get("impact") or 0)
        item = {
            "platform": r.get("platform"), "platform_label": r.get("platform_label"),
            "post_id": r.get("post_id"), "title": r.get("title") or "",
            "url": r.get("url") or "", "author": r.get("author") or "",
            "publish_time": (r.get("publish_time") or "")[:16],
            "engagement": r.get("engagement") or 0,
            "impact": r.get("impact") or 0, "big": big,
            "actor": r.get("actor") or "", "sentiment": r.get("sentiment") or "",
            "keyword": r.get("keyword") or "",
            "cats": cats, "severity": sev, "rank": rank,
            # 入选理由写清楚，别让人猜为什么这条被挑出来
            "reason": "｜".join(
                [f'{c["name"]}（{"、".join(c["words"])}）' for c in cats]
                + (["声量大：负面影响力超过本批高影响门槛"] if big else [])),
        }
        pool.append(item)
        for c in cats:
            g = by_cat.setdefault(c["key"], {
                "key": c["key"], "name": c["name"], "why": c["why"],
                "severity": c["severity"], "posts": 0, "impact": 0.0,
                "engagement": 0, "words": {}, "plats": {},
            })
            g["posts"] += 1
            g["impact"] += r.get("impact") or 0
            g["engagement"] += r.get("engagement") or 0
            for w in c["words"]:
                g["words"][w] = g["words"].get(w, 0) + 1
            pl = r.get("platform_label") or r.get("platform")
            g["plats"][pl] = g["plats"].get(pl, 0) + 1

    big_only = [x for x in pool if x["big"] and not x["cats"]]
    cats_out = []
    for g in by_cat.values():
        cats_out.append({
            "key": g["key"], "name": g["name"], "why": g["why"],
            "severity": g["severity"], "sev_text": SEVERITY[g["severity"]],
            "posts": g["posts"], "impact": round(g["impact"], 1),
            "engagement": g["engagement"],
            "words": sorted(g["words"].items(), key=lambda x: -x[1])[:8],
            "plats": sorted(g["plats"].items(), key=lambda x: -x[1])[:6],
        })
    # 先按严重度、再按条数排，让"高危但量小"的类别不会被"量大但中危"的压下去
    cats_out.sort(key=lambda c: (-c["severity"], -c["posts"]))
    pool.sort(key=lambda x: -x["rank"])
    return {
        "total": len(pool),
        "risky": len(pool) - len(big_only),
        "big_only": len(big_only),
        "cats": cats_out,
        "items": pool[:top],
        "note": ("入池两条通道：① 内容触线（色情/竞品/惊恐/诈骗/涉政/未成年，"
                 "全部按显式词表判定，命中词原样列出可核对）；② 声量大（负面影响力超过本批高影响门槛）。"
                 "涉政类只标「疑似」，必须人工复核后再定性。"),
    }
