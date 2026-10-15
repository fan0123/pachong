#!/usr/bin/env python3
"""舆情条目清洗与分层规则。

分层口径（analyze.py 依赖）：
  A_UGC负反馈    —— 用户自己在社交/社区平台的吐槽、投诉、受损体验陈述  ← 声量主口径
  B_媒体负面报道 —— 媒体/自媒体带批评倾向的报道、实测、追问            ← 声量主口径
  C_中性事件报道 —— 纯监管流程、政策通报、无负面情绪的客观叙述          ← 只做事件时间线，不计声量
  D_噪音         —— 教程/变现/技术/应援/百科/SEO 垃圾                  ← 剔除，单独留档

设计原则：噪音只"移出主口径"，不物理删除，全部带剔除原因落进 Excel 供抽检回溯。
"""
import re

# ---------------- D 层：硬噪音 ----------------

# 1) 变现/投放/商业侧：讲怎么靠开屏赚钱、报价、买量的
BIZ_WORDS = [
    "赚钱", "变现", "收益", "营收", "报价", "刊例", "CPM", "cpm", "CPC", "cpc", "eCPM", "ecpm",
    "ARPU", "arpu", "开户", "返点", "代运营", "广告代理", "招商", "加盟", "买量", "竞价",
    "投放策略", "投放技巧", "投放教程", "广告主必看", "流量主", "媒体主", "广告位出售",
    "广告位招商", "素材制作", "营销案例", "涨粉", "接单", "商单", "广告销售", "行业报告",
    "市场规模", "增长策略", "获客成本", "转化率优化", "出价",
]

# 2) 技术实现侧：讲怎么做一个开屏广告
TECH_WORDS = [
    "SDK", "sdk", "接入", "集成", "源码", "代码实现", "开发教程", "自定义View", "Flutter",
    "Unity", "Android开发", "iOS开发", "聚合广告", "adapter", "API接入", "组件封装",
    "穿山甲接入", "优量汇接入", "广告位创建", "开屏页开发",
]

# 3) 明星应援/品牌投放展示：买开屏而不是骂开屏
FAN_WORDS = [
    "应援", "打投", "生日会", "生日应援", "开屏祝福", "打卡应援", "路透", "预告",
    "代言", "官宣", "大片", "登上开屏", "上开屏", "开屏海报", "开屏物料", "开屏献礼",
    "超话打卡", "空降", "宣传照",
]

# 4) 百科/词条/科普定义
ENCYCLO_WORDS = ["百科", "词条", "是什么意思", "什么意思", "定义", "概念", "发展历程", "科普"]

# 5) 招聘培训等无关
MISC_WORDS = ["招聘", "求职", "面试题", "课程", "培训", "考试", "作业答案"]

# 教程类：必须是"意图词 + 动作词"同现，避免把"关不掉"这种吐槽误杀
HOWTO_INTENT = ["怎么", "如何", "怎样", "方法", "教程", "攻略", "技巧", "妙招", "指南",
                "步骤", "教你", "几步", "秒懂", "全解析", "详解", "设置", "操作指引",
                "解决办法", "解决方法", "亲测有效", "一招"]
HOWTO_ACTION = ["关闭", "屏蔽", "去掉", "取消", "跳过", "拦截", "禁止", "免广告", "去广告",
                "清除", "移除", "净化", "免打扰", "关掉"]
HOWTO_TOOLS = ["李跳跳", "GKD", "gkd", "Xposed", "LSPatch", "太极", "免root", "免ROOT",
               "adb", "ADB", "去广告软件", "广告过滤", "AdGuard", "adguard"]

# 平台本身即工具/文档属性，整体归噪音
NOISE_PLATFORMS = {"百度文库", "百度知道", "百度经验", "百科词条"}

# ---------------- A/B 层：负反馈证据 ----------------

NEG_EMOTION = [
    "烦", "恶心", "垃圾", "吐槽", "流氓", "离谱", "受不了", "骚扰", "差评", "坑", "难用",
    "过分", "服了", "管管", "没法用", "不想用", "一生黑", "缺德", "牛皮癣", "作妖", "反感",
    "厌恶", "气人", "恼火", "忍不了", "毁了", "恶意", "套路", "偷时间", "无语", "崩了",
    "太恶", "吃相", "膈应", "闹心", "糟心", "窒息", "折磨", "受够", "烦死", "讨厌", "反胃",
    "夺命", "劫持", "绑架", "强盗", "吸血", "毒瘤", "智商税", "退钱", "差劲", "破防",
    "不敢动", "不敢碰", "不敢", "手都不敢", "苦不堪言", "深恶痛绝", "怨声载道", "槽点", "劝退",
    "毫无底线", "没有下限", "太脏", "阴间", "恶臭", "无耻", "恶劣", "害人", "乱七八糟",
    "一堆广告", "满屏广告", "广告轰炸", "受害", "被迫", "无力", "抓狂",
    "不喜欢", "没人喜欢", "都不喜欢", "谁爱看", "没人爱看", "屡禁不止", "变态",
]
COMPLAINT_ACTION = [
    "投诉", "举报", "维权", "退款", "退订", "卸载", "弃用", "换回", "黑猫", "12315",
    "工信部举报", "曝光", "声讨", "抵制", "差评", "怒斥", "控诉", "谁来管", "求解决",
    "谁能管", "能不能管", "求整治", "呼吁", "求关闭", "让它消失", "能不能弄掉", "全都弄掉",
]
HARM_SCENE = [
    "误触", "误点", "误碰", "误入", "乱跳转", "自动跳转", "强制跳转", "强制", "直接跳",
    "跳到淘宝", "跳淘宝", "跳到", "直通", "手一抖", "手抖", "一抖就", "轻轻碰",
    "关不掉", "无法关闭", "关不了", "没有关闭", "假的关闭", "找不到跳过", "假关闭", "关不上",
    "摇一摇就跳", "晃一下就", "轻轻一晃", "一动就跳", "颠簸就跳", "走路就跳", "拿起手机就",
    "险些", "差点", "事故", "追尾", "危险", "分神", "安全隐患",
    "被扣费", "莫名扣费", "乱收费", "自动续费", "被开通", "被扣", "就扣", "扣了钱", "点到了",
    "会员还有", "开了会员还", "付费还有", "vip还有", "VIP还有", "充了会员", "躲不掉", "充钱也",
    "时间太长", "十几秒", "秒还没完", "看不完", "跳不过", "强制观看", "被迫看", "看广告",
    "打不开", "卡住", "白屏", "闪退", "偷跑流量",
]
# 隐含吐槽：UGC 里没有情绪词但语气明显不满的句式
IMPLICIT_NEG = [
    "也有开屏广告", "也开始", "又开始", "居然", "竟然", "越来越", "变成了", "开屏广告了",
    "什么时候能", "有必要吗", "能不能不", "为什么要", "怎么这么", "又多了", "开始搞",
    "忍一下", "只能装", "只好装", "不得不装",
]
# 讨论/质疑式：算入声量（用户在公开讨论这件负面事）
QUESTION_DISCUSS = ["如何看待", "怎么看", "为何", "为什么", "是不是", "算不算", "该不该"]
# 聚合页/个人主页/超话页：标题无实质内容，命中的情绪词来自页面其它区块
PAGE_JUNK = ["超话", "微博社区", "微博视频", "微博主页", "个人主页", "话题广场"]

# 新浪/搜狐等站点会在标题尾部拼关键词（"…?|收益|违规|系统"），判商业噪音前要剥掉
_TITLE_TAIL = re.compile(r"[|｜](?:[^|｜]{1,8})(?:[|｜][^|｜]{1,8})*\s*$")
# 媒体批评倾向词（B 层）
MEDIA_CRITIC = [
    "乱象", "屡禁不止", "死灰复燃", "又回来", "反弹", "整改", "实测", "调查", "追问",
    "质疑", "为何", "谁在", "该管", "顽疾", "困局", "轮回", "作秀", "阵风", "打折扣",
    "变本加厉", "花样", "套路", "劫持", "牛皮癣", "偷走", "受损", "侵害", "违规", "被指",
]
# C 层：纯监管/流程叙述
REGULATOR_WORDS = [
    "工信部", "信息通信管理局", "市场监管", "通报", "约谈", "下架", "专题会议", "自查整改",
    "规范", "标准", "管理办法", "征求意见", "新规", "条例", "白皮书", "座谈会",
]

MEDIA_PLATFORMS = {"新闻媒体", "科技媒体", "微信公众号"}

# 相关性：标题必须直指开屏/摇一摇场景；只写"广告"的走宽松通道（需摘要与负面证据双重确认）
STRICT_SCENE = ["开屏", "摇一摇", "闪屏", "启动页", "开机广告", "启动广告", "弹窗"]

# 新闻要素词：标题含这些时不判为教程（"客服回应可设置关闭"是报道不是教程）
NEWS_MARKERS = ["回应", "回复", "客服", "声明", "官方", "通报", "致歉", "承认", "被指",
                "记者", "实测", "报道", "调查", "曝", "热搜", "投诉", "工信部"]

# UGC 中性观察：实测/确认变化，无情绪，归 C 而不是噪音
NEUTRAL_OBSERVE = ["实测", "测试了", "体验", "消失了", "取消了", "没了", "对比", "复测",
                   "统计", "盘点", "汇总"]

# 反爬插入的干扰符号，匹配前统一剥掉
_JUNK_CHARS = re.compile(r"[\s|·．。，,、；;：:!！?？~～\-—_/\\【】\[\]()（）\"'“”‘’*#…]+")


def normalize_text(text: str) -> str:
    """剥掉分隔符与反爬插入的杂符号，避免『摇|一摇』这种被切断的词漏判。"""
    return _JUNK_CHARS.sub("", text or "")


def _hit(text: str, words: list[str]) -> list[str]:
    norm = normalize_text(text)
    return [w for w in words if w in text or normalize_text(w) in norm]


def scene_relevant(item: dict) -> tuple[bool, str]:
    """返回 (是否与开屏场景相关, 判定通道)。"""
    title = item.get("标题") or ""
    body = (item.get("摘要") or "")[:400]
    if _hit(title, STRICT_SCENE):
        return True, "标题直指开屏场景"
    if "广告" in normalize_text(title) and _hit(body, ["开屏", "摇一摇", "闪屏", "启动页"]):
        blob = f"{title} {body}"
        if _hit(blob, NEG_EMOTION) or _hit(blob, HARM_SCENE) or _hit(blob, COMPLAINT_ACTION):
            return True, "宽松通道（摘要确认开屏+负面证据）"
    return False, "与开屏场景无关"


def is_howto(title: str, body: str) -> bool:
    """标题层面判定教程类：意图词+动作词同现，或直接点名去广告工具。
    含新闻要素词（回应/客服/实测/通报…）、负面词或质问句式的标题不判教程，
    避免把『为什么各大 App 都上摇一摇？如何关闭』这类带声量的质问当纯教程剔掉。"""
    if _hit(title, HOWTO_TOOLS):
        return True
    if _hit(title, NEWS_MARKERS) or _hit(title, NEG_EMOTION) or _hit(title, QUESTION_DISCUSS):
        return False
    if _hit(title, HOWTO_INTENT) and _hit(title, HOWTO_ACTION):
        return True
    if re.search(r"[?？]\s*$", title.strip()) and _hit(title, HOWTO_ACTION) and not _hit(title, NEG_EMOTION):
        return True
    return False


def title_core(title: str) -> str:
    """去掉站点拼在标题尾部的关键词串，避免『|收益|违规』误触商业噪音规则。"""
    return _TITLE_TAIL.sub("", title or "")


def noise_reason(item: dict) -> str:
    """返回噪音原因；非噪音返回空串。判定只看标题 + 摘要前 200 字，避免正文长尾误杀。"""
    title = item.get("标题") or ""
    body = (item.get("摘要") or "")[:200]

    ok, channel = scene_relevant(item)
    if not ok:
        return "与开屏/摇一摇场景无关"
    if item.get("平台") in NOISE_PLATFORMS:
        return "文档/问答工具站（教程属性）"
    if is_howto(title, body):
        return "关闭/屏蔽教程类"
    core = title_core(title)
    protected = bool(_hit(title, MEDIA_CRITIC) or _hit(title, NEWS_MARKERS)
                     or _hit(title, NEG_EMOTION) or _hit(title, HARM_SCENE))
    for words, label in [
        (BIZ_WORDS, "广告变现/投放商业信息"),
        (TECH_WORDS, "广告技术实现类"),
        (FAN_WORDS, "明星应援/品牌开屏投放展示"),
        (ENCYCLO_WORDS, "百科/概念科普"),
        (MISC_WORDS, "招聘培训等无关信息"),
    ]:
        hits = _hit(core, words)
        if hits and not protected:
            return f"{label}（命中：{'/'.join(hits[:3])}）"
    # 聚合页/超话页/个人主页：标题无实质诉求，情绪词来自页面其它区块
    junk = _hit(title, PAGE_JUNK) or (["的微博"] if "的微博" in title and len(title) < 20 else [])
    if junk and not (_hit(title, HARM_SCENE) or _hit(title, COMPLAINT_ACTION)
                     or _hit(title, NEG_EMOTION)):
        return f"聚合页/主页无实质内容（命中：{junk[0]}）"
    # 摘要层面的商业噪音要更严格：需命中两个以上商业词
    biz_body = _hit(body, BIZ_WORDS)
    if len(biz_body) >= 2 and not protected:
        return f"广告变现/投放商业信息（摘要命中：{'/'.join(biz_body[:3])}）"
    return ""


def classify(item: dict) -> tuple[str, str]:
    """返回 (分层, 判定依据)。"""
    reason = noise_reason(item)
    if reason:
        return "D_噪音", reason

    title = item.get("标题") or ""
    blob = f"{title} {item.get('摘要') or ''}"
    neg = _hit(blob, NEG_EMOTION)
    act = _hit(blob, COMPLAINT_ACTION)
    harm = _hit(blob, HARM_SCENE)
    title_neg = _hit(title, NEG_EMOTION) + _hit(title, COMPLAINT_ACTION) + _hit(title, HARM_SCENE)
    implicit = _hit(title, IMPLICIT_NEG)
    question = _hit(title, QUESTION_DISCUSS)
    observe = _hit(title, NEUTRAL_OBSERVE)
    critic = _hit(blob, MEDIA_CRITIC)
    reg = _hit(blob, REGULATOR_WORDS)
    platform = item.get("平台") or ""

    evidence = []
    if neg:
        evidence.append("负面情绪=" + "/".join(neg[:3]))
    if act:
        evidence.append("投诉动作=" + "/".join(act[:3]))
    if harm:
        evidence.append("受损场景=" + "/".join(harm[:3]))
    strong = bool(act or harm)

    if platform in MEDIA_PLATFORMS:
        # 标题层出现负面/受损，或有具体受损事实 → 批评报道；只有监管流程叙述 → 中性事件
        if strong or title_neg:
            if critic:
                evidence.append("批评倾向=" + "/".join(critic[:3]))
            return "B_媒体负面报道", "；".join(evidence) or "标题负面=" + "/".join(title_neg[:3])
        if reg:
            return "C_中性事件报道", "监管流程叙述=" + "/".join(reg[:3])
        if evidence or critic:
            if critic:
                evidence.append("批评倾向=" + "/".join(critic[:3]))
            return "B_媒体负面报道", "；".join(evidence)
        return "D_噪音", "媒体条目但无负反馈证据"

    # UGC 平台：先看是否有负面表达，再看隐含吐槽、公开质疑，最后才是中性观察/监管转述
    if evidence:
        return "A_UGC负反馈", "；".join(evidence)
    if implicit:
        return "A_UGC负反馈", "隐含吐槽句式=" + "/".join(implicit[:3])
    if question:
        return "A_UGC负反馈", "公开质疑/讨论=" + "/".join(question[:2])
    if reg:
        return "C_中性事件报道", "监管流程转述=" + "/".join(reg[:3])
    if observe:
        return "C_中性事件报道", "中性实测/观察=" + "/".join(observe[:2])
    return "D_噪音", "UGC 条目但无负面表达证据"


MAIN_LAYERS = ("A_UGC负反馈", "B_媒体负面报道")
