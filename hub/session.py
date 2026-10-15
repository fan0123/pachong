"""登录态管理：扫码登录一次 → 持久化 → 之后静默无头抓取。

每个平台一个独立的 Chrome 用户目录（launch_persistent_context），cookie/localStorage
都留在目录里；另存一份 state/<platform>.json 记录最近登录与校验时间，供 `status` 展示。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import config


class NeedLoginError(RuntimeError):
    def __init__(self, platform: str, label: str):
        super().__init__(
            f"[{label}] 登录态缺失或已失效，先执行：python -m hub.cli login {platform}")
        self.platform = platform


def profile_dir(name: str) -> Path:
    p = config.PROFILE_DIR / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(name: str) -> Path:
    return config.STATE_DIR / f"{name}.json"


def read_state(name: str) -> dict:
    path = state_path(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(name: str, **kv) -> dict:
    state = read_state(name)
    state.update(kv)
    state_path(name).write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    return state


def _launch(playwright, platform: str, headless: bool, mobile: bool = False):
    kwargs = dict(
        user_data_dir=str(profile_dir(platform)),
        headless=headless,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        user_agent=config.UA_MOBILE if mobile else config.UA_PC,
        viewport=config.VIEWPORT_MOBILE if mobile else config.VIEWPORT_PC,
        args=["--disable-blink-features=AutomationControlled"],
    )
    if config.BROWSER_CHANNEL:
        kwargs["channel"] = config.BROWSER_CHANNEL
    ctx = playwright.chromium.launch_persistent_context(**kwargs)
    ctx.set_default_timeout(config.NAV_TIMEOUT_MS)
    # 去掉最容易被检测的 webdriver 标记
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    return ctx


def interactive_login(plat, timeout_sec: int | None = None, on_log=None) -> bool:
    """打开有头浏览器让用户扫码，轮询检测成功后落状态。

    两个刻意的设计：
    1) 只在便宜的 is_logged_in() 变 True 之后才做 verify()，且最少间隔 20 秒、
       复用同一个校验标签页——否则每 3 秒开一个新页面会不停抢焦点、看起来像"一直刷新跳转"。
    2) 通过 on_log 往外吐心跳，前端任务日志才有"已等待 N 秒"的反馈，不然扫完码页面像死了。
    """
    timeout_sec = timeout_sec or config.LOGIN_TIMEOUT_SEC

    def log(line: str):
        print(line)
        if on_log:
            on_log(line)

    log(f"[{plat.label}] 打开浏览器窗口，请扫码或输入账号；登录页 {plat.LOGIN_URL}")
    log(f"最多等待 {timeout_sec} 秒。扫码后不要手动关窗口，检测通过会自动关。")
    started = time.time()
    deadline = started + timeout_sec
    with sync_playwright() as p:
        ctx = _launch(p, plat.name, headless=False, mobile=plat.MOBILE)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        probe = None                    # 复用的校验页，别每轮新开
        last_verify = 0.0
        last_beat = 0.0
        try:
            page.goto(plat.LOGIN_URL, wait_until="domcontentloaded")
            while time.time() < deadline:
                page.wait_for_timeout(3000)
                waited = int(time.time() - started)
                if time.time() - last_beat >= 15:
                    last_beat = time.time()
                    log(f"[{plat.label}] 等待登录中…已等待 {waited} 秒")
                try:
                    if not plat.is_logged_in(page):
                        continue
                    if time.time() - last_verify < 20:
                        continue
                    last_verify = time.time()
                    log(f"[{plat.label}] 页面像是登录了，正在校验搜索能力…")
                    if probe is None or probe.is_closed():
                        probe = ctx.new_page()
                    ok, reason = plat.verify(ctx, page=probe)
                    if not ok:
                        log(f"[{plat.label}] 还没通过：{reason}")
                        continue
                    write_state(plat.name, platform=plat.name, label=plat.label,
                                logged_in=True, reason=reason,
                                last_login=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                last_check=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    log(f"[{plat.label}] 登录成功（{reason}），登录态已落盘，"
                        f"之后采集全程无头静默，不会再弹窗口。")
                    return True
                except Exception as exc:
                    log(f"[{plat.label}] 检测出错，继续等：{exc}"[:160])
                    continue
            log(f"[{plat.label}] 等待超时（{timeout_sec} 秒），未检测到登录成功")
            write_state(plat.name, platform=plat.name, label=plat.label, logged_in=False,
                        reason="登录等待超时",
                        last_check=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            return False
        finally:
            ctx.close()


def check_login(plat) -> bool:
    """静默校验登录态（无头），走平台的功能性探测 verify()。"""
    with sync_playwright() as p:
        mobile = plat.MOBILE
        ctx = _launch(p, plat.name, headless=True, mobile=mobile)
        try:
            ok, reason = plat.verify(ctx)
        except Exception as exc:
            ok, reason = False, f"校验异常：{exc}"[:120]
        finally:
            ctx.close()
    write_state(plat.name, platform=plat.name, label=plat.label, logged_in=ok,
                reason=reason,
                last_check=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return ok


class SilentSession:
    """上下文管理器：静默无头会话。登录态不可用时抛 NeedLoginError，不静默返回空结果。"""

    def __init__(self, plat, require_login: bool = True, headless: bool = True):
        self.plat = plat
        self.require_login = require_login
        self.headless = headless
        self._pw = None
        self.ctx = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        mobile = self.plat.MOBILE
        # 有些平台（微博）未登录时只有移动端入口可用，按登录态自动切 UA/视口
        if getattr(self.plat, "MOBILE_WHEN_ANON", False) \
                and not read_state(self.plat.name).get("logged_in"):
            mobile = True
        self.ctx = _launch(self._pw, self.plat.name, headless=self.headless,
                           mobile=mobile)
        if self.require_login and self.plat.NEEDS_LOGIN:
            try:
                ok, reason = self.plat.verify(self.ctx)
            except Exception as exc:
                ok, reason = False, f"校验异常：{exc}"[:120]
            write_state(self.plat.name, platform=self.plat.name, label=self.plat.label,
                        logged_in=ok, reason=reason,
                        last_check=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            if not ok:
                if getattr(self.plat, "LOGIN_OPTIONAL", False):
                    print(f"  [{self.plat.label}] {reason}")
                    return self.ctx
                self.__exit__(None, None, None)
                raise NeedLoginError(self.plat.name, self.plat.label)
        return self.ctx

    def __exit__(self, *exc):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()
        return False
