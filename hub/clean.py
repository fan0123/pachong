"""清洗分层：直接复用 kaiping_sentiment/clean.py 的四层规则，不在这里重复维护词表。

那套词表是按真实误杀样本调过的（NEWS_MARKERS 守卫、title_core 去尾巴、
normalize_text 去反爬分隔符等），复制一份必然走样，所以按文件路径动态加载。
拿不到那个文件时退化成"不分层"，宁可不判也不要瞎判。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

# 词表文件按候选路径找，第一个存在的生效：
#   1) 开发机：kaiping_sentiment 是 crawler_hub 的兄弟目录，用原文件，改词表立刻生效；
#   2) 分发包：那个兄弟目录不会被打进包，所以 package_share.py 会把词表拷成
#      crawler_hub/rules/kaiping_clean.py。**这条不能少**——少了它别人拿到包以后
#      classify_full() 会一路返回「未加载清洗规则」，layer 全空，
#      于是口径筛选、声量换算、事件归因、重点预警全部失效，而且页面不会报错，
#      只表现为"看板什么都没有"。这个坑在只验证「服务能启动」时发现不了。
_ROOT = Path(__file__).resolve().parent.parent
CLEAN_CANDIDATES = [
    _ROOT.parent / "kaiping_sentiment" / "clean.py",
    _ROOT / "rules" / "kaiping_clean.py",
]
CLEAN_PY = next((p for p in CLEAN_CANDIDATES if p.exists()), CLEAN_CANDIDATES[0])

# clean.py 只用「平台」区分 UGC / 媒体两条判定分支：
# 百家号、头条号上的内容以媒体号稿件为主，按媒体口径判；个人创作者会被一起算进去，
# 这是已知的简化，判定依据列里能看出来。
MEDIA_LIKE = {"baijiahao": "新闻媒体", "toutiao": "新闻媒体", "weixin": "微信公众号"}

# "广告"落在否定句里 —— 这是在夸产品干净，不是在骂广告多。
# 判定层跟 judge._AD_NEGATED 保持同一套口径，两边都要拦：
# 规则层不拦，新采到的数据在送模型之前就已经被打成负反馈了。
AD_NEGATED_RE = re.compile(
    r"(?:无|没有|没得|不见|去掉|去除|屏蔽了|关掉了|干净|清爽)"
    r"(?:各种|乱七八糟|那些|什么|任何|一堆)?(?:广告|广子|弹窗|开屏)"
    r"|(?:广告|广子|弹窗)(?:很少|不多|没有|全无)"
    r"|不用看(?:广告|广子)|免广告|无广"
)

_mod = None
_loaded = False


def module():
    global _mod, _loaded
    if not _loaded:
        _loaded = True
        if CLEAN_PY.exists():
            spec = importlib.util.spec_from_file_location("kp_clean", CLEAN_PY)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _mod = m
    return _mod


def available() -> bool:
    return module() is not None


def main_layers() -> list[str]:
    m = module()
    return list(m.MAIN_LAYERS) if m else []


def all_layers() -> list[str]:
    return ["A_UGC负反馈", "B_媒体负面报道", "C_中性事件报道", "D_噪音", "E_正面反馈"]


# ---------------------------------------------------------------------------
# 场景相关性的本地补充
#
# 共用词表里的 STRICT_SCENE 只有 开屏/摇一摇/闪屏/启动页/开机广告/启动广告/弹窗 七个词，
# 是按"开屏广告"这一个议题调的。关键词换成「百度网盘广告 / 百度网盘自动跳转」之后，
# 大量真实反馈因为标题里没有那七个词，被判成「与开屏/摇一摇场景无关」直接进了噪音——
# 实测 2133 条噪音里有 1897 条（89%）都是这个原因，其中「每次打开百度网盘都是美团广告，
# 能不能管管」「百度网盘绝对有病 我切一次后台再点进去非要显示广告」这种明显是负反馈。
#
# 所以这里加两条救回通道，都**只看标题**（跟共用规则同级，正文只做辅助确认），
# 并且都要求已经抽出了负面证据——没有情绪/诉求证据的还是留在噪音里，
# 否则"手机弹广告怎么关"这类教程会被整批捞回来。
SCENE_TITLE_EXTRA = [
    # 跳转类：网盘/地图这类反馈最常见的说法，共用词表完全没有
    "跳转", "乱跳", "自动跳", "偷偷跳", "自己跳", "跳一跳", "跳个不停", "点了就跳",
    # 摇晃类：摇一摇的各种叫法
    "晃一晃", "扭一扭", "摇晃", "晃动", "倾斜",
    # 广告形态
    "启屏", "首屏", "弹广告", "弹出广告", "弹个不停", "悬浮窗", "浮窗", "插屏",
    "全屏广告", "广告乱", "广告多",
    # 关不掉类：这是负反馈里最集中的诉求
    "关不掉", "关不了", "无法关闭", "跳过按钮", "关闭按钮", "倒计时", "叉号",
    "一打开就", "打开就弹", "开机就", "误触", "误点",
]
# 被监控的自家产品：标题点名产品 + 内容提到广告，就算议题内（哪怕没有任何场景词）
# 繁体写法要一起放：港台用户的「惡心的百度地圖，廣告又回來了」实测因为词表全是简体而漏判。
# 没引繁简转换库（多一个依赖不值），只把高频写法并进来。
PRODUCT_TITLE = ["网盘", "百度网盘", "度盘", "百度云", "百度地图", "百度APP", "百度app",
                 "網盤", "百度網盤", "百度地圖", "百度雲"]
SCENE_BODY_EXTRA = ["开屏", "摇一摇", "闪屏", "启动页", "跳转", "跳一跳", "弹窗", "弹广告",
                    "误触", "关不掉"]
# 网盘/资源类搜索必然带回来的引流软文：解锁版、提取码、拉新推广。
# 这类经常同时含产品词和"广告"，不排掉的话会被上面的通道大量误救。
DRAIN_MARK = ["解锁", "破解", "绿色版", "直装", "内购", "会员共享", "共享账号", "提取码",
              "资源分享", "网盘链接", "夸克网盘", "拉新", "推广赚", "代下", "免费领取会员",
              "永久会员", "低价会员", "开通教程", "教你开通"]


def _hit_local(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w in (text or "")]


def scene_rescue(title: str, content: str) -> str:
    """共用规则判「场景无关」时，用本地更宽的场景词表复核一次。

    返回非空字符串＝可以救回（顺带说明凭什么救的，会写进 layer_reason 便于人工复核）；
    返回空串＝维持噪音判定。
    """
    title = title or ""
    body = (content or "")[:400]
    blob = f"{title} {body}"
    if _hit_local(title, DRAIN_MARK):
        return ""                       # 引流软文，不救
    ad_ctx = any(w in blob for w in ("广告", "广子", "廣告"))
    # 通道 A：标题出现扩充场景词。这里必须同时确认在讲广告，否则
    # 「手机端死活无法跳转到支付页面」这类非广告的跳转故障会被误救进来。
    hits = _hit_local(title, SCENE_TITLE_EXTRA)
    if hits and ad_ctx:
        return f"标题命中扩充场景词：{'/'.join(hits[:3])}"
    # 通道 B：标题点名了被监控的产品。
    # 这里**不再强求出现"广告"三个字**——采集侧的相关性闸门已经确认过内容跟搜索词
    # （百度网盘广告 / 百度网盘自动跳转）相关，而 UGC 标题往往极短，
    # 「百度网盘是真的恶心」「坑爹的百度网盘」这种连正文都没有，卡"广告"字样等于继续误杀。
    # 是否明确涉广告写进理由里，人工在明细里能一眼区分，不用猜。
    prod = _hit_local(title, PRODUCT_TITLE)
    if prod:
        kind = "涉广告" if (ad_ctx or _hit_local(body, SCENE_BODY_EXTRA)) else "产品负反馈"
        return f"标题点名监控产品「{prod[0]}」（{kind}）"
    return ""


# 话题标签、@、URL、emoji 都不承载判断所需的信息，算"有效字数"时要先剥掉
_NOISE_CHARS = re.compile(r"#[^#]{1,24}#|@[\w\-]+|https?://\S+|[\U0001F300-\U0001FAFF"
                          r"\u2600-\u27BF\uFE0F\u200d]")


def text_thin(title: str, content: str) -> tuple[bool, str]:
    """文本信息是否太少，判断只能靠猜。

    小红书大量内容的情绪其实在图片里，标题只留一句「百度网盘你想咋的」，正文与标题相同。
    这类**不该默默判成中性噪音**——那等于假装判准了。显式标出来交人工复核，
    把不确定性摆在台面上比给一个假结论有用。
    """
    title = title or ""
    body = (content or "").strip()
    merged = title if body == title.strip() else f"{title} {body}"
    core = _NOISE_CHARS.sub("", merged).strip()
    core = re.sub(r"[\s，。！？、,.!?~…·\-—_/\\【】\[\]()（）\"'“”‘’*]+", "", core)
    if len(core) <= 14:
        return True, f"文本信息不足（有效字数 {len(core)}，情绪可能在图片里），建议人工复核"
    return False, ""


def is_emergency(title: str, content: str) -> list[str]:
    """应急场景 **且** 在讲广告，才给"绝不判噪音"的硬保护。

    起因是「报警遇上开屏广告，危急时刻延误半分钟」被判成了「明星应援」噪音——
    因为"遇上开屏"里含子串"上开屏"，而它在饭圈词表里。这种漏判在舆情场景里代价最大。
    但只看应急词会误保护「地震预警冒用发布」「医院搬迁」这类无关新闻（实测 85 条），
    所以必须叠加广告语境。"""
    from . import tagging
    blob = f"{title or ''} {(content or '')[:300]}"
    if not any(w in blob for w in ("广告", "广子", "开屏", "弹窗", "跳转")):
        return []
    # 应急词只认标题：正文里偶然出现「120」「报警」会把横评/教程也保护进来
    return _hit_local(title or "", tagging.EMERGENCY_WORDS)


def fan_verdict(title: str, content: str, neg: list[str]) -> tuple[str, str]:
    """饭圈/代言类内容的三分裁决，返回 (处置, 说明)。

    共用规则把所有含"应援/代言/大片/上开屏"的内容一律当噪音，但实测这批里混着三种东西：
      1. 粉丝夸代言开屏好看（"微博开屏好惊艳的肖战""杨幂的开屏好美"）→ 这是正面口碑；
      2. 粉丝/用户反感（"摇一摇广告能不能滚出异世界啊""又来这招，又不把XX当回事"）
         → 这是负反馈，之前被"应援""代言"两个词直接吞掉；
      3. 纯投放展示与打卡预告（"XX官宣代言，微博开屏火速安排"）→ 才是真噪音。
    所以有负面证据先算负面，其次看粉丝夸赞词，都没有才留噪音。
    """
    from . import tagging

    # 优先级：强负面 > 粉丝夸赞 > 弱负面 > 噪音。
    # 为什么夸赞要压过弱负面：饭圈文案里「跳转到小程序」「商业价值越来越高」「曝光」
    # 这些词在共用词表里算负面证据，但在这儿是中性描述甚至是夸——实测
    # 「微博开屏好惊艳的肖战…太权威了」就是因为"跳转到"被判成了负反馈。
    STRONG = ("驱逐/咒骂", "失望/被无视", "激烈表达", "取消诉求", "唱衰/反讽")
    strong = [e for e in neg if any(k in e for k in STRONG)]
    if strong:
        return "neg", "饭圈内容但有强负面证据：" + "；".join(strong[:2])
    pos = _hit_local(title or "", tagging.FAN_POS_WORDS)
    if pos:
        return "pos", f"粉丝对代言/开屏内容的正向评价：{'/'.join(pos[:3])}"
    if neg:
        return "neg", "饭圈内容但有负面证据（无夸赞词）"
    return "noise", ""


def negative_evidence(m, title: str, content: str) -> list[str]:
    """从复用的词表 + 本地补充词表里抽负面证据，按可信度从高到低。

    两条防"乱归因"的规则：
    1) MEDIA_CRITIC 里有"整改/违规/调查/实测"这类监管通报天天出现的词，单命中一个就判负面
       会把中性通报大面积误判，所以标题命中才算、正文要命中两个以上；
    2) tagging.AMBIG_NEG 里的词（直接跳 / 看广告 / 强制 …）在教程和软文里同样高频，
       只有出现在标题里才当证据，否则"手机记账软件挑选指南"会因为正文一句"直接跳过广告"
       被判成负面。

    还有一条前置否决：**"广告"出现在否定句里时不算证据**。
    「无乱七八糟广告，点开就能玩」是在推荐这个功能，作者满意得很，
    之前这类被判成负反馈——它命中的其实是"广告"这个词，不是任何不满情绪。
    """
    from . import tagging

    title = title or ""
    body = (content or "")[:400]
    blob = f"{title} {body}"
    if AD_NEGATED_RE.search(blob) and not any(
            w in blob for w in tagging.EXTRA_NEG_SLANG + tagging.EXTRA_NEG_DRIVE):
        # 只在"没有强烈情绪词"时否决：万一是「无广告个鬼，一天弹十次」这种，
        # 强烈表达仍然要保住负面判定。
        return []

    def keep(hits: list[str]) -> list[str]:
        """多义词的取舍：标题命中直接算；只在正文出现时要求**至少两个**多义词同时命中。

        原来是"只认标题"，正文一律不算——好处是不会把教程误判成负面，坏处是
        用户把抱怨写在正文里（标题只写「百度网盘你想咋的」）就完全读不到。
        改成"正文需多处命中"是个折中：单个词偶然出现不算，两个以上才认，
        跟词表里 MEDIA_CRITIC「正文要命中两个以上」的思路一致。
        """
        amb_in_body = [w for w in hits if w in tagging.AMBIG_NEG
                       and w not in title and tagging._norm(w) not in tagging._norm(title)]
        body_ok = len(amb_in_body) >= 2
        out = []
        for w in hits:
            if w not in tagging.AMBIG_NEG:
                out.append(w)
            elif w in title or tagging._norm(w) in tagging._norm(title):
                out.append(w)
            elif body_ok:
                out.append(w)
        return out

    out = []
    for words, label in [(m.NEG_EMOTION, "负面情绪"), (m.COMPLAINT_ACTION, "投诉动作"),
                         (m.HARM_SCENE, "受损场景"),
                         (tagging.EXTRA_NEG_SLANG, "激烈表达"),
                         (tagging.EXTRA_NEG_DEMAND, "取消诉求/质问"),
                         (tagging.EXTRA_NEG_HARM, "受损场景"),
                         (tagging.EXTRA_NEG_MOCK, "唱衰/反讽"),
                         (tagging.EXTRA_NEG_DRIVE, "驱逐/咒骂"),
                         (tagging.EXTRA_NEG_LETDOWN, "失望/被无视")]:
        hits = keep(m._hit(blob, words))
        if hits:
            out.append(f"{label}=" + "/".join(hits[:3]))
    critic_title = m._hit(title, m.MEDIA_CRITIC)
    critic_body = m._hit(body, m.MEDIA_CRITIC)
    if critic_title:
        out.append("批评倾向=" + "/".join(critic_title[:3]))
    elif len(critic_body) >= 2:
        out.append("批评倾向（正文多处）=" + "/".join(critic_body[:3]))
    if not out:
        # 没有显式负面词时才看隐含句式与公开质疑，避免抢掉更明确的证据
        implicit = m._hit(title, m.IMPLICIT_NEG)
        if implicit:
            out.append("隐含吐槽句式=" + "/".join(implicit[:2]))
        else:
            question = m._hit(title, m.QUESTION_DISCUSS)
            if question and m._hit(title, m.STRICT_SCENE):
                out.append("公开质疑=" + "/".join(question[:2]))
    return out


def classify_full(platform: str, platform_label: str, title: str, content: str,
                  author: str = "", metrics: dict | None = None,
                  extra: dict | None = None) -> dict:
    """主体 × 情感 × 相关性 三个判断一起做，并折算出兼容老口径的 layer。

    主体判定不依赖规则文件（只看作者名和影响力），所以即便 kaiping_sentiment/clean.py
    不在，主体维度也照样可用；情感与噪音判定才需要那份词表。
    """
    from . import tagging

    metrics = metrics or {}
    extra = extra or {}
    actor, actor_reason = tagging.detect_actor(platform, author, metrics, extra)
    m = module()
    if m is None:
        return {"layer": "", "layer_reason": "未加载清洗规则", "actor": actor,
                "actor_reason": actor_reason, "sentiment": "", "sentiment_reason": "",
                "topic_kind": "", "topic_reason": "", "text_thin": 0, "thin_reason": ""}
    item = {"平台": MEDIA_LIKE.get(platform, platform_label),
            "标题": title or "", "摘要": content or ""}
    try:
        noise = m.noise_reason(item)
        rescued = ""
        fan_pos = ""          # 粉丝夸代言：要强制成正面，不能走默认的"无情感倾向→中性"
        neg = negative_evidence(m, title, content)
        strong_neg = any(k in e for e in neg
                         for k in ("取消诉求", "激烈表达", "投诉动作", "负面情绪"))
        # 先判"这条在说什么事"，再判正负。饭圈/营销/修辞内容里的情绪词是冲着偶像、
        # 联名、剧情说的，按广告反馈判必然归错——详见 tagging.detect_topic 的注释。
        topic, topic_reason = tagging.detect_topic(title, content)
        thin, thin_why = text_thin(title, content)
        # 冲着广告骂的强证据：只有这类能把饭圈/营销内容翻成负反馈
        AD_ANGRY = ("驱逐/咒骂", "失望/被无视", "取消诉求", "唱衰/反讽")
        ad_angry = [e for e in neg if any(k in e for k in AD_ANGRY)]
        if topic == "修辞误召回":
            noise = topic_reason
        elif topic == "明星应援":
            verdict, why = fan_verdict(title, content, neg)
            if verdict == "pos":
                noise, rescued, fan_pos = "", why, why
            elif ad_angry:
                noise, rescued = "", f"{topic_reason}，但有冲着广告的强负面：{ad_angry[0]}"
            else:
                # 粉丝场里的"居然/越来越/不喜欢"是在说偶像和剧情，不是广告反感
                noise = f"明星应援（{topic_reason}）"
        elif topic == "营销活动":
            if ad_angry:
                noise, rescued = "", f"{topic_reason}，但有冲着广告的强负面：{ad_angry[0]}"
            else:
                noise = f"营销活动/权益攻略（{topic_reason}）"
        # ↓ 噪音复核必须放在情感判定之前：饭圈通道会把"粉丝夸代言"强制改成正面，
        #   顺序颠倒的话 fan_pos 还是空的，改判就失效了（第一版就踩了这个）
        if noise and is_emergency(title, content):
            # 应急场景硬保护：报警/报火警/急救被广告挡住，绝不能剔成噪音
            noise = ""
            rescued = "应急场景（报警/急救类）不判噪音"
        if noise.startswith("与开屏") and neg and topic == "广告反馈":
            # 产品通道只对"真的在讲广告反馈"的内容开放：饭圈/营销内容走上面的分支，
            # 不能让它们靠"标题点名网盘"被捞成负反馈（这是本轮误判的主要来源）
            # 场景词表是按"开屏广告"调的，换关键词后会大面积误杀，见 scene_rescue 注释
            rescue = scene_rescue(title, content)
            if rescue:
                noise = ""
                rescued = rescue
        if noise and "教程" in noise and strong_neg:
            # "关闭摇一摇广告真的很难吗"这种带情绪的求助，共用词表按教程剔掉了；
            # 有明确诉求/情绪证据时按负反馈保留，否则会把真实抱怨当噪音抹掉
            noise = ""
        sentiment, sent_reason = tagging.detect_sentiment(title, content, neg)
        if fan_pos and sentiment != "正面":
            # 粉丝夸开屏好看：POS_WORDS 里全是"广告被治理"的表达，认不出这类，单独覆盖。
            # 注意要连"负面"一起盖掉——fan_verdict 已经确认过没有强负面证据，
            # 剩下的「跳转到小程序」「商业价值越来越高」「曝光」在饭圈文案里都不是抱怨。
            sentiment, sent_reason = "正面", fan_pos
        if not noise and not neg:
            promo = tagging._hit(title or "", tagging.PROMO_MARK)
            if promo:
                # 横评/软文（"无广告APP推荐""XX测评"）：场景词命中但不是舆情，单独剔除
                noise = f"软文/横向推荐（命中：{promo[0]}）"
        layer, layer_reason = tagging.derive_layer(actor, sentiment, not noise, noise)
        if not noise:
            layer_reason = f"{layer_reason}｜{sent_reason}"
            if rescued:
                # 写清"本来会被判噪音、凭什么救回来"，方便在明细里逐条复核这条规则
                layer_reason = f"{layer_reason}｜场景复核：{rescued}"
        return {"layer": layer, "layer_reason": layer_reason, "actor": actor,
                "actor_reason": actor_reason, "sentiment": sentiment,
                "sentiment_reason": sent_reason,
                "topic_kind": topic, "topic_reason": topic_reason,
                "text_thin": 1 if thin else 0,
                "thin_reason": thin_why}
    except Exception as exc:            # 词表出错不能连带把采集搞挂
        return {"layer": "", "layer_reason": f"分层失败：{exc}"[:120], "actor": actor,
                "actor_reason": actor_reason, "sentiment": "", "sentiment_reason": "",
                "topic_kind": "", "topic_reason": "", "text_thin": 0, "thin_reason": ""}


def classify(platform: str, platform_label: str, title: str, content: str):
    """兼容旧签名：只返回 (分层, 判定依据)。"""
    r = classify_full(platform, platform_label, title, content)
    return r["layer"], r["layer_reason"]
