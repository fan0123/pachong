"""爬虫中台 CLI。

  python -m hub.cli serve                         # 起本地中台页面，点按钮操作（推荐）
  python -m hub.cli platforms                     # 看支持哪些平台、能拿哪些指标
  python -m hub.cli login xhs weibo douyin        # 打开浏览器扫码，登录态持久化
  python -m hub.cli status                        # 各平台登录态体检
  python -m hub.cli crawl -k "开屏广告" -k "摇一摇广告" \
        -p xhs,weibo,weixin -n 60 --since 2026-06-01 --until 2026-08-31
  python -m hub.cli export --since 2026-06-01 --xlsx
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime

from . import config, session, store
from .platforms import get, ordered
from .session import NeedLoginError


def cmd_platforms(args):
    print(f"{'平台':<8}{'代号':<11}{'需登录':<8}{'可采指标'}")
    print("-" * 78)
    for p in ordered():
        print(f"{p.label:<8}{p.name:<11}{'是' if p.NEEDS_LOGIN else '否':<8}"
              f"{'/'.join(p.METRICS)}")
        if p.NOTE:
            print(f"{'':<27}↳ {p.NOTE}")


def cmd_login(args):
    names = args.platforms or [p.name for p in ordered() if p.NEEDS_LOGIN]
    for name in names:
        plat = get(name)
        if not plat.NEEDS_LOGIN and not args.force:
            print(f"[{plat.label}] 免登录即可采集，跳过（--force 可强制登录以提升配额）")
            continue
        session.interactive_login(plat, timeout_sec=args.timeout)


def cmd_status(args):
    print(f"{'平台':<8}{'登录态':<8}{'最近登录':<21}{'最近校验':<21}")
    print("-" * 62)
    for p in ordered():
        st = session.read_state(p.name)
        if args.check:
            ok = session.check_login(p) if p.NEEDS_LOGIN else True
            st = session.read_state(p.name)
        else:
            ok = st.get("logged_in")
        flag = "✔ 可用" if ok else ("— 免登录" if not p.NEEDS_LOGIN else "✘ 需登录")
        print(f"{p.label:<8}{flag:<8}{st.get('last_login', '-'):<21}"
              f"{st.get('last_check', '-'):<21}")


def cmd_crawl(args):
    names = [n.strip() for n in (",".join(args.platforms)).split(",") if n.strip()] \
        if args.platforms else [p.name for p in ordered()]
    keywords = args.keyword or []
    if not keywords:
        print("至少给一个 -k 关键词")
        return 2
    total = 0
    for name in names:
        plat = get(name)
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n=== [{plat.label}] 关键词 {len(keywords)} 个，每词上限 {args.limit}")
        try:
            with session.SilentSession(plat, require_login=not args.no_login_check,
                                      headless=not args.headful) as ctx:
                for kw in keywords:
                    try:
                        posts = plat.search(ctx, kw, args.limit, args.since, args.until)
                    except Exception as exc:
                        print(f"  [{kw}] 采集异常：{exc}")
                        if args.debug:
                            traceback.print_exc()
                        store.log_run(name, kw, 0, 0, started, note=f"error: {exc}"[:200],
                                      since=args.since, until=args.until, limit_n=args.limit)
                        continue
                    saved = store.save(posts)
                    total += saved
                    eng = sum(p.engagement for p in posts)
                    print(f"  [{kw}] 抓到 {len(posts)} 条，入库 {saved} 条，互动量合计 {eng}")
                    if args.comments and posts:
                        if not getattr(plat, "COMMENTS_SUPPORTED", False):
                            print(f"  [{kw}] {plat.label} 暂不支持评论采集，已跳过")
                        else:
                            cn = 0
                            for p in posts:
                                try:
                                    cs = plat.fetch_comments(ctx, p, args.comment_limit)
                                except Exception:
                                    continue
                                for c in cs:
                                    c.setdefault("platform", name)
                                    c.setdefault("post_id", p.post_id)
                                    c.setdefault("keyword", kw)
                                cn += store.save_comments(cs)
                            print(f"  [{kw}] 评论入库 {cn} 条")
                    # 时间窗必须记进流水：覆盖台账靠它判断"这个渠道这几天抓过没有"，
                    # 少记这一项，增量采集就只能靠"库里有没有数据"反推，会把限流截断当成没抓
                    store.log_run(name, kw, len(posts), saved, started,
                                  note=f"{args.since or '-'}~{args.until or '-'} "
                                       f"上限{args.limit} 命中{len(posts)}",
                                  since=args.since, until=args.until, limit_n=args.limit)
                    time.sleep(config.THROTTLE_SEC)
        except NeedLoginError as exc:
            print(f"  {exc}")
            for kw in keywords:
                store.log_run(name, kw, 0, 0, started, note="need_login",
                              since=args.since, until=args.until, limit_n=args.limit)
            continue
        except Exception as exc:
            print(f"  [{plat.label}] 会话失败：{exc}")
            if args.debug:
                traceback.print_exc()
    print(f"\n合计入库 {total} 条 → {config.DB_PATH}")
    return 0


def cmd_export(args):
    names = [n.strip() for n in (",".join(args.platforms)).split(",") if n.strip()] \
        if args.platforms else None
    rows = store.query(names, args.keyword or "", args.since, args.until)
    print(f"命中 {len(rows)} 条")
    if not rows:
        return 0
    if args.xlsx or not args.jsonl:
        print("Excel ->", store.export_xlsx(rows))
    if args.jsonl:
        print("JSONL ->", store.export_jsonl(rows))
    return 0


def cmd_serve(args):
    from .server import serve
    serve(host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="hub", description="多平台声量爬虫中台")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("platforms", help="列出平台与可采指标").set_defaults(func=cmd_platforms)

    p = sub.add_parser("login", help="扫码/账号登录并持久化登录态")
    p.add_argument("platforms", nargs="*", help="平台代号，留空=所有需登录平台")
    p.add_argument("--timeout", type=int, default=config.LOGIN_TIMEOUT_SEC)
    p.add_argument("--force", action="store_true", help="免登录平台也走一次登录")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("status", help="登录态体检")
    p.add_argument("--check", action="store_true", help="真的开无头浏览器校验一次")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("crawl", help="静默抓取")
    p.add_argument("-k", "--keyword", action="append", help="关键词，可重复")
    p.add_argument("-p", "--platforms", action="append", default=[],
                   help="平台代号，逗号分隔；留空=全部")
    p.add_argument("-n", "--limit", type=int, default=50, help="每平台每关键词上限")
    p.add_argument("--since", default="", help="起始日期 YYYY-MM-DD")
    p.add_argument("--until", default="", help="截止日期 YYYY-MM-DD")
    p.add_argument("--headful", action="store_true", help="显示浏览器（处理验证码时用）")
    p.add_argument("--no-login-check", action="store_true", help="跳过登录态预检")
    p.add_argument("--comments", action="store_true", help="同时采集每条帖子下的评论")
    p.add_argument("--comment-limit", type=int, default=30, help="每条帖子最多采多少评论")
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("export", help="导出 Excel / JSONL")
    p.add_argument("-p", "--platforms", action="append", default=[])
    p.add_argument("-k", "--keyword", default="")
    p.add_argument("--since", default="")
    p.add_argument("--until", default="")
    p.add_argument("--xlsx", action="store_true")
    p.add_argument("--jsonl", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("serve", help="起本地操作中台（浏览器点按钮完成登录/采集/导出）")
    p.add_argument("--host", default="127.0.0.1", help="只建议 127.0.0.1，无鉴权")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=cmd_serve)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
