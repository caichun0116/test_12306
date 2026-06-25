#!/usr/bin/env bash
# 一键把本地服务分享到公网（临时网址，靠 Cloudflare Tunnel）
# 用法：./share.sh
# 关闭：在本窗口按 Ctrl+C，公网网址即失效

set -e
cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"
PORT=5001

# 1. 没有虚拟环境就自动创建并装依赖
if [ ! -x "$PY" ]; then
  echo "🔧 首次运行，创建虚拟环境并安装依赖…"
  python3 -m venv "$VENV"
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r requirements.txt
fi

# 2. 检查 cloudflared
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "❌ 未安装 cloudflared，请先执行：brew install cloudflared"
  exit 1
fi

# 3. 关掉可能残留的旧进程
pkill -f "app.py" 2>/dev/null || true
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1

# 4. 后台启动 Flask（关 debug、开多线程，仅监听本机，由隧道转发）
echo "🚄 启动本地服务…"
FLASK_DEBUG=0 HOST=127.0.0.1 PORT=$PORT "$PY" app.py > /tmp/qiangpiao_app.log 2>&1 &
APP_PID=$!

# 进程退出时一并清理
cleanup() {
  echo ""
  echo "🛑 正在关闭…"
  kill "$APP_PID" 2>/dev/null || true
  pkill -f "cloudflared tunnel" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 等服务起来
for i in $(seq 1 15); do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then break; fi
  sleep 1
done

echo ""
echo "🌍 正在生成公网网址（下面那条 https://xxxx.trycloudflare.com 就是，发给朋友即可）"
echo "   ⚠️  此网址临时有效：本窗口关闭 / 按 Ctrl+C 后即失效"
echo "──────────────────────────────────────────────"

# 5. 前台运行隧道（它会打印公网网址）
cloudflared tunnel --url "http://127.0.0.1:$PORT"
