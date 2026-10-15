#!/bin/bash
# 网舆监控中台 —— macOS / Linux 一键启动
# 双击这个文件即可。它会在首次运行时自动装好依赖，之后每次只是起服务。
cd "$(dirname "$0")" || exit 1

echo "=============================================="
echo "  网舆 · 舆论声量监控中台"
echo "=============================================="

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "✘ 没找到 Python 3.10 或更高版本。"
  echo "  请到 https://www.python.org/downloads/ 安装后重新双击本文件。"
  read -r -p "按回车关闭…" _; exit 1
fi
echo "✔ Python：$($PY --version)"

# 虚拟环境：把依赖装在项目里，不污染系统 Python
if [ ! -d ".venv" ]; then
  echo "→ 首次运行，创建虚拟环境…"
  "$PY" -m venv .venv || { echo "✘ 创建虚拟环境失败"; read -r -p "按回车关闭…" _; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 用标记文件判断依赖装过没有，避免每次启动都跑一遍 pip
if [ ! -f ".venv/.deps_ok" ]; then
  echo "→ 安装 Python 依赖（约 1 分钟）…"
  python -m pip install -q --upgrade pip
  python -m pip install -q -r requirements.txt || {
    echo "✘ 依赖安装失败，检查网络后重试"; read -r -p "按回车关闭…" _; exit 1; }
  touch .venv/.deps_ok
fi

if [ ! -f ".venv/.browser_ok" ]; then
  echo "→ 下载浏览器内核 Chromium（约 150MB，只需一次）…"
  # 国内直连慢的话走镜像
  export PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-https://npmmirror.com/mirrors/playwright}"
  python -m playwright install chromium || {
    echo "✘ 浏览器下载失败。可手动执行：python -m playwright install chromium"
    read -r -p "按回车关闭…" _; exit 1; }
  touch .venv/.browser_ok
fi

echo ""
echo "→ 启动中台，浏览器会自动打开 http://127.0.0.1:8765"
echo "  关闭这个窗口就是停止服务。"
echo ""
python -m hub.cli serve
read -r -p "服务已停止，按回车关闭…" _
