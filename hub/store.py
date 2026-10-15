"""落库与导出：SQLite 为主库，按 (platform, post_id) 去重 upsert。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import clean, config
from .models import FIELDS, Post

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    platform        TEXT NOT NULL,
    platform_label  TEXT,
    post_id         TEXT NOT NULL,
    title           TEXT,
    content         TEXT,
    url             TEXT,
    author          TEXT,
    author_id       TEXT,
    publish_time    TEXT,
    likes           INTEGER DEFAULT 0,
    comments        INTEGER DEFAULT 0,
    shares          INTEGER DEFAULT 0,
    collects        INTEGER DEFAULT 0,
    views           INTEGER DEFAULT 0,
    keyword         TEXT,
    engagement      INTEGER DEFAULT 0,
    fetched_at      TEXT,
    extra           TEXT,
    layer            TEXT,
    layer_reason     TEXT,
    actor            TEXT,
    actor_reason     TEXT,
    sentiment        TEXT,
    sentiment_reason TEXT,
    manual_sentiment TEXT,
    manual_layer     TEXT,
    manual_at        TEXT,
    deleted          INTEGER DEFAULT 0,
    PRIMARY KEY (platform, post_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(publish_time);
CREATE INDEX IF NOT EXISTS idx_posts_kw ON posts(keyword);
CREATE TABLE IF NOT EXISTS runs (
    started_at TEXT, finished_at TEXT, platform TEXT, keyword TEXT,
    got INTEGER, saved INTEGER, note TEXT
);
-- 帖子评论：默认不采，采集时勾选「同时采评论」才写。
-- 单独建表而不是塞进 posts 的 extra：评论是一对多，塞 JSON 没法按评论者/时间筛和导出。
-- 主键 (platform, post_id, comment_id) 保证重复采集同一条帖子时评论幂等覆盖。
CREATE TABLE IF NOT EXISTS comments (
    platform      TEXT NOT NULL,
    post_id       TEXT NOT NULL,
    comment_id    TEXT NOT NULL,
    commenter     TEXT,
    commenter_id  TEXT,
    comment_time  TEXT,
    content       TEXT,
    likes         INTEGER DEFAULT 0,
    keyword       TEXT,
    fetched_at    TEXT,
    PRIMARY KEY (platform, post_id, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(platform, post_id);
"""

# 老库没有这些列，补列而不是重建表，历史数据不能丢
MIGRATIONS = [("layer", "TEXT"), ("layer_reason", "TEXT"),
              ("actor", "TEXT"), ("actor_reason", "TEXT"),
              ("sentiment", "TEXT"), ("sentiment_reason", "TEXT"),
              # 人工复核：manual_* 一旦有值就永久覆盖规则结论，reclassify 也不会动它
              ("manual_sentiment", "TEXT"), ("manual_layer", "TEXT"),
              ("manual_at", "TEXT"), ("deleted", "INTEGER DEFAULT 0"),
              # 内容级去重：dup_key 是「标题+作者+正文+发布时间」的指纹，
              # dup_of 指向留下来的那条（platform:post_id），非空即视为重复、不进任何口径
              ("dup_key", "TEXT"), ("dup_of", "TEXT"),
              # 内容类型：规则侧的 detect_topic 结论（广告反馈/明星应援/营销活动/修辞误召回）
              ("topic_kind", "TEXT"), ("topic_reason", "TEXT"),
              # 语义裁决：模型对"规则判不出情绪"的边界样本给的结论。
              # 优先级 人工 manual_* > 模型 ai_* > 规则 sentiment/layer，
              # 三个来源在明细里分得清，谁判的就写谁的依据，不做黑盒覆盖。
              ("ai_sentiment", "TEXT"), ("ai_topic", "TEXT"),
              ("ai_competitor", "INTEGER DEFAULT 0"), ("ai_reason", "TEXT"),
              ("ai_at", "TEXT"),
              # 文本信息不足（有效字数极少、情绪多半在图片里）：显式标出来交人工，
              # 不默默判成中性噪音
              ("text_thin", "INTEGER DEFAULT 0"), ("thin_reason", "TEXT")]
# runs 表补的列：batch_id 把"一次采集计划"里的多个平台×关键词串起来
RUN_MIGRATIONS = [("batch_id", "TEXT"), ("since_day", "TEXT"), ("until_day", "TEXT"),
                  ("limit_n", "INTEGER"), ("scope_note", "TEXT")]


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.DB_PATH)
    c.executescript(SCHEMA)
    have = {r[1] for r in c.execute("PRAGMA table_info(posts)")}
    for col, typ in MIGRATIONS:
        if col not in have:
            c.execute(f"ALTER TABLE posts ADD COLUMN {col} {typ}")
    have_run = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    for col, typ in RUN_MIGRATIONS:
        if col not in have_run:
            c.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")
    c.execute("CREATE INDEX IF NOT EXISTS idx_posts_dup ON posts(dup_key)")
    return c


# ---------------------------------------------------------------- 内容级去重
# 为什么需要它：主键是 (platform, post_id)，只能挡住"同一平台同一条帖子被多次抓到"。
# 但同一条内容会因为**多个关键词**、或跨平台转载/镜像而变成好几行不同 post_id 的记录，
# 看板上就会把一条内容算成两三条声量。所以再加一层"内容指纹"去重。
#
# 指纹口径就是用户要的四要素：标题 + 作者 + 正文 + 发布时间（到分钟）。
# 归一化只做"去空白 + 去标点 + 小写"，不做同义改写——宁可漏判，也不能把两条不同内容合成一条。
_PUNCT = re.compile(r"[\s\u3000!-/:-@\[-`{-~，。、；：？！…—·「」『』（）【】《》“”‘’]+")


def dup_key(title: str, author: str, content: str, publish_time: str) -> str:
    from . import settings

    cfg = settings.get("dedupe")

    def norm(s: str) -> str:
        return _PUNCT.sub("", (s or "")).lower()

    t = (publish_time or "")[:10] if cfg.get("time_to_day", True) else (publish_time or "")[:16]
    a = norm(author) if cfg.get("with_author", True) else ""
    n = int(cfg.get("body_chars") or 0)
    # 正文只取前 N 个归一化字符：不同平台对同一篇文章的截断长度不一样，
    # 全文比对会因为尾部多一句"点击查看原文"就判成不重复
    body = norm(content)[:n] if n > 0 else ""
    return hashlib.md5(f"{norm(title)}|{a}|{body}|{t}".encode("utf-8")).hexdigest()


def dedupe_note() -> str:
    """把当前判重口径写成人话，UI 和报告里直接引用，避免"去了重但说不清按什么去的"。"""
    from . import settings

    cfg = settings.get("dedupe")
    parts = ["标题"]
    if cfg.get("with_author", True):
        parts.append("作者")
    n = int(cfg.get("body_chars") or 0)
    if n > 0:
        parts.append(f"正文前 {n} 字")
    parts.append("发布日期" if cfg.get("time_to_day", True) else "发布时间(到分钟)")
    return "＋".join(parts) + " 完全一致即视为同一条（只标记不删除，可在「历史与导出 › 重复内容」核对）"


def rebuild_dup_keys() -> int:
    """口径改了要重算全库指纹。返回重算行数。"""
    with conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT platform, post_id, title, author, content, publish_time"
                         " FROM posts").fetchall()
        for r in rows:
            c.execute("UPDATE posts SET dup_key=? WHERE platform=? AND post_id=?",
                      (dup_key(r["title"], r["author"], r["content"], r["publish_time"]),
                       r["platform"], r["post_id"]))
        # 口径变了，旧的判重结论一律作废重算，否则会残留上一套口径的 dup_of
        c.execute("UPDATE posts SET dup_of=NULL")
    return len(rows)


def syndication(rows: list[dict], min_size: int = 3) -> dict:
    """同题转载统计：标题相同但作者不同的稿件。

    这类**不自动去重**——多个号转载同一篇稿是真实的扩散，去掉会低估声量；
    但它会把"一篇稿"看成"十几条声量"，所以单独统计出来，让人自己判断要不要收紧口径。
    """
    groups: dict[str, list[dict]] = {}
    for r in rows:
        t = _PUNCT.sub("", (r.get("title") or "")).lower()
        if len(t) >= 8:
            groups.setdefault(t, []).append(r)
    multi = [(t, v) for t, v in groups.items()
             if len(v) >= min_size and len({x.get("author") for x in v}) > 1]
    multi.sort(key=lambda x: -len(x[1]))
    return {
        "groups": len(multi),
        "extra_posts": sum(len(v) - 1 for _, v in multi),
        "top": [{
            "title": (v[0].get("title") or "")[:60],
            "copies": len(v),
            "authors": len({x.get("author") for x in v}),
            "platforms": sorted({x.get("platform_label") or x.get("platform") for x in v}),
            "engagement": sum(x.get("engagement") or 0 for x in v),
        } for _, v in multi[:8]],
    }


def _post_dup_key(p: Post) -> str:
    return dup_key(p.title, p.author, p.content, p.publish_time)


def dedupe(only_keys: list[str] | None = None) -> dict:
    """把同一 dup_key 的多行折成一行：留互动量最高、其次采集最早的那条，其余写 dup_of。

    只标记不删除，历史与导出页可以逐条核对判重是否合理；
    人工已经改判过的（manual_*）优先保留，避免把人的结论判成重复丢掉。
    """
    with conn() as c:
        c.row_factory = sqlite3.Row
        sql = ("SELECT platform, post_id, dup_key, engagement, fetched_at, platform_label,"
               " COALESCE(manual_sentiment,'') AS ms, COALESCE(manual_layer,'') AS ml,"
               " COALESCE(dup_of,'') AS dof FROM posts"
               " WHERE COALESCE(deleted,0)=0 AND COALESCE(dup_key,'') != ''")
        args: list = []
        if only_keys:
            sql += f" AND dup_key IN ({','.join('?' * len(only_keys))})"
            args = list(only_keys)
        groups: dict[str, list[dict]] = {}
        for r in c.execute(sql, args):
            groups.setdefault(r["dup_key"], []).append(dict(r))

        marked, freed, touched = 0, 0, 0
        for key, rows in groups.items():
            if len(rows) == 1:
                # 只剩一条了就把它放回来（之前被判重、现在原始条目被删了的情况）
                if rows[0]["dof"]:
                    c.execute("UPDATE posts SET dup_of=NULL WHERE platform=? AND post_id=?",
                              (rows[0]["platform"], rows[0]["post_id"]))
                    freed += 1
                continue
            touched += 1
            rows.sort(key=lambda r: (
                0 if (r["ms"] or r["ml"]) else 1,        # 人工改判过的优先留
                -(r["engagement"] or 0),                  # 再看互动量（指标更全的那条）
                r["fetched_at"] or "",                    # 最后看谁先入库
            ))
            keep = rows[0]
            keep_ref = f'{keep["platform"]}:{keep["post_id"]}'
            if keep["dof"]:
                c.execute("UPDATE posts SET dup_of=NULL WHERE platform=? AND post_id=?",
                          (keep["platform"], keep["post_id"]))
                freed += 1
            for r in rows[1:]:
                if r["dof"] == keep_ref:
                    continue
                c.execute("UPDATE posts SET dup_of=? WHERE platform=? AND post_id=?",
                          (keep_ref, r["platform"], r["post_id"]))
                marked += 1
        total_dup = c.execute(
            "SELECT COUNT(*) FROM posts WHERE COALESCE(dup_of,'') != ''").fetchone()[0]
    return {"groups": touched, "marked": marked, "freed": freed, "total_dup": total_dup}


def save(posts: list[Post]) -> int:
    if not posts:
        return 0
    for p in posts:
        if not (p.layer and p.actor and p.sentiment):
            tags = clean.classify_full(
                p.platform, p.platform_label, p.title, p.content, p.author,
                {"likes": p.likes, "shares": p.shares, "comments": p.comments,
                 "engagement": p.engagement}, p.extra or {})
            p.layer, p.layer_reason = tags["layer"], tags["layer_reason"]
            p.actor, p.actor_reason = tags["actor"], tags["actor_reason"]
            p.sentiment, p.sentiment_reason = tags["sentiment"], tags["sentiment_reason"]
    cols = ", ".join(FIELDS) + ", dup_key"
    marks = ", ".join("?" * (len(FIELDS) + 1))
    updates = ", ".join(f"{f}=excluded.{f}" for f in FIELDS
                        if f not in {"platform", "post_id"}) + ", dup_key=excluded.dup_key"
    sql = (f"INSERT INTO posts ({cols}) VALUES ({marks}) "
           f"ON CONFLICT(platform, post_id) DO UPDATE SET {updates}")
    keys = []
    with conn() as c:
        rows = []
        for p in posts:
            k = _post_dup_key(p)
            keys.append(k)
            rows.append([p.row()[f] for f in FIELDS] + [k])
        c.executemany(sql, rows)
    # 只对这批新指纹做增量判重，不用每次全库扫
    dedupe(sorted(set(keys)))
    return len(posts)


def save_comments(rows: list[dict]) -> int:
    """写评论。rows 每条至少要有 platform/post_id/comment_id/content。

    幂等：同一 (platform,post_id,comment_id) 重复采集就覆盖，不会重复堆积。
    comment_id 缺失时用「内容+评论者」兜底生成，避免同帖多条评论 PK 撞车。
    """
    if not rows:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = []
    for r in rows:
        cid = str(r.get("comment_id") or "").strip()
        if not cid:
            base = f"{r.get('commenter','')}|{r.get('content','')}|{r.get('comment_time','')}"
            cid = hashlib.md5(base.encode("utf-8")).hexdigest()[:16]
        payload.append((
            r.get("platform", ""), str(r.get("post_id") or ""), cid,
            (r.get("commenter") or "")[:80], str(r.get("commenter_id") or ""),
            r.get("comment_time") or "", (r.get("content") or "")[:2000],
            int(r.get("likes") or 0), r.get("keyword") or "", now,
        ))
    with conn() as c:
        c.executemany(
            "INSERT INTO comments (platform, post_id, comment_id, commenter,"
            " commenter_id, comment_time, content, likes, keyword, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(platform, post_id, comment_id) DO UPDATE SET"
            " commenter=excluded.commenter, comment_time=excluded.comment_time,"
            " content=excluded.content, likes=excluded.likes,"
            " fetched_at=excluded.fetched_at", payload)
    return len(payload)


def comments_of(platform: str, post_id: str, limit: int = 300) -> list[dict]:
    """取某条帖子的评论，按点赞降序、时间降序。"""
    with conn() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT commenter, commenter_id, comment_time, content, likes"
            " FROM comments WHERE platform=? AND post_id=?"
            " ORDER BY likes DESC, comment_time DESC LIMIT ?",
            (platform, post_id, limit))]


def comment_counts(pairs: list[tuple[str, str]] | None = None) -> dict[str, int]:
    """批量取「platform|post_id -> 评论条数」，给明细列表标角标用。

    pairs 为空就返回全库分组计数（量小，全表 group by 够快）。
    """
    with conn() as c:
        rows = c.execute(
            "SELECT platform || '|' || post_id AS k, COUNT(*) FROM comments GROUP BY k")
        out = {k: n for k, n in rows}
    if pairs is None:
        return out
    want = {f"{p}|{i}" for p, i in pairs}
    return {k: v for k, v in out.items() if k in want}



    """被判重的条目清单，带它指向的原始条目，供人工核对。"""
    with conn() as c:
        c.row_factory = sqlite3.Row
        dups = [dict(r) for r in c.execute(
            "SELECT platform, platform_label, post_id, title, author, publish_time,"
            " engagement, keyword, dup_of, dup_key FROM posts"
            " WHERE COALESCE(dup_of,'') != '' ORDER BY dup_key LIMIT ?", (limit,))]
        out = []
        for d in dups:
            plat, _, pid = (d["dup_of"] or "").partition(":")
            src = c.execute(
                "SELECT platform_label, title, keyword, engagement FROM posts"
                " WHERE platform=? AND post_id=?", (plat, pid)).fetchone()
            d["source"] = dict(src) if src else None
            out.append(d)
    return out


def log_run(platform: str, keyword: str, got: int, saved: int,
            started: str, note: str = "", batch_id: str = "",
            since: str = "", until: str = "", limit_n: int = 0):
    """记一条「平台 × 关键词」的执行流水。

    batch_id 是"一次采集计划"的标识（同一次任务里所有平台/关键词共用），
    历史页按它归组：用户关心的是"这次抓了什么"，渠道明细是计划里的下一层。
    """
    with conn() as c:
        c.execute(
            "INSERT INTO runs (started_at, finished_at, platform, keyword, got, saved,"
            " note, batch_id, since_day, until_day, limit_n) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (started, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             platform, keyword, got, saved, note, batch_id, since, until, limit_n))


def get_post(platform: str, post_id: str) -> dict | None:
    with conn() as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM posts WHERE platform=? AND post_id=?",
                      (platform, str(post_id))).fetchone()
    return _apply_manual([dict(r)])[0] if r else None


def set_manual(platform: str, post_id: str, sentiment: str = "",
               layer: str = "") -> dict:
    """人工改判：写 manual_* 列，读取时优先于规则结论，reclassify 也不覆盖。

    传空串＝撤销人工结论，回到规则判定。
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if (sentiment or layer) else ""
    with conn() as c:
        c.execute("UPDATE posts SET manual_sentiment=?, manual_layer=?, manual_at=?"
                  " WHERE platform=? AND post_id=?",
                  (sentiment, layer, stamp, platform, post_id))
        n = c.total_changes
    return {"ok": bool(n), "sentiment": sentiment, "layer": layer, "at": stamp}


def set_deleted(platform: str, post_id: str, deleted: bool = True) -> dict:
    """软删除：不真的 DELETE，标 deleted=1。

    这样重新采集时同一条不会又"复活"成正常数据（upsert 不碰 deleted 列），
    也留了后路可以恢复。
    """
    with conn() as c:
        c.execute("UPDATE posts SET deleted=? WHERE platform=? AND post_id=?",
                  (1 if deleted else 0, platform, post_id))
        n = c.total_changes
    return {"ok": bool(n), "deleted": bool(deleted)}


def _apply_manual(rows: list[dict]) -> list[dict]:
    """把人工结论盖到规则结论上，并留下 *_reason 说明是谁判的。"""
    for r in rows:
        ms = (r.get("manual_sentiment") or "").strip()
        ml = (r.get("manual_layer") or "").strip()
        if ms:
            r["sentiment_rule"] = r.get("sentiment")
            r["sentiment"] = ms
            r["sentiment_reason"] = f"人工改判（{r.get('manual_at') or ''}）；" \
                                    f"规则原判：{r.get('sentiment_rule') or '未判定'}"
        if ml:
            r["layer_rule"] = r.get("layer")
            r["layer"] = ml
            r["layer_reason"] = f"人工改判（{r.get('manual_at') or ''}）；" \
                                f"规则原判：{r.get('layer_rule') or '未分层'}"
    return rows


def _eff_exprs() -> tuple[str, str]:
    """返回 (eff_layer, eff_sentiment) 两段 SQL 表达式。

    抽出来是因为按批次导出也要用同一套口径。复制一份必然走样：
    这里的优先级（人工 > 模型 > 规则）和「模型判中性时分层跟着落到 C/D」
    都是踩过坑改出来的，两处各写一份，改一边忘一边就会出现
    "页面说噪音、导出说负反馈"。
    """
    # 分层/情感一律走"人工优先"的有效值：人工把一条改判成正面后，
    # 主口径（只含负面层）必须立刻不再包含它，否则改判等于没生效
    # 生效值优先级：人工改判 > 模型语义裁决 > 规则。
    eff_sent = "COALESCE(NULLIF(manual_sentiment,''), NULLIF(ai_sentiment,''), sentiment)"
    # layer 跟着生效情感重算：模型把中性翻成负面时，分层要一起动，否则
    # 「情感=负面但还挂在噪音层」会让主口径漏掉它。
    eff_layer = (
        "(CASE WHEN COALESCE(manual_layer,'') != '' THEN manual_layer"
        "     WHEN COALESCE(ai_sentiment,'') = '负面' AND COALESCE(manual_sentiment,'') = ''"
        "          THEN (CASE WHEN actor IN ('官方机构','官方媒体','新闻媒体')"
        "                     THEN 'B_媒体负面报道' ELSE 'A_UGC负反馈' END)"
        "     WHEN COALESCE(ai_sentiment,'') = '正面' AND COALESCE(manual_sentiment,'') = ''"
        "          THEN 'E_正面反馈'"
        # 模型判中性时也必须把分层拉下来。之前这里直接 ELSE layer，规则若已判 A_UGC负反馈，
        # 就会出现「情感=中性 AI、类型=明星应援，分层还挂着 UGC负反馈」——用户截图里的
        # 明星应援和卖 SVIP 兑换码两条正是这么漏进主口径的。
        # 明星应援 / 营销活动 / 与广告无关 三类按用户口径直接剔除到噪音，
        # 其余中性内容归 C_中性事件报道。
        "     WHEN COALESCE(ai_sentiment,'') = '中性' AND COALESCE(manual_sentiment,'') = ''"
        "          THEN (CASE WHEN COALESCE(ai_topic,'') IN ('明星应援','营销活动','无关')"
        "                     THEN 'D_噪音' ELSE 'C_中性事件报道' END)"
        "     ELSE layer END)")
    return eff_layer, eff_sent


def query_by_runs(runs: list[dict], include_deleted: bool = False) -> list[dict]:
    """按采集流水反查这批抓到的帖子。

    posts 里没有 batch_id，所以靠「平台 + 关键词 + 采集时间落在这条流水的起止之间」反查。
    这个口径是**不重不漏**的，原因在 upsert：同一条帖子被后一次采集再抓到时
    fetched_at 会被覆盖成新时间，所以任意一条帖子只会落在**最后一次抓到它**的
    那条流水窗口里，不会同时算进两个批次。这也是它跟"删除计划"的区别——
    删除要动数据所以不能这么反查（见前端注释），只读导出没这个问题。

    起点减 1 秒：流水的 started_at 和第一条 fetched_at 偶尔同秒但先后不定，
    卡死边界会漏掉第一条。
    """
    if not runs:
        return []
    eff_layer, eff_sent = _eff_exprs()
    clauses, args = [], []
    for r in runs:
        st, fi = r.get("started_at") or "", r.get("finished_at") or ""
        if not (st and fi and r.get("platform")):
            continue
        clauses.append("(platform = ? AND keyword = ?"
                       " AND fetched_at BETWEEN datetime(?, '-1 second') AND ?)")
        args += [r["platform"], r.get("keyword") or "", st, fi]
    if not clauses:
        return []
    sql = ("SELECT *, " + eff_layer + " AS eff_layer, " + eff_sent + " AS eff_sentiment"
           " FROM posts WHERE (" + " OR ".join(clauses) + ")")
    if not include_deleted:
        sql += " AND COALESCE(deleted,0) = 0"
    sql += " ORDER BY publish_time DESC"
    with conn() as c:
        c.row_factory = sqlite3.Row
        return _apply_manual([dict(x) for x in c.execute(sql, args)])


def query(platforms: list[str] | None = None, keyword: str = "",
          since: str = "", until: str = "",
          layers: list[str] | None = None,
          actors: list[str] | None = None,
          sentiments: list[str] | None = None,
          keywords: list[str] | None = None,
          include_deleted: bool = False,
          include_dup: bool = False) -> list[dict]:
    eff_layer, eff_sent = _eff_exprs()
    # 生效值要跟着一起回给前端，否则「筛选按 eff_layer、显示按原始 layer」会自相矛盾：
    # 模型把一条从中性翻成负面后，它能被筛进主口径，但表格里还显示"噪音"。
    sql = ("SELECT *, " + eff_layer + " AS eff_layer, " + eff_sent + " AS eff_sentiment"
           " FROM posts WHERE 1=1")
    args: list = []
    if not include_deleted:
        sql += " AND COALESCE(deleted,0) = 0"
    if not include_dup:
        # 内容重复的行不进任何口径：否则同一条内容会按关键词个数被重复计声量
        sql += " AND COALESCE(dup_of,'') = ''"
    if platforms:
        sql += f" AND platform IN ({','.join('?' * len(platforms))})"
        args += platforms
    if layers:
        # 没分层的老数据（layer 为空）跟着主口径一起放过，不然会凭空少数据
        sql += f" AND ({eff_layer} IN ({','.join('?' * len(layers))})"
        args += layers
        sql += f" OR {eff_layer} IS NULL OR {eff_layer} = '')" if "D_噪音" not in layers else ")"
    if actors:
        sql += f" AND actor IN ({','.join('?' * len(actors))})"
        args += actors
    if sentiments:
        sql += f" AND {eff_sent} IN ({','.join('?' * len(sentiments))})"
        args += sentiments
    if keywords:
        # 一次采集计划往往有多个关键词，历史页按计划还原数据时需要一次筛多个词
        sql += " AND (" + " OR ".join(["keyword LIKE ?"] * len(keywords)) + ")"
        args += [f"%{k}%" for k in keywords]
    elif keyword:
        sql += " AND keyword LIKE ?"
        args.append(f"%{keyword}%")
    if since:
        # 严格按发布时间过滤：没有发布时间的条目在指定区间时不展示，避免混进区间外内容
        sql += " AND publish_time != '' AND substr(publish_time,1,10) >= ?"
        args.append(since)
    if until:
        sql += " AND publish_time != '' AND substr(publish_time,1,10) <= ?"
        args.append(until)
    sql += " ORDER BY engagement DESC, publish_time DESC"
    with conn() as c:
        c.row_factory = sqlite3.Row
        return _apply_manual([dict(r) for r in c.execute(sql, args).fetchall()])


def reclassify(force: bool = False) -> dict:
    """给库里已有数据补/重算主体、情感与分层，返回各维度条数。

    人工改判过的条目（manual_sentiment / manual_layer 有值）只重算规则列，
    不动 manual_* —— 重新分层不能把人工结论冲掉。
    """
    with conn() as c:
        c.row_factory = sqlite3.Row
        sql = ("SELECT platform, platform_label, post_id, title, content, author, "
               "likes, shares, comments, engagement, extra, layer, actor FROM posts "
               "WHERE COALESCE(deleted,0) = 0")
        if not force:
            sql += (" AND (layer IS NULL OR layer = '' OR actor IS NULL OR actor = ''"
                    " OR sentiment IS NULL OR sentiment = '')")
        rows = [dict(r) for r in c.execute(sql).fetchall()]
        for r in rows:
            try:
                extra = json.loads(r.get("extra") or "{}")
            except Exception:
                extra = {}
            tags = clean.classify_full(
                r["platform"], r["platform_label"], r["title"], r["content"],
                r.get("author") or "",
                {"likes": r.get("likes") or 0, "shares": r.get("shares") or 0,
                 "comments": r.get("comments") or 0,
                 "engagement": r.get("engagement") or 0}, extra)
            c.execute("UPDATE posts SET layer=?, layer_reason=?, actor=?, actor_reason=?,"
                      " sentiment=?, sentiment_reason=?, topic_kind=?, topic_reason=?,"
                      " text_thin=?, thin_reason=? WHERE platform=? AND post_id=?",
                      (tags["layer"], tags["layer_reason"], tags["actor"],
                       tags["actor_reason"], tags["sentiment"], tags["sentiment_reason"],
                       tags.get("topic_kind") or "", tags.get("topic_reason") or "",
                       tags.get("text_thin") or 0, tags.get("thin_reason") or "",
                       r["platform"], r["post_id"]))
        stat = dict(c.execute(
            "SELECT COALESCE(NULLIF(COALESCE(NULLIF(manual_layer,''),layer),''),'未分层') AS k,"
            " COUNT(*) FROM posts WHERE COALESCE(deleted,0)=0 GROUP BY k"
        ).fetchall())
        actors = dict(c.execute(
            "SELECT COALESCE(NULLIF(actor,''),'未判定') AS k, COUNT(*) FROM posts"
            " WHERE COALESCE(deleted,0)=0 GROUP BY k"
        ).fetchall())
        sents = dict(c.execute(
            "SELECT COALESCE(NULLIF(COALESCE(NULLIF(manual_sentiment,''),sentiment),''),'未判定')"
            " AS k, COUNT(*) FROM posts WHERE COALESCE(deleted,0)=0 GROUP BY k"
        ).fetchall())
        manual = c.execute(
            "SELECT COUNT(*) FROM posts WHERE COALESCE(manual_sentiment,'') != ''"
            " OR COALESCE(manual_layer,'') != ''").fetchone()[0]
        removed = c.execute("SELECT COUNT(*) FROM posts WHERE COALESCE(deleted,0)=1").fetchone()[0]
    return {"updated": len(rows), "layers": stat, "actors": actors, "sentiments": sents,
            "manual_kept": manual, "deleted": removed}


# 导出列。口径是「看板上能看到的标签，导出里一个都不能少」——
# 之前只导规则那一层（layer/sentiment），人工改判和模型判定都不在里面，
# 拿导出文件复核的人看到的还是旧结论，跟页面对不上。
# 所以这里分三段排：生效值（实际口径）→ 三个来源各自的原判 → 互动与溯源字段。
HEADERS = [
    ("平台", "platform_label"), ("发布时间", "publish_time"), ("作者", "author"),
    ("主体类型", "actor"), ("主体依据", "actor_reason"),
    # —— 生效值：人工 > 模型 > 规则，跟页面和口径筛选完全一致
    ("生效情感", "eff_sentiment"), ("生效分层", "eff_layer"),
    ("内容类型", "ai_topic"), ("类型判据", "topic_reason"),
    # —— 规则原判
    ("规则情感", "sentiment"), ("规则情感依据", "sentiment_reason"),
    ("规则分层", "layer"), ("规则判定依据", "layer_reason"),
    ("规则类型", "topic_kind"),
    # —— 模型判定
    ("模型情感", "ai_sentiment"), ("模型类型", "ai_topic"),
    ("模型判竞品", "ai_competitor"), ("模型依据", "ai_reason"), ("模型判定时间", "ai_at"),
    # —— 人工改判
    ("人工情感", "manual_sentiment"), ("人工分层", "manual_layer"), ("人工改判时间", "manual_at"),
    # —— 正文不足标记：这类条目的情绪可能只在图里，复核时要单独看
    ("正文过短", "text_thin"), ("过短原因", "thin_reason"),
    ("声量分", "sov"), ("声量口径", "sov_basis"),
    ("标题", "title"), ("正文", "content"),
    ("点赞", "likes"), ("评论", "comments"),
    ("转发/分享", "shares"), ("收藏", "collects"), ("播放/阅读", "views"),
    ("互动量", "engagement"), ("搜索词", "keyword"), ("链接", "url"),
    ("采集时间", "fetched_at"), ("平台标识", "platform"), ("帖子ID", "post_id"),
    ("判重指向", "dup_of"),
]


def _cell(r: dict, key: str):
    """取一个导出单元格的值。

    两个字段不能直接搬库里的原值：
      * ai_competitor / text_thin 存的是 0/1，导出成数字没人看得懂，转成「是/空」；
      * eff_* 是 query() 现算的列，老的导出调用（比如直接传 conn 查出来的行）
        可能没有，缺了就按同样的优先级在这里补，避免导出比页面少一层结论。
    """
    if key in ("ai_competitor", "text_thin"):
        return "是" if r.get(key) else ""
    if key == "eff_sentiment" and not r.get(key):
        return (r.get("manual_sentiment") or r.get("ai_sentiment")
                or r.get("sentiment") or "")
    if key == "eff_layer" and not r.get(key):
        return r.get("manual_layer") or r.get("layer") or ""
    return r.get(key, "")


def export_csv(rows: list[dict], path: Path | None = None) -> Path:
    """导 CSV。给要拿去 SQL / pandas / BI 里接着算的人用。

    带 UTF-8 BOM：不带的话 Excel 双击打开中文全是乱码，
    而这个文件最常见的用法就是"双击打开看一眼"。
    """
    import csv

    path = path or (config.EXPORT_DIR /
                    f"声量数据_{datetime.now():%Y%m%d_%H%M}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([h for h, _ in HEADERS])
        for r in rows:
            w.writerow([_cell(r, k) for _, k in HEADERS])
    return path


def export_xlsx(rows: list[dict], path: Path | None = None) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path = path or (config.EXPORT_DIR /
                    f"声量数据_{datetime.now():%Y%m%d_%H%M}.xlsx")
    # 声量分是按"当前这批数据"现算的（平台基线来自这批的中位数），所以导出前统一算一次
    from . import sov as sov_mod
    sov_stat = sov_mod.summarize(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = "帖子明细"
    ws.append([h for h, _ in HEADERS])
    for r in rows:
        ws.append([_cell(r, k) for _, k in HEADERS])
    # 宽度按列语义给，不是平均分。逐条对着 HEADERS 数，长度必须一致——
    # 之前插过一列没同步这个表，后面所有列的宽度就整体错位了一格。
    widths = [12, 17, 18, 12, 30,
              10, 14, 12, 34,
              10, 30, 14, 34, 12,
              10, 12, 10, 34, 18,
              10, 12, 18,
              9, 22,
              9, 10,
              46, 60,
              8, 8, 10, 8, 10, 9, 16, 46, 18, 12, 24, 22]
    assert len(widths) == len(HEADERS), f"列宽 {len(widths)} 对不上表头 {len(HEADERS)}"
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E79")
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    ws2 = wb.create_sheet("平台声量汇总")
    ws2.append(["平台", "帖子数", "声量分", "负面声量分", "实测条数", "估算条数",
                "互动量合计", "点赞", "评论", "转发", "收藏"])
    agg: dict[str, list[int]] = {}
    for r in rows:
        a = agg.setdefault(r.get("platform_label", ""), [0, 0, 0, 0, 0, 0])
        a[0] += 1
        a[1] += r.get("engagement", 0) or 0
        a[2] += r.get("likes", 0) or 0
        a[3] += r.get("comments", 0) or 0
        a[4] += r.get("shares", 0) or 0
        a[5] += r.get("collects", 0) or 0
    sov_by_plat = {p["name"]: p for p in sov_stat["by_platform"]}
    for label, a in sorted(agg.items(), key=lambda kv: -kv[1][1]):
        s = sov_by_plat.get(label, {})
        ws2.append([label, a[0], s.get("sov", 0), s.get("neg_sov", 0),
                    s.get("measured", 0), a[0] - s.get("measured", 0)] + a[1:])
    ws2.column_dimensions["A"].width = 14
    for cell in ws2[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E79")

    ws3 = wb.create_sheet("清洗分层汇总")
    ws3.append(["分层", "条数", "说明"])
    desc = {
        "A_UGC负反馈": "普通用户/大V 的吐槽、投诉、负面反馈，声量主口径",
        "B_媒体负面报道": "媒体、机构、平台号发布的负面内容，声量主口径",
        "C_中性事件报道": "只转述事实/流程，不计入负反馈声量",
        "D_噪音": "教程、赚钱、关广告方法等与负反馈无关的内容，已剔除",
        "E_正面反馈": "整治见效、体验变好等正面表达",
        "未分层": "清洗规则未加载或采集时未分层",
    }
    lay: dict[str, int] = {}
    for r in rows:
        k = r.get("layer") or "未分层"
        lay[k] = lay.get(k, 0) + 1
    for k in sorted(lay, key=lambda x: -lay[x]):
        ws3.append([k, lay[k], desc.get(k, "")])
    ws3.column_dimensions["A"].width = 18
    ws3.column_dimensions["B"].width = 8
    ws3.column_dimensions["C"].width = 60
    for cell in ws3[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E79")

    # 主体 × 情感交叉表：看"谁在说负面"，比只看总量更能定舆情等级
    from .tagging import ACTORS, SENTIMENTS
    ws4 = wb.create_sheet("主体×情感")
    ws4.append(["主体类型"] + SENTIMENTS + ["合计", "互动量合计"])
    cross: dict[str, dict] = {}
    for r in rows:
        a = cross.setdefault(r.get("actor") or "未判定",
                             {s: 0 for s in SENTIMENTS} | {"合计": 0, "互动量": 0})
        s = r.get("sentiment") or "中性"
        a[s] = a.get(s, 0) + 1
        a["合计"] += 1
        a["互动量"] += r.get("engagement") or 0
    order = [a for a in ACTORS if a in cross] + [a for a in cross if a not in ACTORS]
    for a in order:
        v = cross[a]
        ws4.append([a] + [v.get(s, 0) for s in SENTIMENTS] + [v["合计"], v["互动量"]])
    ws4.column_dimensions["A"].width = 16
    for cell in ws4[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E79")

    ws5 = wb.create_sheet("声量测算说明")
    cal = sov_stat["calibration"]
    ws5.append(["项目", "取值", "说明"])
    ws5.append(["公式", "(1 + log10(1 + 热度)) × 主体权重", sov_mod.FORMULA_NOTE])
    ws5.append(["总声量分", sov_stat["total_sov"], f"有效内容 {sov_stat['posts']} 条"])
    ws5.append(["负面声量分", sov_stat["neg_sov"],
                f"占总声量 {sov_stat['neg_share']}%（负反馈主口径看这个）"])
    ws5.append(["日均声量分", sov_stat["daily_avg"], "总声量 ÷ 有数据天数"])
    ws5.append(["实测占比", f"{sov_stat['measured_ratio']}%",
                f"实测 {sov_stat['measured']} 条 / 估算 {sov_stat['imputed']} 条；"
                f"估算来自微信公众号、百家号（网页侧无互动数）"])
    for plat, b in cal["platform_base"].items():
        ws5.append([f"平台基线 {plat}", b, "该平台观测量中位数，热度=观测量÷基线，现算不写死"])
    for actor, h in cal["actor_heat"].items():
        ws5.append([f"主体热度中位数 {actor}", h, "无互动数平台按同主体这个值插补"])
    for actor, w in cal["actor_weight"].items():
        ws5.append([f"主体权重 {actor}", w, "补的是抓不到的跨渠道转载放大，可按需调整"])
    ws5.column_dimensions["A"].width = 24
    ws5.column_dimensions["B"].width = 30
    ws5.column_dimensions["C"].width = 70
    for cell in ws5[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E79")
    wb.save(path)
    return path


def export_jsonl(rows: list[dict], path: Path | None = None) -> Path:
    path = path or (config.EXPORT_DIR / f"posts_{datetime.now():%Y%m%d_%H%M}.jsonl")
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path
