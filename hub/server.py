"""本地操作中台：浏览器里点按钮完成扫码登录、发起采集、看数据、导出。

只监听 127.0.0.1，没有鉴权——它能直接驱动你的登录态浏览器，所以别绑 0.0.0.0
或做端口转发暴露到局域网。

  python -m hub.cli serve            # 默认 http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import alerts, config, events, periods, session, sov, store, tagging
from .platforms import get, ordered

WEB_DIR = Path(__file__).resolve().parent / "web"

# 任务表：登录/采集都是长任务，前端轮询 /api/task
TASKS: dict[str, dict] = {}
_LOCK = threading.Lock()
# 同一平台的浏览器目录不能并发开两个，按平台串行
_PLAT_LOCKS: dict[str, threading.Lock] = {}


def _plat_lock(name: str) -> threading.Lock:
    with _LOCK:
        return _PLAT_LOCKS.setdefault(name, threading.Lock())


def new_task(kind: str, title: str) -> str:
    tid = f"{kind}-{int(time.time() * 1000)}"
    with _LOCK:
        TASKS[tid] = {"id": tid, "kind": kind, "title": title, "state": "running",
                      "log": [], "started": datetime.now().strftime("%H:%M:%S"),
                      # control 是任务的"遥控开关"：run / pause / cancel。
                      # 采集循环在每段开始前读它，所以暂停/取消都是**段边界生效**——
                      # 不会把正在跑的那一段浏览器操作硬切断，避免留下半截数据。
                      "control": "run", "result": {}}
    return tid


def set_control(tid: str, action: str) -> dict:
    with _LOCK:
        t = TASKS.get(tid)
        if not t:
            return {"ok": False, "msg": "任务不存在"}
        if t["state"] != "running":
            return {"ok": False, "msg": "任务已结束，无法操作"}
        t["control"] = {"pause": "pause", "resume": "run", "cancel": "cancel"}.get(action, "run")
        return {"ok": True, "control": t["control"]}


def control_of(tid: str) -> str:
    with _LOCK:
        return (TASKS.get(tid) or {}).get("control", "run")


def wait_if_paused(tid: str, on_log=None) -> str:
    """段边界的闸门：暂停就原地等，取消就返回 cancel 让调用方收尾。"""
    said = False
    while control_of(tid) == "pause":
        if not said and on_log:
            on_log("⏸ 已暂停，点「继续」恢复（浏览器会话保持打开，别停太久以免登录态超时）")
            said = True
        time.sleep(0.6)
    if said and on_log:
        on_log("▶ 已继续")
    return control_of(tid)


def say(tid: str, line: str):
    with _LOCK:
        TASKS[tid]["log"].append(f"{datetime.now():%H:%M:%S}  {line}")


def plan_progress(tid: str, items: list[dict]):
    """建立进度清单。items 里每项是一个「渠道 × 关键词 × 窗口」的待办。

    进度和日志分开存：日志是流水（给排查用），进度是状态机（给看板用）。
    前端轮询同一个 /api/task 就能同时拿到两者，不用再加一条轮询链路。
    """
    with _LOCK:
        TASKS[tid]["progress"] = {"items": items, "total": len(items),
                                  "done": 0, "ok": 0, "fail": 0, "saved": 0,
                                  "cur": ""}


def step(tid: str, key: tuple, state: str, **kw):
    """更新某一项的状态。state: wait / running / ok / empty / fail / skip"""
    with _LOCK:
        pg = TASKS[tid].get("progress")
        if not pg:
            return
        for it in pg["items"]:
            if (it["platform"], it["keyword"], it["window"]) != key:
                continue
            was_done = it["state"] not in ("wait", "running")
            it.update(state=state, **kw)
            if state == "running":
                pg["cur"] = f'{it["platform_label"]} × {it["keyword"]}'
            elif not was_done:
                pg["done"] += 1
                pg["ok"] += 1 if state in ("ok", "empty", "skip") else 0
                pg["fail"] += 1 if state == "fail" else 0
                pg["saved"] += int(kw.get("saved") or 0)
            break


def finish(tid: str, state: str, **result):
    with _LOCK:
        TASKS[tid]["state"] = state
        TASKS[tid]["result"] = result


def run_bg(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# ---------------------------------------------------------------- 后台任务

def task_login(tid: str, name: str):
    plat = get(name)
    lock = _plat_lock(name)
    if not lock.acquire(blocking=False):
        say(tid, f"[{plat.label}] 该平台已有任务占用浏览器目录，请等它结束")
        finish(tid, "error")
        return
    try:
        say(tid, f"[{plat.label}] 正在打开浏览器窗口，请在弹出的窗口里扫码或输入账号")
        say(tid, f"登录页：{plat.LOGIN_URL}（最长等待 {config.LOGIN_TIMEOUT_SEC} 秒）")
        say(tid, "登录窗口只会弹这一次；成功后采集全程无头静默，不再弹窗")
        ok = session.interactive_login(plat, on_log=lambda line: say(tid, line))
        if ok:
            say(tid, f"[{plat.label}] 登录成功，登录态已落盘，之后可无头静默采集")
            finish(tid, "done", logged_in=True)
        else:
            say(tid, f"[{plat.label}] 超时未检测到登录成功（窗口关太早或没扫完）")
            finish(tid, "error", logged_in=False)
    except Exception as exc:
        say(tid, f"异常：{exc}")
        say(tid, traceback.format_exc()[-1200:])
        finish(tid, "error")
    finally:
        lock.release()


def task_check(tid: str, names: list[str]):
    result = {}
    for name in names:
        plat = get(name)
        lock = _plat_lock(name)
        if not lock.acquire(blocking=False):
            say(tid, f"[{plat.label}] 浏览器目录被占用，跳过")
            continue
        try:
            if not plat.NEEDS_LOGIN:
                say(tid, f"[{plat.label}] 免登录平台，跳过校验")
                result[name] = True
                continue
            ok = session.check_login(plat)
            result[name] = ok
            say(tid, f"[{plat.label}] {'登录态可用' if ok else '需要登录'}")
        except Exception as exc:
            say(tid, f"[{plat.label}] 校验异常：{exc}")
        finally:
            lock.release()
    finish(tid, "done", **result)


def task_crawl(tid: str, params: dict):
    keywords = [k.strip() for k in params.get("keywords", []) if k.strip()]
    names = params.get("platforms") or [p.name for p in ordered()]
    limit = int(params.get("limit") or 50)
    since, until = params.get("since", ""), params.get("until", "")
    strict = params.get("strict_match", True)
    gap_only = bool(params.get("gap_only"))
    # 评论采集：默认不采。勾了才采，每条帖子最多采 comment_limit 条评论。
    with_comments = bool(params.get("with_comments"))
    comment_limit = int(params.get("comment_limit") or 30)
    total, dropped_all = 0, 0
    detail = []
    # 一次采集任务＝一个「采集计划」，所有平台×关键词的流水都挂在这个 batch_id 上，
    # 历史页就能按"这次抓了什么"归组，而不是几十条渠道流水各自一行
    batch = f"B{datetime.now():%Y%m%d%H%M%S}"
    dup_before = _dedupe_stat()["dup"]

    # 工作单：{平台: [(关键词, 起, 止), …]}。
    # 普通模式每个组合就是整段区间；增量模式按「渠道 × 关键词」各自的缺口切段，
    # 整段都抓过的渠道直接不进工作单——这才是"已爬过的渠道不再爬"。
    work: dict[str, list[tuple[str, str, str]]] = {}
    skipped: list[dict] = []
    if gap_only and since and until:
        plan = periods.plan_gaps(keywords, names, since, until)
        for c in plan["combos"]:
            for r in c["ranges"]:
                work.setdefault(c["platform"], []).append((c["keyword"], r["since"], r["until"]))
        skipped = plan["skip"]
        say(tid, f"采集计划号 {batch}｜模式：只补缺口（按渠道×关键词×天算覆盖）")
        say(tid, f"目标区间 {since} ~ {until}（{plan['combos'] and plan['combos'][0]['total_days'] or 0} 天）")
        say(tid, f"跳过 {len(skipped)} 个已完整覆盖的组合，需补 {len(plan['combos'])} 个组合、"
                 f"共 {plan['requests']} 段")
        for s in skipped:
            say(tid, f"  ⤼ 跳过 {get(s['platform']).label} × {s['keyword']}：{s['reason']}")
        for c in plan["combos"]:
            seg = "、".join(f"{r['since']}~{r['until']}" for r in c["ranges"])
            say(tid, f"  ⤵ 补 {get(c['platform']).label} × {c['keyword']}：缺 {c['missing_days']} 天"
                     f"｜{seg}{'｜' + c['note'] if c['note'] else ''}")
        if not work:
            # 全都覆盖过也要把"勾选清单"给出来，否则用户看不出系统到底核对了什么
            plan_progress(tid, [{
                "platform": sk["platform"], "platform_label": get(sk["platform"]).label,
                "keyword": sk["keyword"], "window": f"{since}~{until}", "state": "skip",
                "got": 0, "saved": 0, "msg": sk["reason"],
            } for sk in skipped])
            say(tid, "目标区间内所有渠道 × 关键词都已完整采集过，无需重复抓取。")
            finish(tid, "done", saved=0, dropped=0, detail=[], since=since, until=until,
                   keywords=keywords, combos=0, missing=0, skipped=len(skipped))
            return
    else:
        for n in names:
            for k in keywords:
                work[n] = work.get(n, []) + [(k, since, until)]
        say(tid, f"采集计划号 {batch}｜模式：全量重抓")
        say(tid, f"时间范围 {since or '不限'} ~ {until or '不限'}｜每平台每词上限 {limit}"
                 f"｜相关性闸门 {'开' if strict else '关'}")
        say(tid, f"计划：{len(names)} 平台 × {len(keywords)} 关键词 = "
                 f"{len(names) * len(keywords)} 个组合")
    say(tid, f"平台：{'、'.join(get(n).label for n in work)}")

    # 完整性台账：这次真正要跑的每一段都先占位，跑完还是"未执行"的就是漏了
    ledger: dict[tuple, str] = {(n, k, a, b): "未执行"
                                for n, jobs in work.items() for k, a, b in jobs}
    # 可视化进度：每个「渠道 × 关键词 × 窗口」一行，跑完打勾/打叉，前端实时刷
    plan_progress(tid, [{
        "platform": n, "platform_label": get(n).label, "keyword": k,
        "window": f"{a or '-'}~{b or '-'}", "state": "wait",
        "got": 0, "saved": 0, "msg": "",
    } for n, jobs in work.items() for k, a, b in jobs] + [{
        "platform": sk["platform"], "platform_label": get(sk["platform"]).label,
        "keyword": sk["keyword"], "window": f"{since}~{until}", "state": "skip",
        "got": 0, "saved": 0, "msg": sk["reason"],
    } for sk in skipped])
    cancelled = False
    for name, jobs in work.items():
        if cancelled or control_of(tid) == "cancel":
            cancelled = True
            for k, a, b in jobs:
                if ledger[(name, k, a, b)] == "未执行":
                    ledger[(name, k, a, b)] = "已取消"
                    step(tid, (name, k, f"{a or '-'}~{b or '-'}"), "skip", msg="任务已取消")
            continue
        plat = get(name)
        lock = _plat_lock(name)
        if not lock.acquire(blocking=False):
            say(tid, f"[{plat.label}] 浏览器目录被占用（可能正在登录），跳过")
            for k, a, b in jobs:
                ledger[(name, k, a, b)] = "跳过：浏览器目录被占用"
                step(tid, (name, k, f"{a or '-'}~{b or '-'}"), "fail", msg="浏览器目录被占用")
            continue
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            say(tid, f"=== [{plat.label}] {len(jobs)} 段任务，每段上限 {limit}")
            with session.SilentSession(plat, require_login=True, headless=True) as ctx:
                for kw, a, b in jobs:
                    span = f"{a or '-'}~{b or '-'}"
                    pkey = (name, kw, span)
                    # 段边界闸门：暂停在这里等，取消在这里退出
                    if wait_if_paused(tid, lambda m: say(tid, m)) == "cancel":
                        cancelled = True
                        ledger[pkey[0], kw, a, b] = "已取消"
                        step(tid, pkey, "skip", msg="任务已取消")
                        say(tid, f"  [{kw} {span}] 已取消，剩余段不再执行")
                        break
                    step(tid, pkey, "running")
                    def _log(g, s, note):        # noqa: E306  每段的窗口要如实记进流水
                        store.log_run(name, kw, g, s, started, note=note, batch_id=batch,
                                      since=a, until=b, limit_n=limit)
                    try:
                        posts = plat.search(ctx, kw, limit, a, b)
                    except Exception as exc:
                        say(tid, f"  [{kw} {span}] 采集异常：{exc}")
                        _log(0, 0, f"error: {exc}"[:200])
                        ledger[(name, kw, a, b)] = f"异常：{exc}"[:60]
                        step(tid, pkey, "fail", msg=f"采集异常：{exc}"[:60])
                        continue
                    got = len(posts)
                    if strict:
                        posts = [p for p in posts
                                 if tagging.keyword_hit(p.title, p.content, kw)]
                        dropped = got - len(posts)
                        dropped_all += dropped
                        if dropped:
                            say(tid, f"  [{kw} {span}] 相关性闸门剔除 {dropped} 条（正文与标题都没提到"
                                     f"「{kw}」或其核心词）")
                    saved = store.save(posts)
                    total += saved
                    eng = sum(p.engagement for p in posts)
                    say(tid, f"  [{kw} {span}] 抓到 {got} 条 → 入库 {saved} 条，互动量 {eng}")
                    # 评论采集：默认关，勾了「同时采评论」才进。放在入库之后，
                    # 因为评论按 post_id 挂靠，帖子得先落库。单条失败不影响整批。
                    if with_comments and posts:
                        if not getattr(plat, "COMMENTS_SUPPORTED", False):
                            say(tid, f"  [{kw} {span}] {plat.label} 暂不支持评论采集，已跳过评论")
                        else:
                            csaved = 0
                            for p in posts:
                                try:
                                    cs = plat.fetch_comments(ctx, p, comment_limit)
                                except Exception as exc:
                                    say(tid, f"    评论采集异常（{p.post_id[:12]}）：{exc}"[:80])
                                    continue
                                for c in cs:
                                    c.setdefault("platform", name)
                                    c.setdefault("post_id", p.post_id)
                                    c.setdefault("keyword", kw)
                                csaved += store.save_comments(cs)
                            if csaved:
                                say(tid, f"  [{kw} {span}] 评论入库 {csaved} 条")
                    _log(got, saved, f"{span} 上限{limit} 命中{len(posts)}/{got}")
                    detail.append({"platform": plat.label, "keyword": kw, "window": span,
                                   "got": got, "saved": saved, "engagement": eng})
                    ledger[(name, kw, a, b)] = (f"入库 {saved}/{got}" if got
                                                else "0 条：该渠道该词在这段没有结果")
                    step(tid, pkey, "ok" if got else "empty", got=got, saved=saved,
                         msg=(f"入库 {saved} / 抓到 {got}" if got else "这段没有结果"))
                    time.sleep(config.THROTTLE_SEC)
        except session.NeedLoginError as exc:
            say(tid, f"  {exc}")
            # 逐段记，别把关键词拼成一行：历史记录要能一行对一个「渠道×关键词×窗口」
            for kw, a, b in jobs:
                store.log_run(name, kw, 0, 0, started,
                              note=f"{a or '-'}~{b or '-'} need_login", batch_id=batch,
                              since=a, until=b, limit_n=limit)
                ledger[(name, kw, a, b)] = "未采集：需要扫码登录"
                step(tid, (name, kw, f"{a or '-'}~{b or '-'}"), "fail", msg="需要扫码登录")
        except Exception as exc:
            say(tid, f"  [{plat.label}] 会话失败：{exc}")
            for kw, a, b in jobs:
                if ledger[(name, kw, a, b)] == "未执行":
                    ledger[(name, kw, a, b)] = f"会话失败：{exc}"[:60]
                    step(tid, (name, kw, f"{a or '-'}~{b or '-'}"), "fail",
                         msg=f"会话失败：{exc}"[:60])
        finally:
            lock.release()
    # 完整性汇总：把每个组合的结果逐条列出来，缺的必须能看见
    say(tid, "———— 采集完整性汇总（渠道 × 关键词 × 窗口）————")
    miss = 0
    for (name, kw, a, b), state in ledger.items():
        ok = state.startswith("入库") and not state.startswith("入库 0/")
        if not ok:
            miss += 1
        say(tid, f"  {'✓' if ok else '✗'} {get(name).label} × {kw}"
                 f" [{a or '-'}~{b or '-'}]：{state}")
    if skipped:
        say(tid, f"  另有 {len(skipped)} 个组合本次跳过（已完整覆盖，不重复抓取）")
    if cancelled:
        say(tid, "⚠ 本次任务被手动取消，已抓到的部分已入库，未执行的段标记为「已取消」")
    # 内容级去重：store.save() 每批都会增量判重，这里把本次计划新增的重复数报出来
    dup_after = _dedupe_stat()["dup"]
    dup_new = max(0, dup_after - dup_before)
    say(tid, "———— 内容去重 ————")
    say(tid, f"  判重口径：{store.dedupe_note()}")
    say(tid, f"  本次计划新判为重复 {dup_new} 条（全库累计 {dup_after} 条），已从所有口径中排除")
    say(tid, f"合计入库 {total} 条（其中 {dup_new} 条与已有内容重复、不计入声量）；"
             f"执行 {len(ledger)} 段，有数据 {len(ledger) - miss} 段，无数据/未执行 {miss} 段"
             + (f"；跳过 {len(skipped)} 个已覆盖组合" if skipped else "")
             + (f"；相关性闸门共剔除 {dropped_all} 条" if dropped_all else ""))
    finish(tid, "cancelled" if cancelled else "done",
           saved=total, dropped=dropped_all, detail=detail,
           since=since, until=until, keywords=keywords, dup=dup_new,
           combos=len(ledger), missing=miss, skipped=len(skipped), cancelled=cancelled)


def task_report(tid: str, payload: dict):
    """跑大模型分析报告。放后台是因为一次生成常要 30~120 秒。"""
    from . import llm
    try:
        scope = payload.get("scope", "main")
        since, until = payload.get("since", ""), payload.get("until", "")
        ov = api_overview(since, until, payload.get("keyword", ""), scope)
        ov["since"], ov["until"] = since, until
        rows = store.query(None, payload.get("keyword", ""), since, until,
                           LAYER_SETS.get(scope, LAYER_SETS["main"]))
        cfg = llm.get_model(payload.get("model_id", ""))
        if not cfg:
            say(tid, "还没有接入模型：到「分析报告」页填 base_url + api_key + 模型名")
            finish(tid, "error")
            return
        say(tid, f"使用模型 {cfg['name']}（{cfg['model']}）")
        say(tid, f"口径 {scope}｜区间 {since}~{until}｜样本 {min(len(rows), 60)} / 共 {len(rows)} 条")
        say(tid, "正在请求模型，长报告可能要 1~2 分钟…")
        res = llm.generate_report(cfg["id"], ov, rows, payload.get("note", ""),
                                  out_fmt=payload.get("out_fmt", "md"))
        if not res.get("ok"):
            say(tid, res.get("msg", "生成失败"))
            finish(tid, "error")
            return
        say(tid, f"报告已生成：{res['file']}"
                 + (f"；同时产出网页版 {res['html_file']}" if res.get("html_file") else ""))
        finish(tid, "done", file=res["file"], html_file=res.get("html_file", ""))
    except Exception as exc:
        say(tid, f"生成失败：{exc}")
        finish(tid, "error")


# ---------------------------------------------------------------- 只读接口

def api_platforms() -> list[dict]:
    out = []
    for p in ordered():
        st = session.read_state(p.name)
        out.append({
            "name": p.name, "label": p.label, "needs_login": p.NEEDS_LOGIN,
            "login_optional": getattr(p, "LOGIN_OPTIONAL", False),
            "metrics": list(p.METRICS), "note": p.NOTE,
            "logged_in": bool(st.get("logged_in")),
            "reason": st.get("reason", ""),
            "last_login": st.get("last_login", ""), "last_check": st.get("last_check", ""),
            "login_url": p.LOGIN_URL,
        })
    return out


LAYER_SETS = {
    "main": ["A_UGC负反馈", "B_媒体负面报道"],
    "neg_plus": ["A_UGC负反馈", "B_媒体负面报道", "C_中性事件报道"],
    "valid": ["A_UGC负反馈", "B_媒体负面报道", "C_中性事件报道", "E_正面反馈"],
    "noise": ["D_噪音"],
    "positive": ["E_正面反馈"],
    "all": [],
}

# 口径与清洗说明：一处定义，看板/报告共用，避免同一套规则在页面上写三遍不同的话
SCOPE_DEFS = [
    ("main", "负面主口径", ["A_UGC负反馈", "B_媒体负面报道"],
     "只保留真正的负反馈，是对外汇报和风险预警的默认口径"),
    ("neg_plus", "主口径 ＋ 中性事件", ["A_UGC负反馈", "B_媒体负面报道", "C_中性事件报道"],
     "加上只转述事实的报道，用来看议题的整体讨论体量"),
    ("valid", "剔噪后全量", ["A_UGC负反馈", "B_媒体负面报道", "C_中性事件报道", "E_正面反馈"],
     "剔掉噪音后的全部有效内容；情感结构、声量换算、事件归因固定用这一档"),
    ("positive", "仅正面反馈", ["E_正面反馈"], "整治见效、体验变好的内容，用来验证治理效果"),
    ("noise", "噪音复核", ["D_噪音"], "被剔掉的内容，用来人工抽检规则有没有错杀"),
    ("all", "全部（含噪音）", [], "不做任何剔除，只用于核对总量"),
]

# 剔除动作：每条都写清「剔什么、按什么判、剔到哪一层」，可在「噪音复核」口径里逐条核对
CLEAN_STEPS = [
    ("采集时：相关性闸门", "标题与正文都没提到搜索词或其核心词的，采集阶段直接不入库",
     "tagging.keyword_hit()", "不入库（日志里报剔除条数）"),
    ("采集时：发布时间窗", "解析不出发布时间、或发布时间不在所选区间内的，一律丢弃",
     "models.in_window()", "不入库"),
    ("入库时：去重", "同一平台同一 post_id 只留一条，重复抓到就更新指标而不是新增",
     "PRIMARY KEY (platform, post_id)", "更新已有行"),
    ("入库时：内容级去重", "跨关键词、跨平台抓到的同一条内容（指纹一致）只留一条，"
     "留互动量最全的那条，其余标记 dup_of 并从所有口径里排除；只标记不删除，可逐条核对",
     "store.dedupe() · " + "指纹＝标题+作者+正文前N字+发布日期", "标记为重复，不进口径"),
    ("清洗：营销/教程类噪音", "推荐、测评、盘点、教你、赚钱、引流等推广或教程语气的内容",
     "clean.PROMO/NOISE 词表", "D_噪音"),
    ("清洗：负面证据判定", "有明确负面证据才算负面；歧义词（如「直接跳」「强制」）只在标题命中才算，"
     "媒体批评类词要标题命中或正文命中 ≥2 次，避免把中性报道判成负面",
     "clean.negative_evidence()", "决定 A/B 层还是 C 层"),
    ("清洗：正面判定收紧", "正面词只在标题命中且不带推广标记时才算正面，避免软文混进正面",
     "tagging.POS_WORDS + PROMO_MARK", "E_正面反馈"),
    ("清洗：主体 × 情感定层", "政务/官媒/媒体/平台号的负面 → B 层；大V/自媒体/普通用户的负面 → A 层；"
     "只转述事实 → C 层", "tagging.derive_layer()", "A / B / C 层"),
    ("人工复核：改判", "人工把某条改成负面/中性/正面，写 manual_sentiment 并同步改判分层，"
     "重新分层不会覆盖它", "store.set_manual()", "覆盖规则结论"),
    ("人工复核：删除", "误采、无关、重复的内容软删除（deleted=1），重新采集也不会复活",
     "store.set_deleted()", "从所有口径中排除"),
]


def _dedupe_stat() -> dict:
    with store.conn() as c:
        dup = c.execute("SELECT COUNT(*) FROM posts WHERE COALESCE(dup_of,'') != ''").fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    return {"dup": dup, "raw": total, "note": store.dedupe_note()}


def clean_rules() -> dict:
    """口径与清洗规则的结构化说明，给前端「口径说明」抽屉和报告用。"""
    return {
        "scopes": [{"key": k, "name": n, "layers": ls, "why": w}
                   for k, n, ls, w in SCOPE_DEFS],
        "steps": [{"stage": s, "what": w, "how": h, "to": t}
                  for s, w, h, t in CLEAN_STEPS],
        "layers": {
            "A_UGC负反馈": "大V/自媒体/普通用户发的负面反馈，是用户真实抱怨的主体",
            "B_媒体负面报道": "政务、官媒、持证媒体、平台企业号发的负面内容，扩散权重更高",
            "C_中性事件报道": "只转述事实、不带情绪的内容，不计入负反馈",
            "D_噪音": "营销软文、教程、赚钱引流等与负反馈无关的内容，已剔除",
            "E_正面反馈": "整治见效、体验变好的内容",
        },
    }


def api_overview(since: str, until: str, keyword: str, scope: str = "main",
                 keywords: list[str] | None = None,
                 plats: list[str] | None = None) -> dict:
    layers = LAYER_SETS.get(scope, LAYER_SETS["main"])
    kw = dict(keyword=keyword, keywords=keywords, platforms=plats)
    rows = store.query(layers=layers, since=since, until=until, **kw)
    all_rows = store.query(layers=None, since=since, until=until, **kw)
    # 主体/情感维度固定看"剔噪后的有效全量"：主口径本身只含负面层，
    # 在它上面统计情感只会得到 100% 负面，看不出结构。
    valid_rows = store.query(layers=LAYER_SETS["valid"], since=since, until=until, **kw)
    by_plat: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    for r in rows:
        label = r.get("platform_label") or r.get("platform")
        a = by_plat.setdefault(label, {"平台": label, "帖子数": 0, "互动量": 0,
                                       "点赞": 0, "评论": 0, "转发": 0, "收藏": 0})
        a["帖子数"] += 1
        a["互动量"] += r.get("engagement") or 0
        a["点赞"] += r.get("likes") or 0
        a["评论"] += r.get("comments") or 0
        a["转发"] += r.get("shares") or 0
        a["收藏"] += r.get("collects") or 0
        day = (r.get("publish_time") or "")[:10]
        if len(day) == 10:
            d = by_day.setdefault(day, {})
            d[label] = d.get(label, 0) + 1
    labels = sorted(by_plat, key=lambda k: -by_plat[k]["帖子数"])
    trend = [{"date": d, **by_day[d]} for d in sorted(by_day)]
    funnel: dict[str, int] = {}
    for r in all_rows:
        funnel[r.get("layer") or "未分层"] = funnel.get(r.get("layer") or "未分层", 0) + 1
    with store.conn() as c:
        c.row_factory = sqlite3.Row
        runs = [dict(r) for r in c.execute(
            "SELECT * FROM runs ORDER BY finished_at DESC LIMIT 12").fetchall()]
    # 负面影响力 + 事件归因：都在"剔噪后全量"上算，跟情感/声量同一批数据
    impact_meta = events.annotate_impact(valid_rows)
    return {"total": len(rows), "engagement": sum(r.get("engagement") or 0 for r in rows),
            "raw_total": len(all_rows), "funnel": funnel, "scope": scope,
            "by_platform": [by_plat[k] for k in labels], "labels": labels,
            "trend": trend, "runs": runs,
            "top_authors": _top_authors(rows),
            "layer_by_platform": _layer_matrix(all_rows),
            "by_actor": _dim_stat(valid_rows, "actor", tagging.ACTORS),
            "by_sentiment": _dim_stat(valid_rows, "sentiment", tagging.SENTIMENTS),
            "actor_sentiment": _cross_stat(valid_rows),
            "sentiment_trend": _sentiment_trend(valid_rows),
            "valid_total": len(valid_rows),
            # 声量换算固定用剔噪后全量，且和上面的主体/情感统计同一批数据，口径对得上
            "sov": sov.summarize(valid_rows),
            "sov_note": sov.FORMULA_NOTE,
            "coverage": _coverage(all_rows, rows),
            "prev": _prev_compare(since, until, keyword, scope, rows, valid_rows),
            "risks": _risks(rows, valid_rows, since, until),
            "impact": impact_meta,
            # 重点预警：必须放在 annotate_impact 之后，它要用 impact / impact_high
            "alerts": alerts.summarize(valid_rows),
            "events": events.attribute(valid_rows),
            # 峰值日 + 当天引爆帖：趋势图上直接标出来，不用再去明细里翻"那天发生了什么"
            "spikes": events.spikes(valid_rows, trend),
            "hot_negative": events.hot_negative(valid_rows, 12),
            "clean_rules": clean_rules(),
            "dedupe": _dedupe_stat(),
            "syndication": store.syndication(valid_rows),
            "keywords": sorted({r.get("keyword") or "" for r in rows} - {""}),
            # 库里全部关键词（不受当前筛选影响），给顶栏下拉用
            "all_keywords": _all_keywords()}


def _all_keywords() -> list[str]:
    with store.conn() as c:
        rows = c.execute("SELECT DISTINCT keyword FROM posts WHERE keyword != ''").fetchall()
    return sorted(r[0] for r in rows)


def _shift(since: str, until: str) -> tuple[str, str]:
    """把区间整体往前挪一个等长周期，用来算环比（上期）。"""
    from datetime import date, timedelta
    try:
        a, b = date.fromisoformat(since), date.fromisoformat(until)
    except ValueError:
        return "", ""
    span = (b - a).days + 1
    return (a - timedelta(days=span)).isoformat(), (a - timedelta(days=1)).isoformat()


def _prev_compare(since: str, until: str, keyword: str, scope: str,
                  rows: list[dict], valid_rows: list[dict]) -> dict:
    """环比：同长度的上一周期。没有上期数据时返回 null，前端就不显示箭头——
    宁可不显示，也不要拿 0 当基数算出「+∞%」这种假指标。"""
    p_since, p_until = _shift(since, until)
    if not p_since:
        return {}
    layers = LAYER_SETS.get(scope, LAYER_SETS["main"])
    prev = store.query(None, keyword, p_since, p_until, layers)
    prev_valid = store.query(None, keyword, p_since, p_until, LAYER_SETS["valid"])
    cur_sov = sov.summarize(valid_rows) if valid_rows else {}
    prv_sov = sov.summarize(prev_valid) if prev_valid else {}

    def delta(cur, prv):
        if not prv:
            return None
        return round((cur - prv) / prv * 100, 1)

    return {
        "window": f"{p_since} ~ {p_until}",
        "total": len(prev),
        "total_delta": delta(len(rows), len(prev)),
        "engagement": sum(r.get("engagement") or 0 for r in prev),
        "engagement_delta": delta(sum(r.get("engagement") or 0 for r in rows),
                                  sum(r.get("engagement") or 0 for r in prev)),
        "neg_sov": prv_sov.get("neg_sov", 0),
        "neg_sov_delta": delta(cur_sov.get("neg_sov", 0) or 0, prv_sov.get("neg_sov", 0) or 0),
    }


def _risks(rows: list[dict], valid_rows: list[dict], since: str, until: str) -> list[dict]:
    """风险预警：全部由真实数据算出来，每条都带可核对的依据，不写死文案。"""
    from datetime import date
    out = []
    p_since, p_until = _shift(since, until)
    if p_since:
        prev = store.query(None, "", p_since, p_until, LAYER_SETS["main"])
        if len(prev) >= 10:
            d = (len(rows) - len(prev)) / len(prev) * 100
            if d >= 30:
                out.append({"level": "高风险", "title": "负面声量环比激增",
                            "detail": f"本期 {len(rows)} 条 vs 上期 {len(prev)} 条，环比 +{d:.1f}%",
                            "action": "立即处理"})
            elif d >= 10:
                out.append({"level": "中风险", "title": "负面内容上升",
                            "detail": f"本期 {len(rows)} 条 vs 上期 {len(prev)} 条，环比 +{d:.1f}%",
                            "action": "重点关注"})
    gov = [r for r in rows if r.get("actor") in ("官方机构", "官方媒体")
           and r.get("sentiment") == "负面"]
    if gov:
        who = "、".join(dict.fromkeys(r.get("author") or "-" for r in gov[:3]))
        out.append({"level": "高风险", "title": "监管/官媒已介入",
                    "detail": f"{len(gov)} 条来自官方机构或官方媒体的负面内容（{who}）",
                    "action": "立即处理"})
    hot = sorted((r for r in rows if r.get("sentiment") == "负面"),
                 key=lambda r: -(r.get("engagement") or 0))[:1]
    if hot and (hot[0].get("engagement") or 0) >= 5000:
        out.append({"level": "中风险", "title": "存在高扩散单帖",
                    "detail": (f"{hot[0].get('platform_label')}「{(hot[0].get('title') or '')[:22]}」"
                               f"互动量 {hot[0].get('engagement'):,}"),
                    "action": "重点关注"})
    by_plat: dict[str, list] = {}
    for r in valid_rows:
        by_plat.setdefault(r.get("platform_label") or "-", []).append(r)
    for label, items in by_plat.items():
        neg = [r for r in items if r.get("sentiment") == "负面"]
        if len(items) >= 20 and len(neg) / len(items) >= 0.8:
            out.append({"level": "中风险", "title": f"{label} 负面高度集中",
                        "detail": f"{label} 有效内容 {len(items)} 条，其中负面 {len(neg)} 条"
                                  f"（{len(neg) / len(items) * 100:.0f}%）",
                        "action": "查看详情"})
    days = sorted({(r.get("publish_time") or "")[:10] for r in rows} - {""})
    if days:
        try:
            gap = (date.today() - date.fromisoformat(days[-1])).days
            if gap > 7:
                out.append({"level": "低风险", "title": "数据不够新",
                            "detail": f"最新一条发布于 {days[-1]}，距今 {gap} 天，建议重跑采集",
                            "action": "持续观察"})
        except ValueError:
            pass
    order = {"高风险": 0, "中风险": 1, "低风险": 2}
    return sorted(out, key=lambda x: order.get(x["level"], 9))[:6]


def _dim_stat(rows: list[dict], field: str, order: list[str]) -> list[dict]:
    """按某个维度（主体 / 情感）统计条数与互动量，输出按给定顺序排。"""
    agg: dict[str, dict] = {}
    for r in rows:
        k = r.get(field) or "未判定"
        a = agg.setdefault(k, {"name": k, "posts": 0, "engagement": 0})
        a["posts"] += 1
        a["engagement"] += r.get("engagement") or 0
    keys = [k for k in order if k in agg] + [k for k in agg if k not in order]
    return [agg[k] for k in keys]


def _cross_stat(rows: list[dict]) -> dict:
    """主体 × 情感交叉表：定舆情等级看的就是"谁在说负面"。"""
    mat: dict[str, dict[str, int]] = {}
    for r in rows:
        a = r.get("actor") or "未判定"
        s = r.get("sentiment") or "未判定"
        mat.setdefault(a, {})
        mat[a][s] = mat[a].get(s, 0) + 1
    return mat


def _sentiment_trend(rows: list[dict]) -> list[dict]:
    """按日的负面/中性/正面条数，用来看倾向随时间的变化。"""
    by_day: dict[str, dict] = {}
    for r in rows:
        day = (r.get("publish_time") or "")[:10]
        if len(day) != 10:
            continue
        d = by_day.setdefault(day, {s: 0 for s in tagging.SENTIMENTS})
        s = r.get("sentiment") or "中性"
        d[s] = d.get(s, 0) + 1
    return [{"date": d, **by_day[d]} for d in sorted(by_day)]


def _top_authors(rows: list[dict], top: int = 12) -> list[dict]:
    """按互动量排作者。作者重名概率低，但同名跨平台会各算一份，所以带上平台。"""
    agg: dict[tuple, dict] = {}
    for r in rows:
        name = (r.get("author") or "").strip()
        if not name:
            continue
        key = (name, r.get("platform_label") or "")
        a = agg.setdefault(key, {"author": name, "platform_label": key[1],
                                 "posts": 0, "engagement": 0})
        a["posts"] += 1
        a["engagement"] += r.get("engagement") or 0
    return sorted(agg.values(), key=lambda x: (-x["engagement"], -x["posts"]))[:top]


def _layer_matrix(rows: list[dict]) -> dict:
    """平台 × 分层矩阵：用来看"哪个平台噪音最多"，判断清洗规则有没有偏。"""
    mat: dict[str, dict[str, int]] = {}
    for r in rows:
        label = r.get("platform_label") or r.get("platform")
        mat.setdefault(label, {})
        k = r.get("layer") or "未分层"
        mat[label][k] = mat[label].get(k, 0) + 1
    return mat


def _coverage(all_rows: list[dict], rows: list[dict]) -> list[dict]:
    """平台覆盖与新鲜度：条数、有发布时间的比例、覆盖了多少天、最新发布日、最近采集时间。

    "不够全 / 不够新"必须能量化，否则只能靠感觉。dated_ratio 低说明该平台入口
    给不出发布时间（会被严格时间过滤挡掉），days 少说明切片深度不够。
    """
    keep = {(r.get("platform"), r.get("post_id")) for r in rows}
    out: dict[str, dict] = {}
    for r in all_rows:
        label = r.get("platform_label") or r.get("platform")
        a = out.setdefault(label, {"平台": label, "入库": 0, "当前口径": 0, "有时间": 0,
                                   "days": set(), "最新发布": "", "最近采集": ""})
        a["入库"] += 1
        if (r.get("platform"), r.get("post_id")) in keep:
            a["当前口径"] += 1
        day = (r.get("publish_time") or "")[:10]
        if len(day) == 10:
            a["有时间"] += 1
            a["days"].add(day)
            a["最新发布"] = max(a["最新发布"], day)
        a["最近采集"] = max(a["最近采集"], r.get("fetched_at") or "")
    res = []
    for a in out.values():
        a["覆盖天数"] = len(a.pop("days"))
        a["有时间占比"] = round(a["有时间"] / a["入库"] * 100) if a["入库"] else 0
        res.append(a)
    return sorted(res, key=lambda x: -x["入库"])


def api_posts(since: str, until: str, keyword: str, plats: list[str], limit: int,
              scope: str = "main", q: str = "",
              keywords: list[str] | None = None) -> list[dict]:
    rows = store.query(plats or None, keyword, since, until,
                       LAYER_SETS.get(scope, LAYER_SETS["main"]),
                       keywords=keywords)
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows
                if needle in (r.get("title") or "").lower()
                or needle in (r.get("author") or "").lower()
                or needle in (r.get("content") or "").lower()]
    # 影响力要在"这批数据"上算门槛，所以在这里现算一次（跟看板同一套函数）
    events.annotate_impact(rows)
    # platform + post_id 是人工改判/删除的主键，必须回给前端
    keep = ("platform", "post_id", "platform_label", "publish_time", "author", "title",
            "url", "likes", "comments", "shares", "collects", "views", "engagement",
            "keyword", "layer", "actor", "sentiment", "manual_sentiment", "manual_layer",
            "manual_at", "impact", "impact_high",
            # 内容类型（规则判）+ 语义裁决（模型判）：让人能在明细里看出
            # 「这条是谁判的、判成了什么类型」，而不是只看到一个分层结果
            "topic_kind", "ai_sentiment", "ai_topic", "ai_competitor",
            # 生效分层/情感：表格显示这两个，跟口径筛选用的是同一套值
            "eff_layer", "eff_sentiment", "text_thin")
    # 三个 *_reason 是判定依据，只在 hover 提示里出现，但它们占了整个响应体的大头
    # （5000 条时实测 590KB → 截断后 ~300KB，首屏快一半）。截到 90 字够看清判定原因。
    reasons = ("layer_reason", "actor_reason", "sentiment_reason", "ai_reason",
               "topic_reason")
    # 空值字段直接不回：manual_* 在 99% 的行上是空串，但 JSON 里每行都要付一遍键名的字节，
    # 649 行就是 40KB 纯键名。前端读的时候用 `r.manual_sentiment` 取到 undefined，效果一样。
    ccount = store.comment_counts()
    out = []
    for r in rows[:limit]:
        d = {}
        for k in keep:
            v = r.get(k)
            if v not in (None, "", 0, False) or k in ("engagement", "impact"):
                d[k] = v
        for k in reasons:
            v = r.get(k) or ""
            if v:
                d[k] = v[:90] + "…" if len(v) > 90 else v
        # 采到评论的帖子挂个数字，前端标角标、可点开看
        n = ccount.get(f"{r.get('platform')}|{r.get('post_id')}")
        if n:
            d["comment_n"] = n
        out.append(d)
    return out


def api_exports() -> list[dict]:
    out = []
    for f in sorted(config.EXPORT_DIR.glob("*"), key=lambda p: -p.stat().st_mtime):
        if f.is_file():
            out.append({"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1),
                        "mtime": datetime.fromtimestamp(f.stat().st_mtime)
                        .strftime("%Y-%m-%d %H:%M")})
    return out[:40]


def api_wordcloud(since: str, until: str, keyword: str, scope: str = "main",
                  top: int = 90) -> list[dict]:
    from . import wordcloud
    rows = store.query(None, keyword, since, until,
                       LAYER_SETS.get(scope, LAYER_SETS["main"]))
    return wordcloud.extract(rows, top=top)


def api_runs(limit: int = 200) -> list[dict]:
    """采集历史：runs 表就是每次「平台 × 关键词」的执行记录，note 里带时间范围与命中数。"""
    with store.conn() as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM runs ORDER BY finished_at DESC LIMIT ?", (limit,))]
    labels = {p.name: p.label for p in ordered()}
    for r in rows:
        r["platform_label"] = labels.get(r.get("platform"), r.get("platform"))
        note = r.get("note") or ""
        head = note.split(" ")[0]
        r["window"] = head if "~" in head else ""
        if not r["window"] and r.get("since_day"):
            r["window"] = f"{r['since_day']}~{r['until_day']}"
        r["failed"] = note.startswith("error") or "need_login" in note
    return rows


def api_plans(limit: int = 600) -> list[dict]:
    """采集计划（批次）视图。

    用户关心的是"这一次抓了什么"，而不是几十条渠道流水，所以这里把 runs 按
    batch_id 归成计划。老数据没有 batch_id（每个平台各自一个 started_at），
    只能推断：按「同一小时 + 同一发布时间窗」合并，并打上 legacy 标记说明这是推断的，
    不假装它和新记录一样准。
    """
    # limit 是"要几个计划"，不是"要几条流水"——一个计划可能含几十条渠道流水，
    # 直接把 limit 传给 api_runs 会出现"limit=5 只回 1 个计划"这种对不上的结果
    runs = api_runs(max(limit * 30, 600))
    plans: dict[str, dict] = {}
    for r in runs:
        if r.get("batch_id"):
            key = r["batch_id"]
        else:
            key = f"legacy:{(r.get('started_at') or '')[:13]}:{r.get('window') or ''}"
        p = plans.setdefault(key, {
            "id": key, "legacy": not r.get("batch_id"),
            "started_at": r.get("started_at") or "", "finished_at": r.get("finished_at") or "",
            "since": r.get("since_day") or "", "until": r.get("until_day") or "",
            "limit_n": r.get("limit_n") or 0, "runs": [],
        })
        p["runs"].append(r)
        p["started_at"] = min(p["started_at"] or r["started_at"], r["started_at"] or "")
        p["finished_at"] = max(p["finished_at"] or "", r.get("finished_at") or "")
        if not p["since"] and r.get("window") and "~" in r["window"]:
            a, b = r["window"].split("~", 1)
            p["since"], p["until"] = a.strip(), b.strip()

    out = []
    for p in plans.values():
        rs = p["runs"]
        kws = sorted({(r.get("keyword") or "").strip() for r in rs} - {""})
        plats = sorted({r["platform_label"] for r in rs})
        saved = sum(r.get("saved") or 0 for r in rs)
        got = sum(r.get("got") or 0 for r in rs)
        ok = [r for r in rs if not r["failed"] and (r.get("saved") or 0) > 0]
        failed = [r for r in rs if r["failed"]]
        out.append({
            **{k: p[k] for k in ("id", "legacy", "started_at", "finished_at",
                                 "since", "until", "limit_n")},
            "keywords": kws, "platforms": plats,
            "combos": len(rs), "ok_combos": len(ok), "failed_combos": len(failed),
            "got": got, "saved": saved,
            "window": f"{p['since']}~{p['until']}" if p["since"] else "",
            # 渠道明细：归到计划里的下一层，前端展开才看
            "channels": [{
                "platform": r.get("platform"), "platform_label": r["platform_label"],
                "keyword": r.get("keyword") or "", "got": r.get("got") or 0,
                "saved": r.get("saved") or 0, "failed": r["failed"],
                "at": (r.get("finished_at") or "")[11:16],
                "note": (r.get("note") or "")[:80],
            } for r in sorted(rs, key=lambda x: (x["platform_label"], x.get("keyword") or ""))],
        })
    out.sort(key=lambda p: p["finished_at"], reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "crawler_hub"

    def log_message(self, fmt, *args):  # 别把每个轮询都刷到终端
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel: str):
        path = (WEB_DIR / rel).resolve()
        if not str(path).startswith(str(WEB_DIR)) or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            return self._static("index.html")
        if u.path.startswith("/vendor/") or u.path.startswith("/logos/") \
                or u.path.endswith((".js", ".css", ".svg", ".png")):
            return self._static(u.path.lstrip("/"))
        if u.path == "/api/platforms":
            return self._json(api_platforms())
        if u.path == "/api/overview":
            kws = [x for x in q.get("keywords", "").split("|") if x]
            plats = [x for x in q.get("platforms", "").split(",") if x]
            return self._json(api_overview(q.get("since", ""), q.get("until", ""),
                                           q.get("keyword", ""), q.get("scope", "main"),
                                           kws or None, plats or None))
        if u.path == "/api/posts":
            plats = [x for x in q.get("platforms", "").split(",") if x]
            kws = [x for x in q.get("keywords", "").split("|") if x]
            return self._json(api_posts(q.get("since", ""), q.get("until", ""),
                                        q.get("keyword", ""), plats,
                                        int(q.get("limit") or 300),
                                        q.get("scope", "main"), q.get("q", ""),
                                        kws or None))
        if u.path == "/api/exports":
            return self._json(api_exports())
        if u.path == "/api/wordcloud":
            return self._json(api_wordcloud(q.get("since", ""), q.get("until", ""),
                                            q.get("keyword", ""), q.get("scope", "main"),
                                            int(q.get("top") or 90)))
        if u.path == "/api/runs":
            return self._json(api_runs(int(q.get("limit") or 200)))
        if u.path == "/api/plans":
            return self._json(api_plans(int(q.get("limit") or 600)))
        if u.path == "/api/compare":
            # 周期对比：grain = week / month / 数字（自定义天数）
            return self._json(periods.compare(
                q.get("since", ""), q.get("until", ""), q.get("grain", "week"),
                q.get("keyword", ""),
                LAYER_SETS.get(q.get("scope", "main"), LAYER_SETS["main"])))
        if u.path == "/api/coverage":
            kws = [x for x in q.get("keywords", "").split("|") if x] or _all_keywords()
            plats = [x for x in q.get("platforms", "").split(",") if x] \
                or [p.name for p in ordered()]
            return self._json({
                "rows": periods.coverage_summary(kws, q.get("since", ""), q.get("until", ""),
                                                 plats, q.get("strict", "1") == "1"),
                "labels": {p.name: p.label for p in ordered()},
            })
        if u.path == "/api/gap-plan":
            kws = [x for x in q.get("keywords", "").split("|") if x]
            plats = [x for x in q.get("platforms", "").split(",") if x] \
                or [p.name for p in ordered()]
            plan = periods.plan_gaps(kws, plats, q.get("since", ""), q.get("until", ""))
            labels = {p.name: p.label for p in ordered()}
            for c in plan["combos"]:
                c["platform_label"] = labels.get(c["platform"], c["platform"])
            for sk in plan["skip"]:
                sk["platform_label"] = labels.get(sk["platform"], sk["platform"])
            return self._json(plan)
        if u.path == "/api/clean-rules":
            return self._json(clean_rules())
        if u.path == "/api/dups":
            return self._json({"note": store.dedupe_note(),
                               "stat": _dedupe_stat(),
                               "rows": store.dup_list(int(q.get("limit") or 300))})
        if u.path == "/api/deleted":
            # 人工删掉的内容清单，给"回收站"用——软删除必须有地方能看见、能恢复
            with store.conn() as c:
                c.row_factory = sqlite3.Row
                rows = [dict(r) for r in c.execute(
                    "SELECT platform, platform_label, post_id, title, author, publish_time,"
                    " engagement, keyword, sentiment FROM posts WHERE COALESCE(deleted,0)=1"
                    " ORDER BY engagement DESC LIMIT 300")]
            return self._json(rows)
        if u.path == "/api/comments":
            # 点开某条帖子看它的评论（评论者 + 时间 + 内容）
            return self._json(store.comments_of(
                q.get("platform", ""), q.get("post_id", ""),
                int(q.get("limit") or 300)))
        if u.path == "/api/settings":
            from . import settings as st
            return self._json({"current": st.all(), "defaults": st.DEFAULTS})
        if u.path == "/api/llm/models":
            from . import llm
            return self._json({**llm.list_models(), "presets": llm.PRESETS})
        if u.path == "/api/reports":
            from . import llm
            return self._json(llm.list_reports())
        if u.path == "/api/report":
            from . import llm
            return self._json({"text": llm.read_report((q.get("name") or "").strip())})
        if u.path == "/api/report/download":
            from . import llm
            name = (q.get("name") or "").strip()
            path = llm.any_file(name)
            if not path:
                return self.send_error(404)
            body = path.read_bytes()
            inline = path.suffix in (".html", ".pdf") and q.get("inline") == "1"
            ctype = {".html": "text/html; charset=utf-8", ".pdf": "application/pdf",
                     ".txt": "text/plain; charset=utf-8",
                     ".md": "text/markdown; charset=utf-8"}.get(path.suffix,
                                                                "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if not inline:
                self.send_header("Content-Disposition",
                                 f"attachment; filename*=UTF-8''{quote(path.name)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if u.path == "/api/download":
            name = (q.get("file") or "").strip()
            path = (config.EXPORT_DIR / name).resolve()
            # 只允许下载导出目录里的文件，防目录穿越
            if not str(path).startswith(str(config.EXPORT_DIR.resolve())) \
                    or not path.is_file():
                return self.send_error(404)
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(path.name)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if u.path == "/api/task":
            with _LOCK:
                t = TASKS.get(q.get("id", ""))
                return self._json(t or {"state": "unknown", "log": []})
        if u.path == "/api/tasks":
            with _LOCK:
                return self._json([{k: v for k, v in t.items() if k != "log"}
                                   for t in list(TASKS.values())[-20:]][::-1])
        if u.path == "/api/plan/export":
            # 按采集计划导出：用户要的是"点这一批就把这一批导出来"，
            # 不用再去看板上对着日期区间手调一遍（区间还对不准——
            # 一次计划抓的是"发布时间在某窗口内"的内容，跟"哪天采的"是两件事）
            plans = {p["id"]: p for p in api_plans(2000)}
            plan = plans.get(q.get("id", ""))
            if not plan:
                return self._json({"ok": False, "msg": "找不到这次采集计划"})
            runs = [r for r in api_runs(20000)
                    if (r.get("batch_id") or
                        f"legacy:{(r.get('started_at') or '')[:13]}:{r.get('window') or ''}")
                    == plan["id"]]
            rows = store.query_by_runs(runs)
            if not rows:
                return self._json({"ok": False,
                                   "msg": "这次计划没有反查到帖子（可能已被删除或被后续采集覆盖）"})
            fmt = (q.get("fmt") or "xlsx").lower()
            tag = (plan["finished_at"] or "").replace("-", "").replace(":", "").replace(" ", "_")
            stem = f"采集计划_{tag[:13]}"
            if fmt == "csv":
                path = store.export_csv(rows, config.EXPORT_DIR / f"{stem}.csv")
            elif fmt == "jsonl":
                path = store.export_jsonl(rows, config.EXPORT_DIR / f"{stem}.jsonl")
            else:
                path = store.export_xlsx(rows, config.EXPORT_DIR / f"{stem}.xlsx")
            return self._json({"ok": True, "rows": len(rows), "file": path.name,
                               "files": [str(path)]})
        if u.path == "/api/export":
            plats = [x for x in q.get("platforms", "").split(",") if x]
            rows = store.query(plats or None, q.get("keyword", ""),
                               q.get("since", ""), q.get("until", ""),
                               LAYER_SETS.get(q.get("scope", "main"), LAYER_SETS["main"]))
            if not rows:
                return self._json({"ok": False, "msg": "当前筛选条件下没有数据"})
            paths = [str(store.export_xlsx(rows))]
            if q.get("jsonl") == "1":
                paths.append(str(store.export_jsonl(rows)))
            return self._json({"ok": True, "rows": len(rows), "files": paths})
        self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if u.path == "/api/dedupe":
            # 判重口径改了要全库重算指纹再判一次
            n = store.rebuild_dup_keys()
            r = store.dedupe()
            return self._json({"ok": True, "rekeyed": n, **r, "note": store.dedupe_note()})
        if u.path == "/api/task/control":
            # 暂停 / 继续 / 取消。都在"段边界"生效，不会硬切断正在跑的那一段
            return self._json(set_control(payload.get("id", ""), payload.get("action", "")))
        if u.path == "/api/plan/delete":
            # 删采集计划记录＝删 runs 里这个批次的流水。
            # **不删帖子**：posts 里没有批次归属字段，硬按「平台×关键词×时间窗」反查会连带
            # 删掉别的计划抓到的同一批内容。数据在库里是资产，记录只是台账，分开处理更安全。
            bid = (payload.get("batch_id") or "").strip()
            if not bid:
                return self._json({"ok": False, "msg": "缺少计划号"})
            with store.conn() as c:
                if bid.startswith("legacy:"):
                    # 老记录没有 batch_id，页面上的分组是按「同一小时 + 同一时间窗」推断的，
                    # 删除时用同样的条件反查，保证删掉的就是卡上显示的那一组
                    parts = bid.split(":")
                    hour = ":".join(parts[1:3]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
                    n = c.execute(
                        "DELETE FROM runs WHERE COALESCE(batch_id,'')=''"
                        " AND substr(COALESCE(started_at,''),1,13)=?", (hour,)).rowcount
                else:
                    n = c.execute("DELETE FROM runs WHERE batch_id=?", (bid,)).rowcount
            return self._json({"ok": bool(n), "deleted": n,
                               "msg": f"已删除 {n} 条渠道流水（库里的帖子数据不受影响）"})
        if u.path == "/api/post/tag":
            # 人工改判情感。传空串＝撤销，回到规则判定。
            # 分层跟着一起改：不然把一条改成"正面"后，只含负面层的主口径还会显示它。
            sent = (payload.get("sentiment") or "").strip()
            if sent and sent not in tagging.SENTIMENTS:
                return self._json({"ok": False, "msg": f"情感只能是 {tagging.SENTIMENTS}"})
            layer = ""
            if sent:
                cur = store.get_post(payload.get("platform", ""),
                                     str(payload.get("post_id", "")))
                actor = (cur or {}).get("actor") or "普通用户"
                layer, _why = tagging.derive_layer(actor, sent, True, "")
            r = store.set_manual(payload.get("platform", ""), str(payload.get("post_id", "")),
                                 sent, layer)
            return self._json(r)
        if u.path == "/api/post/delete":
            r = store.set_deleted(payload.get("platform", ""),
                                  str(payload.get("post_id", "")),
                                  bool(payload.get("deleted", True)))
            return self._json(r)
        if u.path == "/api/login":
            name = payload.get("platform", "")
            tid = new_task("login", f"登录 {get(name).label}")
            run_bg(task_login, tid, name)
            return self._json({"task": tid})
        if u.path == "/api/check":
            names = payload.get("platforms") or [p.name for p in ordered()]
            tid = new_task("check", "登录态体检")
            run_bg(task_check, tid, names)
            return self._json({"task": tid})
        if u.path == "/api/crawl":
            if not [k for k in payload.get("keywords", []) if k.strip()]:
                return self._json({"error": "至少填一个关键词"}, 400)
            tid = new_task("crawl", "采集 " + "/".join(payload.get("keywords", [])))
            run_bg(task_crawl, tid, payload)
            return self._json({"task": tid})
        if u.path == "/api/reclean":
            stat = store.reclassify(force=bool(payload.get("force", True)))
            return self._json({"ok": True, **stat})
        if u.path == "/api/settings":
            from . import settings as st
            before = st.get("dedupe")
            cur = st.reset() if payload.get("reset") else st.save(payload.get("patch") or {})
            # 判重口径变了必须立刻重算，否则页面上写的是新口径、数据还是老口径的判重结果
            redup = None
            if cur.get("dedupe") != before:
                store.rebuild_dup_keys()
                redup = store.dedupe()
            return self._json({"ok": True, "current": cur, "redup": redup,
                               "dedupe_note": store.dedupe_note()})
        if u.path == "/api/report/export":
            from . import llm
            name = (payload.get("name") or "").strip()
            # 从报告标题里解析出它当时的区间/口径，重算统计给图表用——
            # 这样老报告也能补出图表版，不必在生成时额外落一份快照
            ov = None
            # PDF 是拿 HTML 打印出来的，所以它也需要 ov，否则导出的 PDF 里一张图都没有
            if payload.get("fmt", "html") in ("html", "pdf"):
                md = llm.read_report(name)
                m = re.search(r"（(\d{4}-\d{2}-\d{2}) ~ (\d{4}-\d{2}-\d{2})）", md or "")
                scope = (re.search(r"口径：(\w+)", md or "") or [None, "main"])[1] \
                    if "口径：" in (md or "") else "main"
                if m:
                    ov = api_overview(m.group(1), m.group(2), "", scope)
                    ov["since"], ov["until"] = m.group(1), m.group(2)
                    ov["_top"] = api_posts(m.group(1), m.group(2), "", [], 10, scope, "")
            return self._json(llm.export_report(name, payload.get("fmt", "html"), ov))
        if u.path.startswith("/api/llm/"):
            from . import llm
            action = u.path.rsplit("/", 1)[-1]
            if action == "save":
                return self._json(llm.save_model(payload))
            if action == "delete":
                return self._json(llm.delete_model(payload.get("id", "")))
            if action == "default":
                return self._json(llm.set_default(payload.get("id", "")))
            if action == "test":
                return self._json(llm.test_model(payload.get("id", "")))
            if action == "models_of":
                return self._json(llm.fetch_model_list(payload.get("id", "")))
        if u.path == "/api/llm/report" or u.path == "/api/report/create":
            tid = new_task("report", "生成分析报告")
            run_bg(task_report, tid, payload)
            return self._json({"task": tid})
        self.send_error(404)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print("=" * 68)
    print(f"声量采集中台已启动：{url}")
    print("仅监听本机、无鉴权（它能驱动你的登录态浏览器，不要暴露到局域网）")
    print(f"数据库：{config.DB_PATH}")
    print("Ctrl+C 停止")
    print("=" * 68)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
