"""大模型接入：填入 base_url + api_key + 模型名就能用，走 OpenAI 兼容的
`/chat/completions` 协议——这是各家（硅基流动、智谱、Moonshot、百炼、DeepSeek、
OpenRouter、ModelScope、本地 Ollama）唯一都认的协议，切换只需要改 base_url 和 model。

设计取舍：
1. 不装 openai SDK，用标准库 urllib 直接发 HTTP。少一个依赖，也少一层版本兼容问题；
2. 模型清单（含 key）落盘在 data/llm.json，重启后还在——用户明确要求"记忆保留填入的模型"；
3. 对外返回的 key 一律打码，只有真正发请求时才读明文；
4. 报告生成失败时把上游返回的错误原文带出来（限长），不吞异常——否则填错 key
   只会看到"生成失败"，没法排查。

参考：songquanpeng/one-api、BerriAI/litellm 的做法都是把各家收敛成 OpenAI 协议，
本项目只需要单点直连，所以不引网关，直接按同一协议发。
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from . import config


def _ssl_ctx(cfg: dict | None = None) -> ssl.SSLContext:
    """macOS 上的 Python 默认没装系统根证书，urllib 会报
    `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`
    （curl 能通、Python 不通就是这个原因）。有 certifi 就用它的根证书包。

    内网网关常用自签或企业 CA，certifi 里也没有；那种情况可以在模型配置里勾
    `insecure` 跳过校验——只对本机直连的内网地址这么做，公网服务别关。
    """
    if cfg and cfg.get("insecure"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

# 常见的 OpenAI 兼容服务预设，填 key 即可用；免费额度以各家官网为准，会变
PRESETS = [
    {"name": "Agnes AI", "base_url": "https://apihub.agnes-ai.com/v1",
     "model": "agnes-2.5-pro",
     "note": "OpenAI 兼容；agnes-2.5-flash / 2.5-pro 都是推理模型，max_tokens 别给太小"},
    {"name": "硅基流动 SiliconFlow", "base_url": "https://api.siliconflow.cn/v1",
     "model": "deepseek-ai/DeepSeek-V3", "note": "注册送额度，开源模型齐全"},
    {"name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model": "glm-4-flash", "note": "glm-4-flash 长期免费，中文舆情够用"},
    {"name": "DeepSeek 官方", "base_url": "https://api.deepseek.com/v1",
     "model": "deepseek-chat", "note": "新用户有免费额度"},
    {"name": "阿里百炼 Qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "model": "qwen-plus", "note": "新用户免费额度较大"},
    {"name": "Moonshot Kimi", "base_url": "https://api.moonshot.cn/v1",
     "model": "moonshot-v1-8k", "note": "限速 3 次/分钟，不限总 token"},
    {"name": "ModelScope 魔搭", "base_url": "https://api-inference.modelscope.cn/v1",
     "model": "Qwen/Qwen2.5-72B-Instruct", "note": "每日 2000 次免费调用"},
    {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
     "model": "deepseek/deepseek-chat-v3:free", "note": "有 :free 后缀的免费路由，需外网"},
    {"name": "本地 Ollama", "base_url": "http://localhost:11434/v1",
     "model": "qwen2.5:7b", "note": "本机跑，api_key 随便填，数据不出网"},
]


# ------------------------------------------------------------------ 配置存取

def _load() -> dict:
    if not config.LLM_CONFIG.exists():
        return {"models": [], "default": ""}
    try:
        return json.loads(config.LLM_CONFIG.read_text("utf-8"))
    except Exception:
        return {"models": [], "default": ""}


def _dump(data: dict):
    config.LLM_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def mask(key: str) -> str:
    """只露头尾，够用户认出是哪把 key 就行。"""
    key = key or ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def list_models(safe: bool = True) -> dict:
    data = _load()
    out = []
    for m in data.get("models", []):
        item = dict(m)
        if safe:
            item["api_key"] = mask(m.get("api_key", ""))
        out.append(item)
    return {"models": out, "default": data.get("default", "")}


def get_model(mid: str) -> dict | None:
    data = _load()
    mid = mid or data.get("default", "")
    for m in data.get("models", []):
        if m.get("id") == mid:
            return m
    return (data.get("models") or [None])[0]


def save_model(form: dict) -> dict:
    """新增或更新一个模型配置。api_key 传空表示"沿用原来的"，避免前端拿到打码值后回写把 key 覆盖没了。"""
    data = _load()
    mid = (form.get("id") or "").strip() or uuid.uuid4().hex[:8]
    models = data.setdefault("models", [])
    old = next((m for m in models if m.get("id") == mid), None)
    item = {
        "id": mid,
        "name": (form.get("name") or "未命名模型").strip(),
        "base_url": (form.get("base_url") or "").strip().rstrip("/"),
        "model": (form.get("model") or "").strip(),
        "api_key": (form.get("api_key") or "").strip() or (old or {}).get("api_key", ""),
        "note": (form.get("note") or "").strip(),
        "insecure": bool(form.get("insecure")),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if old:
        models[models.index(old)] = item
    else:
        models.append(item)
    if not data.get("default"):
        data["default"] = mid
    _dump(data)
    return {"ok": True, "id": mid}


def delete_model(mid: str) -> dict:
    data = _load()
    data["models"] = [m for m in data.get("models", []) if m.get("id") != mid]
    if data.get("default") == mid:
        data["default"] = (data["models"][0]["id"] if data["models"] else "")
    _dump(data)
    return {"ok": True}


def set_default(mid: str) -> dict:
    data = _load()
    if any(m.get("id") == mid for m in data.get("models", [])):
        data["default"] = mid
        _dump(data)
        return {"ok": True}
    return {"ok": False, "msg": "没有这个模型"}


# ------------------------------------------------------------------ 调用

def chat(cfg: dict, messages: list[dict], temperature: float | None = None,
         max_tokens: int | None = None, timeout: int = 240) -> str:
    """发一次 /chat/completions，返回文本。异常里带上游原文，便于排查 key/额度问题。

    温度与 max_tokens 默认取「参数配置」里的值：推理型模型（Agnes 2.5、DeepSeek-R1 之类）
    会先花一批 token 做 reasoning，给小了就只剩思考、正文为空，所以默认放宽。
    """
    from . import settings

    rc = settings.get("report")
    temperature = rc["temperature"] if temperature is None else temperature
    max_tokens = rc["max_tokens"] if max_tokens is None else max_tokens
    if not cfg or not cfg.get("base_url") or not cfg.get("model"):
        raise RuntimeError("模型未配置：base_url 或 model 为空")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps({"model": cfg["model"], "messages": messages,
                       "temperature": temperature, "max_tokens": max_tokens,
                       "stream": False}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.get('api_key', '')}",
        # OpenRouter 要求带来源标识，其他家忽略这两个头
        "HTTP-Referer": "http://127.0.0.1:8765",
        "X-Title": "public-opinion-monitor",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx(cfg)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400] if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}：{detail or exc.reason}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        # 首连超时/抖动重试一次：部分网关（如 Agnes）冷启动第一发就要几十秒
        try:
            time.sleep(2)
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx(cfg)) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc2:
            reason = getattr(exc, "reason", exc)
            raise RuntimeError(f"网络不可达（已重试）：{reason}；第二次：{exc2}"[:260]) from None
    except Exception as exc:
        raise RuntimeError(f"调用异常：{exc}"[:200]) from None
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"返回里没有 choices：{json.dumps(data, ensure_ascii=False)[:300]}")
    msg = choices[0].get("message") or {}
    # 有的推理模型把正文放在 reasoning_content 里，content 为空
    text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    if not text:
        usage = data.get("usage") or {}
        det = (usage.get("completion_tokens_details") or {})
        raise RuntimeError(
            "模型只返回了思考、正文为空。"
            f"本次 completion_tokens={usage.get('completion_tokens')}，其中 reasoning="
            f"{det.get('reasoning_tokens')}、text={det.get('text_tokens')}；"
            f"到「参数配置」把 report.max_tokens 调大（当前 {max_tokens}）或换非推理模型")
    return text


def fetch_model_list(mid: str) -> dict:
    """拉 `GET {base_url}/models`，把网关真实支持的模型名列出来。

    加这个是因为模型名写错的报错很不直观：内网 one-api 把 `gpt5.5`（漏了连字符）报成
    `503 当前分组 default 下对于模型 gpt5.5 无可用渠道`，看上去像"没配额"，其实是名字错了。
    """
    cfg = get_model(mid)
    if not cfg:
        return {"ok": False, "msg": "还没有配置任何模型"}
    url = cfg["base_url"].rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {cfg.get('api_key', '')}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=45, context=_ssl_ctx(cfg)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200] if exc.fp else ""
        return {"ok": False, "msg": f"HTTP {exc.code}：{detail or exc.reason}"}
    except Exception as exc:
        return {"ok": False, "msg": f"拉取失败：{exc}"[:200]}
    items = data.get("data") if isinstance(data, dict) else data
    ids = [str(m.get("id") or m.get("name") or "") for m in (items or []) if isinstance(m, dict)]
    ids = [i for i in ids if i]
    if not ids:
        return {"ok": False, "msg": f"接口没返回模型列表：{json.dumps(data, ensure_ascii=False)[:200]}"}
    return {"ok": True, "models": sorted(ids), "count": len(ids)}


def test_model(mid: str) -> dict:
    """连通测试。判定标准是"接口通了"而不是"内容非空"：
    实测 glm-4-flash 对「只回复两个字：连通」这种极短指令会返回空正文（completion_tokens=2），
    但换成正常问题就答得很好——把空正文当失败会误判可用的模型不可用。
    """
    cfg = get_model(mid)
    if not cfg:
        return {"ok": False, "msg": "还没有配置任何模型"}
    t0 = time.time()
    try:
        # 推理模型（Agnes 2.5、DeepSeek-R1 等）32 个 token 全烧在思考上，所以给宽一点
        text = chat(cfg, [{"role": "user", "content": "请用一句话说明开屏广告为什么招人烦"}],
                    max_tokens=512, timeout=180)
    except RuntimeError as exc:
        msg = str(exc)
        if "正文为空" in msg:
            return {"ok": True, "msg": f"{cfg['name']} 接口连通，但这次没返回正文（{msg[:80]}）",
                    "latency": round(time.time() - t0, 2)}
        return {"ok": False, "msg": msg}
    return {"ok": True, "msg": f"{cfg['name']} 连通（{time.time() - t0:.1f}s）：{text[:40]}",
            "latency": round(time.time() - t0, 2)}


# ------------------------------------------------------------------ 报告

SYS_PROMPT = (
    "你是舆情分析师，只依据给定数据写结论。"
    "严禁编造数据里没有的数字、平台、账号或事件；数据不足就直接写「数据不足，无法判断」。"
    "输出中文 Markdown，不要写客套话和自我介绍。"
)

REPORT_ASK = """请基于下面的开屏/摇一摇广告负反馈监控数据写一份分析报告。

**写法要求（很重要）**：报告会被渲染成带图表的 HTML，KPI、趋势图、平台分布、事件卡、
重点负面帖、重点预警色块都由平台侧用真实数据现算好了，**你不要复述这些数字表格**。
你的文字只补图表看不出来的判断。因此：
- 每节控制在 3~6 个要点，用 `- ` 列表写，单条不超过两行；不要写大段叙述性长文；
- 引用数字时只挑支撑判断的那一两个关键数，不要罗列；
- 不要编造数据里没有的事件、帖子、平台或数字。

## 一、总体结论
3~5 条要点：声量规模与环比方向、负面占比、主要来源、是否已进入监管/官媒视野。

## 二、事件归因
**直接使用数据里「事件归因」小节给出的事件**，不要另编事件名。每个事件一条要点：
为什么会引爆（结合证据词与引爆帖）＋处在扩散哪个阶段＋是否与其他事件同源。
「自动聚类」类型要注明是按共现短语聚的、需人工确认。

## 三、传播结构
3~5 条要点：哪类主体在放大（用户自发 vs 媒体放大）、是否有监管介入信号。

## 四、风险评级
给出 低 / 中 / 中高 / 高 之一，下面列 2~4 条判断依据，每条引用一个具体数字。

## 五、重点预警复核
只从数据里「重点预警」小节挑，逐类一条：这类触线是真实广告内容问题还是词表误伤
（结合命中词判断），以及是否需要人工立即复核。涉政类必须写明只是疑似。

## 六、建议动作
3~5 条，按优先级排列，每条指明对应哪个事件 / 哪个平台 / 哪个预警类别。
"""


def build_prompt(ov: dict, rows: list[dict], limit_samples: int | None = None) -> str:
    """把统计口径 + 样本帖子拼成提示词。样本条数走「参数配置」report.samples，控制 token。"""
    from . import settings

    limit_samples = limit_samples or settings.get("report", "samples")
    lines = ["# 监控数据", ""]
    lines.append(f"- 时间范围：{ov.get('since') or '不限'} ~ {ov.get('until') or '不限'}")
    lines.append(f"- 关键词：{'、'.join(ov.get('keywords') or []) or '未知'}")
    lines.append(f"- 当前口径：{ov.get('scope')}，共 {ov.get('total', 0)} 条")
    lines.append("- **口径说明（别弄混）**：当前口径 main 只含负面内容（UGC 负反馈 + 媒体负面报道），"
                 "所以在它里面算「负面占比」必然接近 100%，没有分析意义；"
                 f"下面的主体分布与情感分布统计的是**剔噪后全量** {ov.get('valid_total', 0)} 条"
                 "（含中性与正面），要判断负面占比请用这一组数。")
    lines.append(f"- 原始入库（含噪音）：{ov.get('raw_total', 0)} 条")
    lines.append(f"- 当前口径互动量合计：{ov.get('engagement', 0)}")
    lines.append("")
    lines.append("## 平台分布")
    for p in ov.get("by_platform", []):
        lines.append(f"- {p['平台']}：{p['帖子数']} 条，互动量 {p['互动量']}")
    lines.append("")
    lines.append("## 声量换算（已按平台归一 + 缺失插补，可跨平台相加）")
    sv = ov.get("sov") or {}
    if sv:
        lines.append(f"- 总声量 {sv.get('total_sov')} 分，负面声量 {sv.get('neg_sov')} 分"
                     f"（占 {sv.get('neg_share')}%），日均 {sv.get('daily_avg')} 分")
        lines.append(f"- 实测占比 {sv.get('measured_ratio')}%（实测 {sv.get('measured')} 条 / "
                     f"估算 {sv.get('imputed')} 条，估算来自微信公众号与百家号——网页侧无互动数）")
        for p in sv.get("by_platform", []):
            lines.append(f"  - {p['name']}：声量 {p['sov']}（负面 {p['neg_sov']}），{p['posts']} 条")
    lines.append("")
    # 事件归因与重点负面帖都由平台侧用真实数据算好再喂进去：
    # 模型只负责解释"为什么会这样"，不负责发明事件名和数字，否则报告里必然出现编造内容
    evs = ov.get("events") or []
    if evs:
        im = ov.get("impact") or {}
        lines.append("## 事件归因（平台侧已按证据词/共现短语聚好，直接用，别另起事件名）")
        lines.append(f"- 影响力口径：{im.get('note') or '互动量 × 主体权重'}")
        for i, e in enumerate(evs, 1):
            lead = e.get("lead") or {}
            plats = "、".join(f"{p['name']}{p['posts']}条" for p in (e.get("platforms") or [])[:5])
            lines.append(
                f"{i}. 【{e['name']}】类型={e['kind']}｜{e['posts']} 条｜负面影响力 {e['impact']}"
                f"｜含重点负面 {e['high_posts']} 条｜{e['first_day']} ~ {e['last_day']}｜平台：{plats}")
            lines.append(f"   - 归因说明：{e['why']}")
            lines.append(f"   - 命中证据词：{'、'.join(e.get('evidence') or []) or '无'}")
            lines.append(f"   - 引爆帖：{lead.get('platform')}／{lead.get('author') or '匿名'}"
                         f"／{lead.get('day')}／互动量 {lead.get('engagement')}：{lead.get('title')}")
        lines.append("")
    hot = ov.get("hot_negative") or []
    if hot:
        lines.append("## 重点负面帖（负面影响力降序，「重点」＝超过本批高影响门槛）")
        for h in hot:
            lines.append(
                f"- [{'重点' if h.get('high') else '一般'}] {h['platform_label']}／"
                f"{h.get('author') or '匿名'}／{(h.get('publish_time') or '')[:10]}"
                f"｜互动量 {h['engagement']}｜负面影响力 {h['impact']}"
                f"｜归因场景 {h.get('scene')}｜{h['title']}")
        lines.append("")
    al = ov.get("alerts") or {}
    if al.get("total"):
        lines.append("## 重点预警（内容触线按显式词表判定，命中词已列出；涉政类只标疑似）")
        lines.append(f"- 进池 {al['total']} 条：内容触线 {al.get('risky', 0)} 条，"
                     f"仅声量大 {al.get('big_only', 0)} 条")
        for c in al.get("cats") or []:
            words = "、".join(f"{w[0]}×{w[1]}" for w in (c.get("words") or [])[:6])
            plats = "、".join(f"{p[0]}{p[1]}条" for p in (c.get("plats") or [])[:4])
            lines.append(f"  - {c['name']}（严重度{c.get('sev_text')}）：{c['posts']} 条，"
                         f"影响力 {c['impact']}｜命中词 {words}｜平台 {plats}")
        lines.append("")
    lines.append("## 发声主体分布（剔噪后全量）")
    for a in ov.get("by_actor", []):
        lines.append(f"- {a['name']}：{a['posts']} 条，互动量 {a['engagement']}")
    lines.append("")
    lines.append("## 情感分布（剔噪后全量）")
    for s in ov.get("by_sentiment", []):
        lines.append(f"- {s['name']}：{s['posts']} 条")
    lines.append("")
    trend = ov.get("trend") or []
    if trend:
        tops = sorted(trend, key=lambda r: -sum(v for k, v in r.items() if k != "date"))[:8]
        lines.append("## 声量最高的日期")
        for r in tops:
            n = sum(v for k, v in r.items() if k != "date")
            lines.append(f"- {r['date']}：{n} 条")
        lines.append("")
    lines.append(f"## 样本帖子（按互动量降序，共 {min(len(rows), limit_samples)} 条）")
    for r in rows[:limit_samples]:
        lines.append(
            f"- [{r.get('platform_label')}｜{r.get('actor') or '-'}｜{r.get('sentiment') or '-'}"
            f"｜互动 {r.get('engagement') or 0}] {(r.get('author') or '-')}："
            f"{(r.get('title') or '')[:80]}")
    lines.append("")
    lines.append(REPORT_ASK)
    return "\n".join(lines)


def generate_report(mid: str, ov: dict, rows: list[dict], note: str = "",
                    out_fmt: str = "md") -> dict:
    from . import settings

    cfg = get_model(mid)
    if not cfg:
        return {"ok": False, "msg": "还没有接入任何模型，先在「分析模型」里填 base_url + key"}
    samples = settings.get("report", "samples")
    prompt = build_prompt(ov, rows, samples)
    if out_fmt == "html":
        prompt += "\n" + HTML_ASK
    if note:
        prompt += f"\n补充要求：{note}\n"
    text = chat(cfg, [{"role": "system", "content": SYS_PROMPT},
                      {"role": "user", "content": prompt}])
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = config.REPORT_DIR / f"舆情分析报告_{stamp}.md"
    head = (f"# 舆情分析报告（{ov.get('since')} ~ {ov.get('until')}）\n\n"
            f"> 模型：{cfg['name']}（{cfg['model']}）｜生成时间："
            f"{datetime.now():%Y-%m-%d %H:%M}｜口径：{ov.get('scope')}｜"
            f"样本 {min(len(rows), samples)} / 共 {ov.get('total', 0)} 条\n\n")
    path.write_text(head + text, "utf-8")
    res = {"ok": True, "file": path.name, "text": head + text}
    # 模型返回的是 HTML（或被要求输出 HTML）时，立刻产出可直接打开的页面
    if out_fmt == "html" or extract_html(text):
        html_res = export_report(path.name, "html")
        if html_res.get("ok"):
            res["html_file"] = html_res["file"]
    return res


def list_reports() -> list[dict]:
    out = []
    for f in sorted(config.REPORT_DIR.glob("*.md"), key=lambda p: -p.stat().st_mtime):
        out.append({"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1),
                    "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out[:40]


def read_report(name: str) -> str:
    path = (config.REPORT_DIR / name).resolve()
    if not str(path).startswith(str(config.REPORT_DIR.resolve())) or not path.is_file():
        return ""
    return path.read_text("utf-8")


# ------------------------------------------------------------------ 多格式导出

HTML_TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --brand:#3d5fd9; --brand2:#6b86e8; --brand-weak:#eff2fd;
    --txt:#1c212b; --txt2:#566072; --txt3:#8b93a3; --line:#e8eaef;
    --danger:#cf4a44; --danger-weak:#fdeeed; --warn:#b07d1a; --warn-weak:#fdf5e6;
    --ok:#178a5f; --ok-weak:#e8f6f0; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; color:var(--txt);
    background:radial-gradient(1100px 380px at 80% -14%, #e7eeff 0%, rgba(231,238,255,0) 70%), #f4f5f7;
    font:15px/1.85 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:1060px; margin:34px auto; background:#fff; border:1px solid var(--line);
    border-radius:20px; box-shadow:0 2px 6px rgba(28,33,43,.04),0 18px 44px rgba(28,33,43,.07);
    overflow:hidden; }}
  /* 封面头：渐变底 + 斜向光斑，避免大色块显得死板 */
  .hd {{ padding:34px 38px 30px; color:#fff; position:relative; overflow:hidden;
    background:linear-gradient(128deg,#2f4bc4 0%,#3d5fd9 45%,#6b86e8 100%); }}
  .hd::after {{ content:""; position:absolute; right:-90px; top:-120px; width:340px; height:340px;
    border-radius:50%; background:rgba(255,255,255,.09); }}
  .hd .tagline {{ font-size:12px; letter-spacing:2.6px; opacity:.82; text-transform:uppercase; }}
  .hd h1 {{ margin:8px 0 10px; font-size:26px; line-height:1.35; letter-spacing:.3px; font-weight:650; }}
  .hd .meta {{ font-size:12.5px; opacity:.9; position:relative; }}
  .hd .meta span {{ display:inline-block; background:rgba(255,255,255,.16); border-radius:20px;
    padding:2px 11px; margin:4px 6px 0 0; }}
  .bd {{ padding:26px 38px 36px; }}
  .lead {{ font-size:12.5px; color:var(--txt2); background:#fafbfd; border:1px solid var(--line);
    border-radius:12px; padding:11px 14px; margin:0 0 16px; }}
  /* 章节标题：编号胶囊 + 细线，长报告里层级一眼看清 */
  h2 {{ font-size:18px; margin:30px 0 12px; padding:0 0 9px; font-weight:650;
    border-bottom:1px solid var(--line); display:flex; align-items:center; gap:10px; }}
  h2::before {{ content:""; width:4px; height:18px; border-radius:3px; flex:0 0 4px;
    background:linear-gradient(180deg,var(--brand),var(--brand2)); }}
  h3 {{ font-size:15.5px; margin:20px 0 8px; font-weight:650; color:#26314a; }}
  h3::before {{ content:"▍"; color:var(--brand2); margin-right:4px; font-size:12px; }}
  p {{ margin:9px 0; }}
  ul,ol {{ padding-left:20px; margin:9px 0; }}
  li {{ margin:5px 0; }}
  li::marker {{ color:var(--brand2); }}
  b {{ color:#16204a; }}
  blockquote {{ margin:14px 0; padding:12px 16px; background:var(--brand-weak); color:var(--txt2);
    border-left:3px solid var(--brand2); border-radius:0 10px 10px 0; font-size:13.5px; }}
  code {{ background:var(--brand-weak); padding:1px 6px; border-radius:5px; font-size:13.5px; }}
  table {{ border-collapse:separate; border-spacing:0; width:100%; margin:14px 0; font-size:13px;
    border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
  th,td {{ padding:9px 12px; text-align:left; border-bottom:1px solid var(--line); }}
  th {{ background:#f7f8fc; font-size:12.5px; font-weight:650; color:var(--txt2); }}
  tr:last-child td {{ border-bottom:0; }}
  tbody tr:nth-child(even) td {{ background:#fcfdff; }}
  /* 关键词自动上色：模型写「高风险 / 建议 / 已整治」时正文里能看出轻重 */
  .lv-high {{ background:var(--danger-weak); color:var(--danger); border-radius:20px; padding:1px 9px;
    font-size:12px; font-weight:650; }}
  .lv-mid {{ background:var(--warn-weak); color:var(--warn); border-radius:20px; padding:1px 9px;
    font-size:12px; font-weight:650; }}
  .lv-low {{ background:var(--ok-weak); color:var(--ok); border-radius:20px; padding:1px 9px;
    font-size:12px; font-weight:650; }}
  .ft {{ padding:18px 38px; border-top:1px solid var(--line); color:var(--txt3); font-size:12px;
    background:#fafbfd; }}
  /* 后半段的模块色块：每个 ## 小节一张卡，左侧强调色 + 编号徽章，
     避免"报告后半部分是纯文本"的观感 */
  .mod {{ border:1px solid var(--line); border-left:3px solid var(--acc,#3d5fd9);
    border-radius:12px; margin:14px 0; overflow:hidden; background:#fff; }}
  .mod-hd {{ display:flex; align-items:center; gap:9px; padding:11px 16px; font-size:15.5px;
    font-weight:650; color:#16204a;
    background:linear-gradient(90deg, color-mix(in srgb, var(--acc,#3d5fd9) 9%, #fff), #fff); }}
  .mod-no {{ width:20px; height:20px; flex:0 0 20px; border-radius:6px; display:grid;
    place-items:center; background:var(--acc,#3d5fd9); color:#fff; font-size:11.5px; }}
  .mod-bd {{ padding:6px 18px 14px; }}
  .mod-bd > *:first-child {{ margin-top:6px; }}
  .mod-lead {{ font-size:13px; color:var(--txt2); margin:8px 0; }}
  /* 事件归因 / 重点负面：平台侧用真实数据渲染的色块，跟模型文字分开摆 */
  .ev {{ border:1px solid var(--line); border-left:3px solid var(--danger); border-radius:12px;
    padding:12px 15px; margin:10px 0; background:#fff; }}
  .ev.auto {{ border-left-color:var(--txt3); background:#fcfcfd; }}
  .ev-hd {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:14.5px;
    font-weight:650; }}
  .ev-hd .no {{ width:19px; height:19px; flex:0 0 19px; border-radius:6px; display:grid;
    place-items:center; background:var(--danger); color:#fff; font-size:11px; }}
  .ev.auto .ev-hd .no {{ background:var(--txt3); }}
  .ev-hd .k {{ font-size:11.5px; font-weight:600; border-radius:20px; padding:1px 9px;
    background:var(--brand-weak); color:var(--brand); }}
  .ev.auto .ev-hd .k {{ background:#f0f2f6; color:var(--txt2); }}
  .ev-hd .when {{ margin-left:auto; font-size:11.5px; font-weight:400; color:var(--txt3); }}
  .ev-bar {{ height:5px; border-radius:3px; background:var(--line); margin:9px 0 8px; overflow:hidden; }}
  .ev-bar > i {{ display:block; height:100%; border-radius:3px;
    background:linear-gradient(90deg,var(--danger),#e98a85); }}
  .ev.auto .ev-bar > i {{ background:linear-gradient(90deg,#8b93a3,#c7d0e4); }}
  .ev-nums {{ display:flex; gap:16px; flex-wrap:wrap; font-size:12px; color:var(--txt2); }}
  .ev-nums b {{ color:#16204a; }}
  .ev-why {{ font-size:12.5px; color:var(--txt2); line-height:1.7; margin-top:7px; }}
  .ev-tags {{ font-size:11.5px; color:var(--txt3); margin-top:5px; }}
  .ev-tags code {{ background:#f6f7fb; border:1px solid var(--line); padding:0 5px; }}
  .ev-lead {{ display:flex; align-items:center; gap:8px; margin-top:9px; padding:8px 11px;
    background:var(--warn-weak); border:1px solid #f3e3c4; border-radius:8px; font-size:12.5px; }}
  .ev-lead .b {{ font-size:11px; font-weight:650; color:var(--warn); white-space:nowrap; }}
  tr.hi td {{ background:#fdf4f3; }}
  .dot-hi {{ display:inline-block; font-size:11px; font-weight:650; color:#fff;
    background:var(--danger); border-radius:20px; padding:1px 8px; }}
  @media print {{
    body {{ background:#fff; }}
    .wrap {{ border:0; box-shadow:none; margin:0; border-radius:0; max-width:none; }}
    h2 {{ page-break-after:avoid; }}
    table, .kpi-row {{ page-break-inside:avoid; }}
  }}
  @media (max-width:720px) {{ .hd,.bd,.ft {{ padding-left:20px; padding-right:20px; }} }}
</style></head>
<body><div class="wrap">
  <div class="hd">
    <div class="tagline">Sentiment Volume Report</div>
    <h1>{title}</h1><div class="meta">{meta}</div>
  </div>
  <div class="bd">{body}</div>
  <div class="ft">由「舆论声量监控平台」生成 · 图表与数字取自本机库内真实采集数据 ·
    文字结论由所选大模型输出，请以采集口径为准 · {stamp}</div>
</div></body></html>
"""


# 正文里的风险/程度词自动包上色标，报告不再是"一大片黑字"
_LEVEL_WORDS = (
    (r"高风险|重大风险|紧急", "lv-high"),
    (r"中风险|需关注|待观察", "lv-mid"),
    (r"低风险|已缓解|已整治", "lv-low"),
)


def _mark_levels(html: str) -> str:
    # 只在标签之外的正文里替换：把 HTML 按 <...> 切开，奇数段是标签，原样放回
    parts = re.split(r"(<[^>]*>)", html)
    for i, seg in enumerate(parts):
        if seg.startswith("<"):
            continue
        for pat, cls in _LEVEL_WORDS:
            seg = re.sub(rf"({pat})", rf'<span class="{cls}">\1</span>', seg)
        parts[i] = seg
    return "".join(parts)


# 报告后半段（模型分析）以前是一长串 h2 + p，读起来就是一片黑字。
# 这里把每个 ## 小节切成独立的色块模块，六个小节轮换配色，标题带编号徽章。
_SEC_ACCENT = ["#3d5fd9", "#cf4a44", "#b07d1a", "#178a5f", "#7c5cff", "#0aa2c0"]


def _sectionize(html: str) -> str:
    """把 md_to_html_fragment 的输出按 <h2> 切成一组 .mod 色块。"""
    chunks = re.split(r"(<h2>.*?</h2>)", html, flags=re.DOTALL)
    lead = chunks[0].strip()
    out = [f'<div class="mod-lead">{lead}</div>'] if lead else []
    for i in range(1, len(chunks), 2):
        title = re.sub(r"</?h2>", "", chunks[i]).strip()
        body = (chunks[i + 1] if i + 1 < len(chunks) else "").strip()
        c = _SEC_ACCENT[(i // 2) % len(_SEC_ACCENT)]
        out.append(
            f'<section class="mod" style="--acc:{c}">'
            f'<div class="mod-hd"><span class="mod-no">{i // 2 + 1}</span>{title}</div>'
            f'<div class="mod-bd">{body or "<p>（本节模型未输出内容）</p>"}</div>'
            f'</section>')
    return "".join(out) if out else html

def md_to_html_fragment(md: str) -> str:
    """极简 markdown → HTML（标题/列表/引用/加粗/行内代码/表格）。

    不引第三方 markdown 库：报告结构是我们自己在 prompt 里定死的，只有这几种元素，
    引库反而多一层依赖和样式适配。
    """
    import html as _h

    out, in_list, in_table = [], False, False
    seen_heading = False
    for raw in (md or "").split("\n"):
        line = _h.escape(raw.rstrip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        if re.match(r"^\s*\|.+\|\s*$", raw):
            cells = [c.strip() for c in raw.strip().strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{_h.escape(c)}</th>" for c in cells) + "</tr>")
                continue
            out.append("<tr>" + "".join(f"<td>{_h.escape(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)$", raw)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = _h.escape(m.group(1))
            item = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", item)
            out.append(f"<li>{item}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if raw.startswith("### "):
            out.append(f"<h3>{line[4:]}</h3>")
            seen_heading = True
        elif raw.startswith("## "):
            out.append(f"<h2>{line[3:]}</h2>")
            seen_heading = True
        elif raw.startswith("# "):
            continue                      # 一级标题由模板头部渲染，避免重复
        elif raw.startswith("> "):
            # 正文第一段之前的引用块就是报告头的元信息，头部已经渲染成胶囊了，别再重复一遍
            if not seen_heading:
                continue
            out.append(f"<blockquote>{line[2:]}</blockquote>")
        elif raw.strip():
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


def export_report(name: str, fmt: str = "html", ov: dict | None = None) -> dict:
    """把已生成的报告导出成 html / txt / pdf（md 本身就有）。

    PDF 用 Playwright 打印 HTML —— 项目本来就装了 Playwright，不必再引 wkhtmltopdf。
    """
    md = read_report(name)
    if not md:
        return {"ok": False, "msg": "报告不存在"}
    stem = Path(name).stem
    lines = md.split("\n")
    title = lines[0].lstrip("# ").strip() if lines else stem
    meta = next((l.lstrip("> ").strip() for l in lines if l.startswith("> ")), "")
    if fmt == "txt":
        path = config.REPORT_DIR / f"{stem}.txt"
        path.write_text(re.sub(r"[*`>#]", "", md), "utf-8")
        return {"ok": True, "file": path.name}
    inner = extract_html(md)
    if ov and not inner:
        # 有统计数据就走"图表 + 文字"的富版本；模型自己输出 HTML 的报告保持原样
        html = build_rich_html(name, md, ov)
    else:
        body = inner or _sectionize(_mark_levels(md_to_html_fragment(md)))
        html = HTML_TPL.format(title=title, meta=_meta_pills(meta), body=body,
                               stamp=datetime.now().strftime("%Y-%m-%d %H:%M"))
    hpath = config.REPORT_DIR / f"{stem}.html"
    hpath.write_text(html, "utf-8")
    if fmt == "html":
        return {"ok": True, "file": hpath.name}
    if fmt == "pdf":
        try:
            from playwright.sync_api import sync_playwright
            ppath = config.REPORT_DIR / f"{stem}.pdf"
            with sync_playwright() as p:
                b = p.chromium.launch()
                pg = b.new_page()
                pg.goto(hpath.as_uri(), wait_until="load")
                pg.pdf(path=str(ppath), format="A4", print_background=True,
                       margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"})
                b.close()
            return {"ok": True, "file": ppath.name}
        except Exception as exc:
            return {"ok": False, "msg": f"PDF 生成失败：{exc}"[:200]}
    return {"ok": False, "msg": f"不支持的格式：{fmt}"}


def any_file(name: str) -> Path | None:
    """按文件名取报告目录下的任意产物（md/html/pdf/txt），带目录穿越校验。"""
    path = (config.REPORT_DIR / name).resolve()
    if not str(path).startswith(str(config.REPORT_DIR.resolve())) or not path.is_file():
        return None
    return path


# 模型经常把 HTML 包在 ```html 代码块里返回（用户看到的就是"一段代码"而不是页面），
# 这里统一识别并抽出来，导出时直接当页面用。
_FENCE_HTML = re.compile(r"```(?:html)?\s*(<(?:!DOCTYPE|html|div|section|body)[\s\S]+?)```",
                         re.IGNORECASE)


def extract_html(md: str) -> str:
    """如果报告正文其实是 HTML（带 ``` 围栏或裸 HTML），返回该 HTML，否则返回空串。"""
    text = (md or "").strip()
    m = _FENCE_HTML.search(text)
    if m:
        return m.group(1).strip()
    body = re.sub(r"^#.*$|^>.*$", "", text, flags=re.MULTILINE).strip()
    if re.match(r"^<(?:!DOCTYPE|html|div|section)\b", body, re.IGNORECASE):
        return body
    return ""


HTML_ASK = """输出要求（重要）：直接输出一段**完整可渲染的 HTML 片段**，不要用 ``` 代码块包裹，
不要输出任何解释文字。片段要求：
- 用 <section>/<h2>/<p>/<ul>/<table> 组织，结构同上面六段；
- 关键数字用 <b> 强调，风险等级用带背景色的 <span style="..."> 标出；
- 允许写内联 style，但不要引用外部 CSS/JS、不要 <script>；
- 表格用于「平台 × 声量」和「主体 × 情感」两组数据。
"""


def _event_blocks(ov: dict) -> str:
    """事件归因色块：全部字段来自库内真实数据，模型碰不到这里的任何数字。"""
    import html as _h

    evs = ov.get("events") or []
    if not evs:
        return ""
    top = max((e["impact"] for e in evs), default=1) or 1
    out = ['<h2>事件归因（平台侧按证据词聚类，非模型生成）</h2>']
    im = ov.get("impact") or {}
    if im.get("note"):
        out.append(f'<div class="mod-lead">{_h.escape(im["note"])}</div>')
    for i, e in enumerate(evs, 1):
        lead = e.get("lead") or {}
        plats = "、".join(f'{p["name"]} {p["posts"]}' for p in (e.get("platforms") or [])[:6])
        auto = e.get("kind") != "已知场景"
        out.append(
            f'<div class="ev{" auto" if auto else ""}">'
            f'<div class="ev-hd"><span class="no">{i}</span>{_h.escape(e["name"])}'
            f'<span class="k">{_h.escape(e["kind"])}</span>'
            + (f'<span class="lv-high">含 {e["high_posts"]} 条重点负面</span>'
               if e.get("high_posts") else "")
            + f'<span class="when">{_h.escape(e["first_day"])} ~ {_h.escape(e["last_day"])}</span></div>'
            f'<div class="ev-bar"><i style="width:{max(3, e["impact"] / top * 100):.1f}%"></i></div>'
            f'<div class="ev-nums"><span>条数 <b>{e["posts"]:,}</b></span>'
            f'<span>负面影响力 <b>{e["impact"]:,}</b></span>'
            f'<span>互动量 <b>{e["engagement"]:,}</b></span>'
            f'<span>平台：{_h.escape(plats)}</span></div>'
            f'<div class="ev-why">{_h.escape(e["why"])}</div>'
            f'<div class="ev-tags">证据词：'
            + (" ".join(f"<code>{_h.escape(w)}</code>" for w in e.get("evidence") or []) or "—")
            + '</div>'
            f'<div class="ev-lead"><span class="b">引爆帖</span>'
            f'{_h.escape(lead.get("platform") or "")}／{_h.escape(lead.get("author") or "匿名")}'
            f'／{_h.escape(lead.get("day") or "")}／互动量 {lead.get("engagement") or 0:,}'
            f'：{_h.escape((lead.get("title") or "")[:60])}</div>'
            '</div>')
    return "".join(out)


def _hot_table(ov: dict) -> str:
    from . import charts

    hot = ov.get("hot_negative") or []
    if not hot:
        return ""
    rows = [[('<span class="dot-hi">重点</span>' if h.get("high") else str(i)),
             h["platform_label"], (h.get("publish_time") or "")[:10],
             (h.get("title") or "")[:38], h.get("author") or "匿名",
             h.get("scene") or "未归类", f'{h["engagement"]:,}', f'{h["impact"]:,}']
            for i, h in enumerate(hot, 1)]
    return ('<h2>重点负面帖（负面影响力降序）</h2>'
            + charts.table(["", "平台", "发布", "标题", "作者", "归因场景", "互动量", "负面影响力"],
                           rows, {6, 7}, raw_cols={0}))


_ALERT_COLOR = {"porn": "#e0457b", "competitor": "#3b82f6", "scare": "#8b5cf6",
                "fraud": "#ef4444", "politics": "#b45309", "minor": "#0d9488"}


def _alert_blocks(ov: dict) -> str:
    """重点预警：一类一个色块 + 命中词，后面跟明细表。

    报告里最容易被写成一段"存在色情、诈骗等风险"的空话，所以这里把类别、条数、
    命中词、平台分布全部做成色块摊开——结论可以由模型写，但证据必须是库里的。
    """
    from . import charts

    al = ov.get("alerts") or {}
    cats = al.get("cats") or []
    if not al.get("total"):
        return ""
    blocks = []
    for c in cats:
        color = _ALERT_COLOR.get(c["key"], "#59637a")
        words = "".join(
            f'<span style="display:inline-block;font-size:11px;padding:1px 6px;margin:0 4px 4px 0;'
            f'border-radius:5px;background:{color}1f;color:#1c212b">{_esc_h(w[0])} {w[1]}</span>'
            for w in (c.get("words") or [])[:6])
        plats = "、".join(f'{_esc_h(p[0])} {p[1]}' for p in (c.get("plats") or [])[:4])
        blocks.append(
            f'<div style="flex:1 1 210px;min-width:200px;background:#fff;border:1px solid #e8eaef;'
            f'border-left:4px solid {color};border-radius:12px;padding:11px 13px">'
            f'<div style="font-size:13px;font-weight:650;margin-bottom:3px">{_esc_h(c["name"])}'
            f'<span style="font-size:11px;font-weight:500;color:{color};margin-left:6px">'
            f'严重度{_esc_h(c.get("sev_text", ""))}</span></div>'
            f'<div style="font-size:12px;color:#59637a;margin-bottom:7px">'
            f'<b style="font-size:18px;color:{color}">{c["posts"]}</b> 条'
            f' · 影响力 {c["impact"]:,}</div>'
            f'<div style="margin-bottom:5px">{words or "—"}</div>'
            f'<div style="font-size:11px;color:#8b95a9">{plats or "—"}</div></div>')
    items = al.get("items") or []
    rows = [[ "、".join(x["name"] for x in (it.get("cats") or [])) or "声量大",
              it.get("platform_label") or "", (it.get("publish_time") or "")[:10],
              (it.get("title") or "")[:34], (it.get("reason") or "")[:46],
              f'{it.get("engagement") or 0:,}', f'{it.get("impact") or 0:,}']
            for it in items[:12]]
    return (f'<h2>重点预警监控</h2>'
            f'<div class="lead">{_esc_h(al.get("note", ""))}</div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin:10px 0 12px">'
            f'{"".join(blocks)}</div>'
            + (charts.table(["命中类别", "平台", "发布", "标题", "入选理由", "互动量", "影响力"],
                            rows, {5, 6}) if rows else ""))


def _esc_h(t) -> str:
    import html as _h
    return _h.escape(str(t if t is not None else ""))


def build_rich_html(name: str, md: str, ov: dict) -> str:
    """把「平台侧生成的图表」+「模型写的文字」拼成一份内容饱满的 HTML 报告。

    分工是刻意的：图表和所有数字来自库里的真实数据（charts.py 现算 SVG），
    模型只负责议题归纳与建议——避免出现"模型编的图表数字"和看板对不上的情况。
    """
    from . import charts

    lines = md.split("\n")
    title = lines[0].lstrip("# ").strip() if lines else name
    meta = next((l.lstrip("> ").strip() for l in lines if l.startswith("> ")), "")
    sv = ov.get("sov") or {}
    sents = {s["name"]: s["posts"] for s in (ov.get("by_sentiment") or [])}

    # KPI 跟看板保持同一套口径：3 个总量 + 「声量最大平台」「舆论最高事件」两个结论指标
    by_sov = sorted(sv.get("by_platform") or [], key=lambda p: -p.get("sov", 0))
    top_plat = by_sov[0] if by_sov else None
    sov_all = sum(p.get("sov", 0) for p in by_sov) or 1
    hot = max((ov.get("_top") or []), key=lambda r: r.get("engagement") or 0, default=None)

    kpis = charts.kpi_row([
        {"label": "当前口径帖子数", "value": f"{ov.get('total', 0):,}",
         "sub": f"入库原始 {ov.get('raw_total', 0):,} 条"},
        {"label": "负面声量分", "value": f"{sv.get('neg_sov', 0):,}",
         "sub": f"占总声量 {sv.get('neg_share', 0)}%"},
        {"label": "互动量合计", "value": f"{ov.get('engagement', 0):,}",
         "sub": "赞+评×2+转×3+藏×2"},
        {"label": "声量最大的平台",
         "value": top_plat["name"] if top_plat else "—",
         "sub": (f"{top_plat['sov']:,} 分 · 占全网 {top_plat['sov'] / sov_all * 100:.1f}%"
                 if top_plat else "无声量数据")},
        {"label": "舆论最高的事件",
         "value": f"{(hot or {}).get('engagement', 0):,}" if hot else "—",
         "sub": ((hot.get("title") or "")[:22] + f"（{hot.get('platform_label')}）") if hot else "无内容"},
    ])

    trend = ov.get("trend") or []
    labels = ov.get("labels") or []
    daily = [(r["date"][5:], sum(v for k, v in r.items() if k != "date")) for r in trend]
    sov_trend = {r["date"]: r for r in (sv.get("trend") or [])}
    neg_line = [sov_trend.get(r["date"], {}).get("neg_sov", 0) for r in trend]

    plat_rows = [(p["name"], p["sov"], p["neg_sov"]) for p in (sv.get("by_platform") or [])]
    actor_parts = [(a["name"], a["posts"]) for a in (ov.get("by_actor") or [])]
    sent_parts = [(k, sents.get(k, 0)) for k in ("负面", "中性", "正面") if sents.get(k)]
    neg_share = sv.get("neg_share", 0)

    cov_rows = [[c["平台"], f'{c["入库"]:,}', f'{c["当前口径"]:,}', f'{c["有时间占比"]}%',
                 f'{c["覆盖天数"]}', c["最新发布"] or "-"] for c in (ov.get("coverage") or [])]
    top_rows = [[r.get("platform_label"), (r.get("title") or "")[:34], r.get("actor") or "-",
                 r.get("sentiment") or "-", f'{r.get("engagement") or 0:,}']
                for r in (ov.get("_top") or [])[:10]]

    def card(title_, body, flex="1 1 340px"):
        return (f'<div style="flex:{flex};min-width:320px;background:#fff;border:1px solid #e8eaef;'
                f'border-radius:14px;padding:14px 16px;margin:0">'
                f'<div style="font-size:13.5px;font-weight:650;padding-left:9px;'
                f'border-left:3px solid {charts.BRAND};margin-bottom:10px">{title_}</div>'
                f'{body}</div>')

    charts_html = (
        '<div style="display:flex;flex-wrap:wrap;gap:12px;margin:12px 0">'
        + card("按日声量趋势（真实数据）",
               charts.line(daily, neg_line if any(neg_line) else None), "1 1 100%")
        + card("各平台声量（含负面占比）", charts.bar(plat_rows))
        + card("情感结构", charts.donut(sent_parts, f"{neg_share}%", "负面声量占比"))
        + card("发声主体分布", charts.donut(actor_parts, f'{len(actor_parts)}', "类主体"))
        + card("平台覆盖与新鲜度",
               charts.table(["平台", "入库", "当前口径", "有时间占比", "覆盖天数", "最新发布"],
                            cov_rows, {1, 2, 3, 4}))
        + (card("互动量 TOP10",
                charts.table(["平台", "标题", "主体", "情感", "互动量"], top_rows, {4}))
           if top_rows else "")
        + "</div>")

    body = (f'<div class="lead">下方 KPI、图表、事件归因与重点负面帖全部由平台侧用库内真实采集数据现算生成'
            f'（声量口径：{ov.get("scope", "")}，实测 {sv.get("measured", 0)} 条 / '
            f'估算 {sv.get("imputed", 0)} 条，实测占比 {sv.get("measured_ratio", 0)}%）；'
            f'最后一节「模型分析」的文字由所选大模型基于同一份数据撰写，不参与任何数字计算。</div>'
            f'{kpis}{charts_html}'
            f'{_event_blocks(ov)}{_alert_blocks(ov)}{_hot_table(ov)}'
            f'<h2>模型分析</h2>{_sectionize(_mark_levels(md_to_html_fragment(md)))}')
    return HTML_TPL.format(title=title, meta=_meta_pills(meta), body=body,
                           stamp=datetime.now().strftime("%Y-%m-%d %H:%M"))


def _meta_pills(meta: str) -> str:
    """报告头部的元信息拆成一组小胶囊，比一长串「·」分隔的文字好读。"""
    import html as _h

    parts = [p.strip() for p in re.split(r"[·|｜]", meta or "") if p.strip()]
    if not parts:
        return _h.escape(meta or "")
    return "".join(f"<span>{_h.escape(p)}</span>" for p in parts)
