"""全局配置与路径。"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("HUB_DATA_DIR", BASE / "data"))
PROFILE_DIR = DATA / "profiles"      # 每个平台一个 Chrome 持久化目录（登录态就在这里）
STATE_DIR = DATA / "state"           # 登录态元信息（最近登录时间、最近校验结果）
EXPORT_DIR = DATA / "exports"
REPORT_DIR = DATA / "reports"         # 大模型生成的分析报告（markdown）
LLM_CONFIG = DATA / "llm.json"        # 接入的模型清单（含 api_key，别提交到仓库）
SETTINGS_PATH = DATA / "settings.json"  # 用户自行配置的口径参数（权重/阈值/默认值）
DB_PATH = DATA / "hub.sqlite3"

for _p in (DATA, PROFILE_DIR, STATE_DIR, EXPORT_DIR, REPORT_DIR):
    _p.mkdir(parents=True, exist_ok=True)

UA_PC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")

VIEWPORT_PC = {"width": 1440, "height": 900}
VIEWPORT_MOBILE = {"width": 390, "height": 844}

# 浏览器通道：留空用 Playwright 自带 Chromium；设为 "chrome" 复用系统 Chrome
BROWSER_CHANNEL = os.environ.get("HUB_BROWSER_CHANNEL", "")

LOGIN_TIMEOUT_SEC = int(os.environ.get("HUB_LOGIN_TIMEOUT", "300"))
NAV_TIMEOUT_MS = 45000
# 平台间与翻页间的节流，别调太小
THROTTLE_SEC = float(os.environ.get("HUB_THROTTLE", "1.2"))
SCROLL_PAUSE_MS = 1500
