"""语义裁决：规则判不出来的，交给大模型判「情绪 / 内容类型 / 竞品关系」。

为什么必须引入这一层：词表方法在这个场景已经到顶了。实测连续补了三轮词表，每轮都还有
新的漏网，而且漏的都是**必须理解句子意思才能判**的表达：

    「百度网盘你很缺💰吗」      —— 反讽指责，emoji 承载语义
    「百度网盘你有毒」          —— 网络化辱骂，没有任何"负面词"
    「百度网盘不能用了」        —— 陈述故障，隐含不满
    「百度网盘的广告为啥那么多」 —— 质问式抱怨
    「百度网盘吃像太难看」      —— 错别字（吃相→吃像），词表直接失配

再往词表里加词只是把下一批漏网推后一轮。所以这里让模型做**语义判定**，
但严格限定它的职责边界，沿用项目既有的分工原则：

  * 模型只判"这句话什么意思"，**不参与任何数字统计**（声量、影响力、覆盖率照旧由代码算）；
  * 只判**规则判不出的边界样本**，不全量覆盖——省 token，也保住规则那部分的可解释性；
  * 判定必须回带 `reason`，落库后人工能逐条复核；
  * 优先级 **人工改判 > 模型判定 > 规则判定**，三者来源在明细里分得清。

竞品关系是单独拎出来讲的一件事：用户的原话是「只有反馈为什么百度网盘开屏还投放了迅雷、
夸克等竞品才是竞品，不是提到百度网盘和夸克的关键词就变成竞品关系，有些人只是说想用成
夸克而已」。所以 prompt 里用对比样例把这条讲死——这是关键词共现永远做不到的判断。
"""
from __future__ import annotations

import json
import re
import time

BATCH = 25                  # 实测 25 条能完整回收（38.8s/批），15 条要 23.3s，25 更划算
WORKERS = 4                 # 并发批数。分类是只读任务，并发只受上游限流约束
# 每条喂多少字。**必须把正文一起喂进去**：只看标题会漏掉大量语境——
# 实测标题+正文合并长度中位数 153、p90 246，220 字会截断 27%（1180 条）的内容，
# 而正文比标题长的有 2663 条。600 字能完整覆盖 99.4%，只有 25 条超长需要截断。
MAX_TEXT = 600

SYSTEM = "你是舆情数据标注员。只输出 JSON 数组，不要任何解释、不要 markdown 代码块。"

# 兜底改判用的两条正则。分开写是因为它们回答的是两个不同问题：
#   _AD_MENTIONED —— 这条到底有没有在讲广告；
#   _AD_NEGATED   —— 讲到的"广告"是不是落在否定句里。
# 后者是用户指出的误判：「无乱七八糟广告，点开就能玩」是在推荐功能，
# 之前只用 "广告" in blob 判断，这种夸奖被当成了负反馈。
_AD_MENTIONED = re.compile(r"广告|广子|弹窗|开屏")
_AD_NEGATED = re.compile(
    r"(?:无|没有|没得|不见|去掉|去除|屏蔽了|关掉了|干净|清爽)"
    r"(?:各种|乱七八糟|那些|什么|任何|一堆)?(?:广告|广子|弹窗|开屏)"
    r"|(?:广告|广子|弹窗)(?:很少|不多|没有|全无)"
    r"|不用看(?:广告|广子)|免广告|无广"
)

ASK = """给「百度网盘开屏/跳转广告负反馈监控」做标注。逐条判断，严格输出 JSON 数组。

**每条给你的是「标题 + 正文」拼在一起的全文，必须结合整体判断，不要只看开头那句。**
标题常常只是个引子（"百度网盘你想咋的"），真正在抱怨什么、是不是在说广告、
是不是粉丝应援，往往要读到正文才清楚。标题与正文倾向不一致时以整体表达的意思为准。

每条返回：{"id":该条前面的序号数字（如 1、2、3，不要填内容原文）, "sentiment":"负面|中性|正面", "topic":"广告反馈|明星应援|营销活动|产品其他问题|无关", "competitor":true|false, "reason":"20字内依据"}

**sentiment 怎么判**：抱怨、质问、讽刺、辱骂、说产品变差、表达失望或诉求 → 负面。
反讽和网络黑话都算负面，例如「你很缺钱吗」「你有毒」「吃相难看」「不能用了」「为啥那么多」
都是负面。纯功能陈述、教程步骤、官方公告 → 中性。夸赞、说体验变好 → 正面。

**topic 怎么判**：
- 广告反馈：在说广告本身（太多/关不掉/误触/跳转/内容不适）
- 明星应援：粉丝为偶像的代言、超话、应援物料，情绪是冲着偶像的
- 营销活动：抽奖、优惠、会员促销、看广告领权益的攻略
- 产品其他问题：抱怨速度、容量、限速、登录、涨价等**非广告**问题
- 无关：**跟广告完全没关系**才填这个（比如「孔雀开屏式追妻」这种修辞、纯资源分享）。
  注意：**只要在吐槽广告，就算广告反馈，不要因为它吐槽的不是百度网盘的广告就填"无关"**。
  吐槽手机弹窗、导航开屏、别家 App 的广告，同样是这个议题的声量，一律填"广告反馈"。

**再强调一遍口径**：只要全文提到广告，**并且是在抱怨这个广告**，
topic 就填"广告反馈"、sentiment 就填"负面"。反讽尤其要注意——
「不得不夸百度的用户服务做得无可挑剔，专门推出SVIP增值广告套餐」这种通篇夸的写法
其实是骂得最狠的，别被字面意思骗了。

**但下面五类不算负反馈，判错的比漏判的更麻烦，逐条对照着排除：**

1）**"没有广告"是夸，不是骂。** 广告一词出现在否定句里，说明作者在推荐、在满意：
   中性/正面 ← 「里面超多正版游戏，不占内存，无乱七八糟广告，点开就能玩」
   中性/正面 ← 「没有广告」「不用看广告」「广告很少」「终于没广告了」
   这类 topic 填"无关"或"产品其他问题"，**绝不能因为出现"广告"两个字就判负面**。

2）**先看是不是明星/饭圈内容，是的话再判情绪。** 出现偶像名、超话、应援、打投、
   代言、物料、生日站、专辑、直播打榜等信号，就按饭圈逻辑读：
   粉丝夸自家哥哥的开屏、晒应援广告牌 → 正面或中性，topic 填"明星应援"；
   只有粉丝自己在骂"别再出这个开屏了""这广告好恶心"才算负面。
   **注意情绪的主人是谁**：「你说你讨厌时XX」是在转述/反驳别人的话，
   讨厌的不是发帖人；这种一律不算发帖人的负面。
   还有一种**反着说的炫耀**，最容易判错，务必按明星应援处理：
   「这下好了，你说你讨厌时代少年团，但现在满大街都是他们的地广，打开百度网盘
   弹窗又是他们，已深度入侵日常生活，实现国民全覆盖，咋办👀」
   —— 这是粉丝在得意地夸偶像无处不在，讨厌的是"你"不是发帖人，
   topic 填"明星应援"、sentiment 填"正面"或"中性"，**绝不是广告负反馈**。
   开头单独一个「投」字、带"早午间投""晚投""💳"这类，是打投贴，同样按明星应援处理。

3）**卖货、促销、拉人头的一律剔除。** 卖 SVIP／超级会员／年卡／兑换码／
   低价开会员／带推广链接或口令／抽奖领福利／"日白嫖"这类，
   topic 填"营销活动"、sentiment 填"中性"，**不管文案里有没有"广告"两个字**。

4）**长文只是顺带提一句，不算在骂。** 知乎/公众号那种长篇，如果通篇在讲别的
   （网盘选型、教程、行业分析），只是其中一句提到百度网盘或它的广告，
   sentiment 填"中性"。只有**整篇的主要矛盾就是广告**时才填负面。

5）**判的是发帖人的真实意图**，不是关键词。转述新闻、复述别人吐槽、
   客观罗列各家网盘优缺点，都算中性。

**competitor 怎么判**（只有一种情况是 true）：
内容在说【百度网盘自己的广告位投放了竞品的广告】。
  true  ← 「为什么百度网盘开屏还给夸克投广告」「网盘开屏居然是迅雷的广告」
  false ← 「百度网盘太慢了我想换成夸克」（用户想迁移，不是投放关系）
  false ← 「夸克比百度网盘好用」（对比评价）
  false ← 「夸克网盘链接分享」（只是提到别家产品）
只要没明说"网盘的广告位出现了竞品"，一律 false。

待标注内容：
"""


def _pick_all(rows: list[dict]) -> list[dict]:
    """全量模式：除了人工已定和判过的，其余都送模型。

    为什么要有这个模式：规则判"负面"的那些也可能判错——饭圈文案里的「居然」「越来越」、
    横评里的「离谱」都会误命中。只补规则的盲区（boundary 模式）解决不了这一类，
    要"准"就得全量重判，让语义判定覆盖规则的**误判**，而不只是**漏判**。
    """
    out = []
    for r in rows:
        if (r.get("manual_sentiment") or "").strip():
            continue
        if r.get("ai_at"):
            continue
        text = f"{r.get('title') or ''} {r.get('content') or ''}"
        if "网盘" not in text and "百度" not in text and "广告" not in text:
            continue                    # 跟议题完全无关的不浪费调用
        out.append(r)
    return out


def _pick(rows: list[dict]) -> list[dict]:
    """挑边界样本：规则给不出情绪证据、但明显是主观表达的 UGC。

    只挑这些是刻意的——规则已经判明白的不重判，避免模型把稳定结论搅乱，
    也避免为了"全量覆盖"烧掉大量 token。
    """
    out = []
    for r in rows:
        if (r.get("manual_sentiment") or "").strip():
            continue                        # 人工已定，模型不插手
        if r.get("ai_at"):
            continue                        # 判过就不重判（改 prompt 后想重判用 --force）
        text = f"{r.get('title') or ''} {r.get('content') or ''}"
        if "网盘" not in text and "百度" not in text and "广告" not in text:
            continue
        reason = r.get("sentiment_reason") or ""
        weak = ("无明显情感倾向" in reason or "流程/事实叙述" in reason
                or r.get("layer") == "D_噪音")
        if weak:
            out.append(r)
    return out


def _parse(txt: str) -> list[dict]:
    """模型偶尔会包 markdown 或前后加话，这里只捞 JSON 数组。"""
    s = (txt or "").strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        data = json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)]


def _one_batch(cfg: dict, chunk: list[dict]) -> dict:
    """判一批，返回 {platform|post_id: verdict}。异常向上抛给调用方计失败数。"""
    from . import llm

    lines = []
    for n, r in enumerate(chunk, 1):
        title = re.sub(r"\s+", " ", r.get("title") or "").strip()
        body = re.sub(r"\s+", " ", r.get("content") or "").strip()
        # 正文与标题相同时（小红书 38% 的条目就是这样）不重复喂，省 token
        if body and body != title:
            t = f"【标题】{title}【正文】{body}"
        else:
            t = f"【标题】{title}（无独立正文）"
        lines.append(f"{n}. {t[:MAX_TEXT]}")
    raw = llm.chat(cfg, [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": ASK + "\n".join(lines)}],
                   temperature=0.1, max_tokens=2800)
    got = _parse(raw if isinstance(raw, str) else str(raw))
    out = {}
    for pos, d in enumerate(got):
        raw_id = str(d.get("id") or "").strip()
        m = re.match(r"^(\d+)", raw_id)
        idx = int(m.group(1)) - 1 if m else pos
        if not 0 <= idx < len(chunk):
            continue
        r = chunk[idx]
        sent = (d.get("sentiment") or "").strip()
        topic = (d.get("topic") or "").strip()
        reason = (d.get("reason") or "").strip()[:60]
        # 确定性兜底：用户定的口径是「只要提到广告且带吐槽/反馈情绪，就算广告问题」。
        # 模型仍会时不时把"吐槽别家广告"判成"无关"（实测 452 条），所以这条不靠模型自觉，
        # 在这里强制纠正——规则能确定的事就不该交给概率。
        #
        # 但兜底本身也会误伤，用户点出三类：饭圈应援、卖会员的营销、以及「无广告」这种
        # 否定式提及。所以先做排除，再决定要不要改判：
        #   * 模型已经判成明星应援/营销活动的，尊重它，不再往广告反馈上拽；
        #   * 文本里的"广告"落在否定句里（无广告/没有广告/不用看广告），那是夸不是骂。
        blob = f"{r.get('title') or ''} {r.get('content') or ''}"
        if topic in ("明星应援", "营销活动"):
            # 这两类按口径直接剔除，情绪一律不算负面——分层会随之落到噪音层
            if sent == "负面":
                sent = "中性"
                reason = (reason + f"｜按口径剔除：{topic}不计负反馈")[:60]
        elif sent == "负面" and _AD_MENTIONED.search(blob) and not _AD_NEGATED.search(blob):
            if topic in ("无关", "产品其他问题", ""):
                topic = "广告反馈"
                reason = (reason + "｜按口径修正：提到广告且为负面反馈")[:60]
        out[f"{r['platform']}|{r['post_id']}"] = {
            "sentiment": sent, "topic": topic,
            "competitor": bool(d.get("competitor")), "reason": reason,
        }
    return out


def judge(rows: list[dict], model_id: str = "", on_log=None, scope: str = "boundary",
          workers: int = WORKERS, persist=None) -> dict:
    """批量语义裁决。

    scope="boundary" 只补规则盲区；scope="all" 全量重判（规则误判也一起纠）。
    persist 是个回调，每批完成就在**主线程**里落库——这样中途失败已判的不丢，
    也避免多线程同时写 SQLite。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from . import llm

    def log(msg):
        print(msg, flush=True)
        if on_log:
            on_log(msg)

    cfg = llm.get_model(model_id) if model_id else None
    if cfg is None:
        cfg = llm.get_model(llm.list_models(safe=False).get("default") or "")
    if cfg is None:
        return {"ok": False, "msg": "没有可用模型，先在「分析报告」页配一个"}

    pool = _pick_all(rows) if scope == "all" else _pick(rows)
    chunks = [pool[i:i + BATCH] for i in range(0, len(pool), BATCH)]
    log(f"[语义裁决] 模式={scope} 待判 {len(pool)} 条 / {len(chunks)} 批 / 并发 {workers} / "
        f"模型 {cfg.get('name')}")
    done = fails = saved = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one_batch, cfg, ch): i for i, ch in enumerate(chunks)}
        for fut in as_completed(futs):
            done += 1
            try:
                verdicts = fut.result()
            except Exception as exc:
                fails += 1
                log(f"  [{done}/{len(chunks)}] 失败：{exc}"[:150])
                continue
            if persist and verdicts:
                saved += persist(verdicts)
            el = time.time() - t0
            eta = el / done * (len(chunks) - done)
            log(f"  [{done}/{len(chunks)}] 回收 {len(verdicts)} 条，累计入库 {saved}，"
                f"已用 {el/60:.1f} 分，预计还需 {eta/60:.1f} 分")
    return {"ok": True, "total": len(pool), "judged": saved,
            "failed_batches": fails, "minutes": round((time.time() - t0) / 60, 1)}


def save_verdicts(verdicts: dict) -> int:
    """把模型判定写进 ai_* 列。返回写入条数。"""
    from . import store

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with store.conn() as c:
        for key, v in verdicts.items():
            platform, _, post_id = key.partition("|")
            c.execute("UPDATE posts SET ai_sentiment=?, ai_topic=?, ai_competitor=?,"
                      " ai_reason=?, ai_at=? WHERE platform=? AND post_id=?",
                      (v["sentiment"], v["topic"], 1 if v["competitor"] else 0,
                       v["reason"], now, platform, post_id))
            n += c.total_changes and 1 or 1
    return len(verdicts)
