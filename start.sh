#!/usr/bin/env bash
# 一键启动 12306 余票查询服务
# 用法：./start.sh        （首次会自动建虚拟环境并装依赖）

set -e
cd "$(dirname "$0")"

# ══════════════════════════════════════════════════════════
#  站主接收通知（可选）：抢到票 / 监控发现有票时，也给你(站主)发一份。
#  在下面两行填上你自己的推送渠道即可（留空则不给站主发）。
#  渠道 OWNER_NOTIFY_CHANNEL 可选：pushplus / serverchan / wecom
#  凭证只在本机、不下发访客。
export OWNER_NOTIFY_CHANNEL="wecom"     # 例：wecom
export OWNER_NOTIFY_TOKEN="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=6a3e0783-17f6-4221-ad13-687543865231"       # 例：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
# ══════════════════════════════════════════════════════════

VENV=".venv"
PY="$VENV/bin/python"
PORT=5001

# 1. 没有虚拟环境就自动创建并安装依赖
if [ ! -x "$PY" ]; then
  echo "🔧 未检测到虚拟环境，正在创建并安装依赖（首次运行需要联网，稍等）…"
  python3 -m venv "$VENV"
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r requirements.txt
  echo "✅ 依赖安装完成"
elif ! "$PY" -c "import flask, requests, cryptography" >/dev/null 2>&1; then
  echo "🔧 检测到依赖缺失，正在补装 requirements.txt…"
  "$PY" -m pip install -q -r requirements.txt
  echo "✅ 依赖检查完成"
fi

# 2. 读取访问令牌（没有就自动生成一个，存到 .app_token，仅本机可读）
TOKEN_FILE=".app_token"
if [ ! -s "$TOKEN_FILE" ]; then
  python3 -c "import secrets; print(secrets.token_urlsafe(24))" > "$TOKEN_FILE"
  echo "🔑 已生成新的访问令牌：$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
export APP_TOKEN="$(cat "$TOKEN_FILE")"

# 3. 关掉可能已在运行的旧服务，避免端口被占用
pkill -f "app.py" 2>/dev/null || true
sleep 1

# 4. 启动
echo "🚄 正在启动服务…  打开浏览器访问 http://127.0.0.1:$PORT"
echo "   🔑 首次打开页面，点右上角「令牌」填入下面这串（之后浏览器会记住）："
echo "      $APP_TOKEN"
echo "   按 Ctrl+C 停止"
exec "$PY" app.py
