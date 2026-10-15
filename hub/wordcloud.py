"""关键词云：从标题里抽高频词组，不装分词库。

为什么不用 jieba：环境里没有，且装了也要维护词典。这里用"n-gram + 停用词 + 长词优先"
的老办法——对"开屏广告、摇一摇、误触跳转、关不掉"这类固定搭配足够准，且零依赖。

做法：
1. 只取中文串和英文/数字 token，按标点切段（跨标点的 n-gram 没有意义）；
2. 段内生成 2/3/4-gram 计数（1 个字的词没有信息量，不要）；
3. 去停用词、去纯数字、去"广告"这类在本项目里出现在每条里的背景词（可配）；
4. 长词优先：如果 3-gram 的出现次数 ≥ 它包含的 2-gram 的 80%，就把短的那个丢掉，
   避免云里同时出现"摇一摇"和"一摇"。
"""
from __future__ import annotations

import re
from collections import Counter

# 分段：任何非中文、非字母数字的字符都当分隔符
_SEG = re.compile(r"[\u4e00-\u9fa5]+|[A-Za-z][A-Za-z0-9\.\-]{1,}")
# 中文停用词（虚词、代词、时间词、口语连接词）
STOP = set("""的 了 是 在 有 和 与 及 也 都 就 还 又 很 太 更 最 不 没 别 把 被 给 让 使
这 那 我 你 他 她 它 们 自己 什么 怎么 为什么 如何 哪些 哪个 多少 一个 一下 一样 一直
现在 目前 已经 还是 但是 而且 所以 因为 如果 可以 能够 需要 应该 可能 觉得 感觉 真的
今天 昨天 明天 今年 去年 每天 天天 时候 时间 问题 情况 方式 方法 内容 相关 以及 通过
用户 手机 软件 应用 功能 出现 进行 提示 显示 点击 打开 关闭 设置 更新 版本 系统""".split())
# 本项目的背景词：几乎每条都有。**先从文本里抹掉**再切 n-gram，否则会留下
# "开屏广""屏广告"这种碎片（它们的频次比真正的词还高，会把词云占满）
BACKGROUND = ["开屏广告", "摇一摇广告", "弹窗广告", "开屏", "广告", "APP", "app", "App"]
# 词内不允许出现的连接字：出现就说明这个 n-gram 跨了短语边界（"视频暂停和摇"）
BOUNDARY = set("的了和与在是就把被给让而及或对从并且也都还又很太更最不没别把很")
MIN_LEN, MAX_LEN = 2, 5


def _mask(text: str) -> str:
    for w in BACKGROUND:
        text = text.replace(w, " ")
    return text


def extract(rows: list[dict], top: int = 80, field: str = "title",
            drop_background: bool = True) -> list[dict]:
    """返回 [{name, value, posts}]，value 是出现次数，posts 是命中的帖子数。"""
    counter: Counter = Counter()
    posts: Counter = Counter()
    for r in rows:
        text = (r.get(field) or "")[:120]
        if drop_background:
            text = _mask(text)
        seen_in_post = set()
        for seg in _SEG.findall(text):
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9\.\-]+", seg):
                token = seg.lower()
                if len(token) >= 2:
                    counter[token] += 1
                    seen_in_post.add(token)
                continue
            n = len(seg)
            for size in range(MIN_LEN, min(MAX_LEN, n) + 1):
                for i in range(n - size + 1):
                    gram = seg[i:i + size]
                    if gram in STOP or any(c in BOUNDARY for c in gram):
                        continue
                    counter[gram] += 1
                    seen_in_post.add(gram)
        for g in seen_in_post:
            posts[g] += 1

    # 过滤：停用词、只出现一次的、以停用字开头或结尾的（多半是切歪的）
    cand = {}
    for gram, cnt in counter.items():
        if cnt < 2 or gram in STOP:
            continue
        if len(gram) >= 2 and (gram[0] in STOP or gram[-1] in STOP):
            continue
        cand[gram] = cnt

    # 长词优先：短词的量基本等于长词时，短词是长词的碎片，丢掉
    drop = set()
    for gram in sorted(cand, key=len, reverse=True):
        if len(gram) <= MIN_LEN:
            continue
        for size in range(MIN_LEN, len(gram)):
            for i in range(len(gram) - size + 1):
                sub = gram[i:i + size]
                if sub in cand and cand[sub] <= cand[gram] * 1.15:
                    drop.add(sub)
    out = [{"name": g, "value": c, "posts": posts.get(g, 0)}
           for g, c in cand.items() if g not in drop]
    out.sort(key=lambda x: (-x["value"], -len(x["name"])))
    return out[:top]
