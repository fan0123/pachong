"""周期对比 + 采集覆盖台账。

两件事绑在一起做，因为它们回答的是同一个问题的两面：

  1. **周期对比**（周/月/自定义天数）：同一关键词在相邻周期的条数、声量、互动量怎么变。
  2. **覆盖台账**：这个关键词到底"抓过哪些天"。

为什么必须有台账：看板上"某周只有 3 条"有两种完全不同的含义——
真的只有 3 条，还是那周根本没抓过。不把这两者分开，周期对比就是在拿"没抓"当"没声量"，
结论直接是错的。所以每个周期都带 coverage 状态（full / partial / none）和缺失天数，
前端对 partial/none 的周期置灰、不让选进对比。

覆盖判定的口径（刻意写在这里，避免以后误会）：
  - 只认**成功执行过**的 run：note 里带 need_login / error: 的不算，因为它压根没抓
  - 覆盖是按 **(平台 × 关键词 × 天)** 记的：增量采集要回答的是"这个渠道这几天抓过没有"，
    只按关键词记会把"微博抓过"当成"十个平台都抓过"，于是永远补不上缺的渠道
  - 关键词级视图（周期对比用）是各平台覆盖的并集，同时回报当天覆盖到几个平台
  - 老记录没有 since_day/until_day 列，退化成解析 note 开头的 "起~止" 片段
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from . import store

WINDOW_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})")
GRAINS = {"week": 7, "month": 0, "day": 1}      # month 走自然月，不是固定天数
FILL_HOLE = 3          # 推断覆盖时，被两侧夹住且不超过这么多天的空洞按已覆盖处理
MIN_BASE = 5           # 环比的最小基数：上期低于这个数就不给百分比（避免 +7800% 这种噪音）


def _d(s: str) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _run_window(r: dict) -> tuple[date, date] | None:
    a, b = _d(r.get("since_day")), _d(r.get("until_day"))
    if not (a and b):
        m = WINDOW_RE.search(r.get("note") or "")
        if not m:
            return None                 # 没写时间窗的老记录，不敢当成覆盖
        a, b = _d(m.group(1)), _d(m.group(2))
    if not (a and b) or b < a:
        return None
    return a, b


def _failed(r: dict) -> bool:
    note = r.get("note") or ""
    return note.startswith("error") or "need_login" in note


def run_days(keyword: str = "", platform: str = "") -> dict[str, set]:
    """确证覆盖：{日期: {平台…}}，只来自有时间窗记录的成功 run。"""
    with store.conn() as c:
        c.row_factory = sqlite3.Row
        sql = "SELECT keyword, platform, since_day, until_day, note FROM runs WHERE 1=1"
        args: list = []
        if keyword:
            sql += " AND keyword = ?"
            args.append(keyword)
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        runs = [dict(r) for r in c.execute(sql, args)]
    days: dict[str, set] = {}
    for r in runs:
        if _failed(r):
            continue
        win = _run_window(r)
        if not win:
            continue
        a, b = win
        if (b - a).days > 400:          # 明显是"全量兜底"窗口，不当成逐日覆盖凭据
            continue
        d = a
        while d <= b:
            days.setdefault(d.isoformat(), set()).add(r.get("platform"))
            d += timedelta(days=1)
    return days


def data_days(keyword: str = "", platform: str = "") -> dict[str, set]:
    """推断覆盖：{日期: {平台…}}，按"库里这天有这个平台的数据"推断当天抓过。

    给早期 CLI 采集兜底——那批 run 没记时间窗，但库里躺着帖子，说明那些天确实抓过。
    局限必须说清：**只能证明"有数据的那天抓过"**，某天真的一条都没有时
    无法区分"没抓"还是"没人发"，所以要配合 FILL_HOLE 补洞一起用。
    """
    with store.conn() as c:
        sql = ("SELECT substr(publish_time,1,10) AS d, platform FROM posts"
               " WHERE publish_time != '' AND COALESCE(deleted,0)=0 AND COALESCE(dup_of,'')=''")
        args: list = []
        if keyword:
            sql += " AND keyword = ?"
            args.append(keyword)
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        sql += " GROUP BY d, platform"
        days: dict[str, set] = {}
        for d, plat in c.execute(sql, args):
            if d and len(d) == 10:
                days.setdefault(d, set()).add(plat)
        return days


def _fill_holes(cov: dict[str, set], src: dict[str, str]) -> None:
    """把被两侧覆盖日夹住的短空洞按已覆盖处理，标记来源 fill。原地改。"""
    if not cov:
        return
    have = sorted(cov)
    d, b = _d(have[0]), _d(have[-1])
    hole: list[str] = []
    while d and b and d <= b:
        k = d.isoformat()
        if k in cov:
            if hole and len(hole) <= FILL_HOLE:
                for h in hole:
                    cov[h] = set()
                    src[h] = "fill"
            hole = []
        else:
            hole.append(k)
        d += timedelta(days=1)


def merged_coverage(keyword: str = "", platform: str = "",
                    strict: bool = False) -> tuple[dict[str, int], dict[str, str]]:
    """确证 + 推断覆盖合并：({日期: 覆盖平台数}, {日期: 来源 run/data/fill})。

    给了 platform 就是"这个渠道"的覆盖，用于增量采集；不给就是各渠道的并集，用于周期对比。

    strict=True 只认采集流水（run），不做数据推断。**增量采集默认走 strict**，原因是：
    采集有「每词上限」，限流会让结果集中在少数高互动的天上——weibo × 开屏广告 实测 383 条
    只落在 17 个不同日期上。拿这种数据反推"这天抓过"会把大量实际没抓透的天算成已覆盖，
    补缺口就补不到。展示侧（周期对比）反过来用宽松口径，否则整个区间几乎全是灰的。
    """
    runs = run_days(keyword, platform)
    cov: dict[str, set] = {k: set(v) for k, v in runs.items()}
    src = {d: "run" for d in cov}
    if not strict:
        for d, plats in data_days(keyword, platform).items():
            if d in cov:
                cov[d] |= plats
            else:
                cov[d] = set(plats)
                src[d] = "data"
        _fill_holes(cov, src)
    return {k: len(v) for k, v in cov.items()}, src


def periods(since: str, until: str, grain: str = "week") -> list[dict]:
    """把区间切成周期。week＝按自然周（周一起），month＝按自然月，数字＝固定天数。"""
    a, b = _d(since), _d(until)
    if not (a and b) or b < a:
        return []
    out = []
    if grain == "month":
        cur = a.replace(day=1)
        while cur <= b:
            nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
            s, e = max(cur, a), min(nxt - timedelta(days=1), b)
            out.append({"label": f"{cur:%Y-%m}", "since": s.isoformat(), "until": e.isoformat()})
            cur = nxt
    else:
        span = 7 if grain == "week" else max(1, int(grain))
        if grain == "week":
            cur = a - timedelta(days=a.weekday())    # 对齐到周一，周对比才有可比性
        else:
            cur = a
        while cur <= b:
            e = cur + timedelta(days=span - 1)
            s, ee = max(cur, a), min(e, b)
            label = (f"{s:%m-%d}~{ee:%m-%d}" if grain != "week"
                     else f"{cur:%G}-W{cur:%V}")
            out.append({"label": label, "since": s.isoformat(), "until": ee.isoformat(),
                        "full_since": cur.isoformat(), "full_until": e.isoformat()})
            cur = e + timedelta(days=1)
    return out


def _days(a: str, b: str) -> list[str]:
    d, e, out = _d(a), _d(b), []
    while d and e and d <= e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def all_keywords() -> list[str]:
    with store.conn() as c:
        return sorted(r[0] for r in c.execute(
            "SELECT DISTINCT keyword FROM posts WHERE keyword != ''"))


def scope_coverage(keyword: str = "", strict: bool = False) -> tuple[dict[str, int], dict[str, str], list[str]]:
    """对比口径下的覆盖：指定了关键词就是它自己的覆盖；没指定就是**所有关键词的交集**。

    为什么是交集而不是并集：看"全部关键词"的总量时，中途新增一个关键词会让总条数凭空上跳。
    那种上涨是"多抓了一个词"而不是"舆论变多了"。取交集＝只有每个词都抓过的日子才算可比，
    这样周期对比不会把采集口径的变化读成声量变化。第三个返回值是"卡住覆盖的关键词"，
    UI 上要指名道姓，否则用户不知道该去补哪个词。
    """
    if keyword:
        cov, src = merged_coverage(keyword, "", strict)
        return cov, src, []
    kws = all_keywords()
    if not kws:
        return {}, {}, []
    per = {k: merged_coverage(k, "", strict) for k in kws}
    common = set.intersection(*[set(c) for c, _ in per.values()]) if per else set()
    cov: dict[str, int] = {}
    src: dict[str, str] = {}
    for d in common:
        cov[d] = max(per[k][0].get(d, 0) for k in kws)
        srcs = {per[k][1].get(d) for k in kws}
        src[d] = "run" if srcs == {"run"} else ("data" if srcs == {"data"} else "mixed")
    # 谁把覆盖卡住了：覆盖天数最少的几个词
    limit_by = sorted(kws, key=lambda k: len(per[k][0]))[:3]
    return cov, src, limit_by


def compare(since: str, until: str, grain: str = "week", keyword: str = "",
            layers: list[str] | None = None) -> dict:
    """逐周期算条数/声量/互动量，并标出每个周期的采集覆盖情况。"""
    from . import sov

    cov, src, limit_by = scope_coverage(keyword)
    rows = store.query(keyword=keyword, since=since, until=until, layers=layers)
    valid = store.query(keyword=keyword, since=since, until=until,
                        layers=["A_UGC负反馈", "B_媒体负面报道", "C_中性事件报道", "E_正面反馈"])
    # 声量的平台基线要在"整个对比区间"上标定一次，再分摊到各周期，
    # 否则每个周期各自标定，周期之间的分数不可比（这是跨周期对比最容易踩的坑）
    cal = sov.calibrate(valid)
    sov.annotate(valid, cal)
    by_day_sov: dict[str, dict] = {}
    for r in valid:
        day = (r.get("publish_time") or "")[:10]
        if len(day) != 10:
            continue
        s = by_day_sov.setdefault(day, {"sov": 0.0, "neg": 0.0})
        s["sov"] += r.get("sov") or 0
        if (r.get("sentiment") or "") == "负面":
            s["neg"] += r.get("sov") or 0
    by_day_cnt: dict[str, dict] = {}
    for r in rows:
        day = (r.get("publish_time") or "")[:10]
        if len(day) != 10:
            continue
        s = by_day_cnt.setdefault(day, {"posts": 0, "eng": 0})
        s["posts"] += 1
        s["eng"] += r.get("engagement") or 0

    out = []
    for p in periods(since, until, grain):
        ds = _days(p["since"], p["until"])
        # 覆盖率按"这个周期的完整天数"算：周期被区间截断时（首尾周），
        # 用完整周期的天数做分母才不会把"区间边界"误报成"数据缺口"
        full = _days(p.get("full_since", p["since"]), p.get("full_until", p["until"]))
        miss = [d for d in full if d not in cov]
        posts = sum(by_day_cnt.get(d, {}).get("posts", 0) for d in ds)
        eng = sum(by_day_cnt.get(d, {}).get("eng", 0) for d in ds)
        sv = round(sum(by_day_sov.get(d, {}).get("sov", 0) for d in ds), 1)
        neg = round(sum(by_day_sov.get(d, {}).get("neg", 0) for d in ds), 1)
        state = "full" if not miss else ("none" if len(miss) == len(full) else "partial")
        srcs = {src.get(d) for d in full if d in cov}
        out.append({
            **p, "days": len(ds), "full_days": len(full),
            "posts": posts, "engagement": eng, "sov": sv, "neg_sov": neg,
            "coverage": state, "missing": len(miss),
            "missing_days": miss[:14],
            # 覆盖凭据来源：run＝有采集流水记录，data＝按库里当天有数据推断
            "source": "run" if srcs == {"run"} else ("data" if srcs == {"data"}
                                                     else ("mixed" if srcs else "")),
            "plats": max((cov.get(d, 0) for d in ds), default=0),
            # 只有"整段都抓过"的周期才允许进对比：缺口周期的数字不是真实声量
            "comparable": state == "full",
        })
    # 环比：只在相邻两个周期都可比时才给，否则显式 null（前端不画箭头）。
    # 还要挡住"小基数"：上期只有 1 条时算出 +7800% 是真的，但它没有信息量，
    # 而且会把折线的纵轴撑得没法看别的周期。基数低于 MIN_BASE 一律不给环比。
    for i, p in enumerate(out):
        prev = out[i - 1] if i else None
        for k in ("posts", "sov", "neg_sov", "engagement"):
            d = None
            if prev and p["comparable"] and prev["comparable"] and (prev[k] or 0) >= MIN_BASE:
                d = round((p[k] - prev[k]) / prev[k] * 100, 1)
            p[f"{k}_delta"] = d
    ok = [p for p in out if p["comparable"]]
    scope_note = ("覆盖按这个关键词自己算。" if keyword else
                  "看的是「全部关键词」，覆盖按各关键词的交集算——"
                  "中途新增关键词会让总量凭空上跳，那是采集口径变了、不是舆论变了。"
                  + (f"当前卡住覆盖的是：{'、'.join(limit_by)}。" if limit_by else ""))
    return {
        "grain": grain, "keyword": keyword, "since": since, "until": until,
        "periods": out,
        "comparable": len(ok), "total": len(out),
        "limit_by": limit_by,
        "note": ("周期对比只统计「整段都采集过」的周期；"
                 "有采集缺口的周期会置灰并标出缺失天数，因为那种周期的低声量分不出"
                 "「真的没人讨论」还是「那几天没抓」。"),
        "scope_note": scope_note,
        "sov_note": ("各周期声量分用同一套平台基线标定（在整个对比区间上算一次），所以可跨周期相加比较。"
                     f"上期基数低于 {MIN_BASE} 时不给环比——那种百分比只是噪音。"),
    }


def gaps(keyword: str, since: str, until: str, platform: str = "",
         strict: bool = False) -> list[dict]:
    """算出（这个渠道的）这个关键词在目标区间里还缺哪些日期，合并成连续区间返回。"""
    cov, _src = merged_coverage(keyword, platform, strict)
    missing = [d for d in _days(since, until) if d not in cov]
    out = []
    for d in missing:
        if out and _d(d) - _d(out[-1]["until"]) == timedelta(days=1):
            out[-1]["until"] = d
            out[-1]["days"] += 1
        else:
            out.append({"since": d, "until": d, "days": 1})
    return out


def plan_gaps(keywords: list[str], platforms: list[str], since: str, until: str,
              max_ranges: int = 6, strict: bool = True) -> dict:
    """增量采集的计划表：每个「渠道 × 关键词」还缺哪几段日期。

    这是"已爬过的渠道不再爬"的依据——覆盖必须按渠道算，
    否则微博抓过就把十个平台都当抓过了，缺的渠道永远补不上。

    strict 默认 True：只认采集流水。限流采集会让"有数据的天"远少于"实际抓过的天"，
    用数据反推会漏掉大量该补的渠道。代价是台账建立之前会重抓一轮——
    这正好对上"部分渠道很离散、可以重新爬取"。

    max_ranges：某个组合的缺口碎成太多段时，不值得发几十次请求，
    直接退化成"抓整段最小外包区间"，并在结果里标出来。
    """
    total_days = len(_days(since, until))
    combos, skip = [], []
    for plat in platforms:
        for kw in keywords:
            g = gaps(kw, since, until, plat, strict)
            miss = sum(x["days"] for x in g)
            if not miss:
                skip.append({"platform": plat, "keyword": kw,
                             "reason": f"整段 {total_days} 天都有采集记录"})
                continue
            merged, note = g, ""
            if len(g) > max_ranges:
                merged = [{"since": g[0]["since"], "until": g[-1]["until"],
                           "days": len(_days(g[0]["since"], g[-1]["until"]))}]
                note = f"缺口碎成 {len(g)} 段，合并成一段整抓"
            combos.append({
                "platform": plat, "keyword": kw,
                "missing_days": miss, "total_days": total_days,
                "ranges": merged, "raw_ranges": len(g), "note": note,
            })
    return {"combos": combos, "skip": skip, "strict": strict,
            "since": since, "until": until,
            "requests": sum(len(c["ranges"]) for c in combos),
            "total_combos": len(combos) + len(skip)}


def coverage_summary(keywords: list[str], since: str, until: str,
                     platforms: list[str] | None = None,
                     strict: bool = True) -> list[dict]:
    """每个关键词在目标区间里的覆盖情况，给采集页「只补缺口」用。

    platforms 给了就同时算出"哪些渠道整段抓过（可跳过）、哪些还缺"。
    """
    out = []
    total = len(_days(since, until))
    for kw in keywords:
        g = gaps(kw, since, until, "", strict)
        miss = sum(x["days"] for x in g)
        cov, _ = merged_coverage(kw, "", strict)
        have = sorted(cov)
        row = {
            "keyword": kw, "total_days": total, "missing_days": miss,
            "covered_days": total - miss,
            "ratio": round((total - miss) / total * 100, 1) if total else 0,
            "gaps": g[:12], "gap_count": len(g),
            "first_covered": have[0] if have else "", "last_covered": have[-1] if have else "",
        }
        if platforms:
            done, todo = [], []
            for p in platforms:
                pm = sum(x["days"] for x in gaps(kw, since, until, p, strict))
                (done if pm == 0 else todo).append({
                    "platform": p, "missing_days": pm,
                    "ratio": round((total - pm) / total * 100, 1) if total else 0,
                })
            row["plat_done"] = done
            row["plat_todo"] = sorted(todo, key=lambda x: -x["missing_days"])
        out.append(row)
    return out
