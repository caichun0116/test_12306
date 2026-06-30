#!/usr/bin/env python3
"""
12306 余票查询 Web 服务（买长乘短）

本地启动：python app.py            （仅本机可访问）
对外分享：用 cloudflared 做内网穿透，见 share.sh
环境变量：
  HOST=0.0.0.0   监听地址（默认 127.0.0.1，仅本机）
  PORT=5000      端口
  FLASK_DEBUG=1  开启调试（对外分享时务必不要开）
"""

import os
import hmac
import time
import hashlib
import json
import secrets
import threading
import subprocess
import urllib.error
import urllib.request

from flask import Flask, request, jsonify, render_template, session

import ticket
import notify
from monitor_service import MANAGER
import order12306
from order_service import MANAGER as ORDER_MANAGER

app = Flask(__name__)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PASSENGER_KEY_SALT = os.environ.get("PASSENGER_KEY_SALT") or os.urandom(16).hex()
# 共享 Token 鉴权：所有非首页/静态接口都需带 X-App-Token（入场票，谁能进服务器）。
_APP_TOKEN_FILE = os.path.join(_BASE_DIR, ".app_token")
# 管理员令牌：带正确 X-Admin-Token 的请求可查看/停止/删除所有人的任务。
_ADMIN_TOKEN_FILE = os.path.join(_BASE_DIR, ".admin_token")
_LOCAL_ADDRS = {"127.0.0.1", "::1"}
# 会话空闲多久（秒）无任何浏览器请求就驱逐：连带停掉其名下抢票/监控任务
_IDLE_TTL = max(60, int(os.environ.get("LOGIN_IDLE_TTL", "1800") or "1800"))
_CHROME_DEBUG_URL = "http://127.0.0.1:9222"
_CHROME_PROFILE_DIR = "/tmp/qp-chrome-12306"
_OFFICIAL_LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"


def _write_secret_file(path: str, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (value + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_token(env_name: str, path: str) -> tuple[str, str]:
    """通用令牌加载：env 优先，否则读/生成令牌文件（0600）。返回 (token, source)。"""
    env_token = (os.environ.get(env_name) or "").strip()
    if env_token:
        return env_token, "env"
    try:
        if os.path.exists(path):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            with open(path, encoding="utf-8") as f:
                token = f.read().strip()
            if token:
                return token, "file"
        token = secrets.token_urlsafe(24)
        _write_secret_file(path, token)
        return token, "generated"
    except OSError:
        return "", "none"


_APP_TOKEN, _APP_TOKEN_SOURCE = _load_token("APP_TOKEN", _APP_TOKEN_FILE)
_ADMIN_TOKEN, _ADMIN_TOKEN_SOURCE = _load_token("ADMIN_TOKEN", _ADMIN_TOKEN_FILE)


def is_admin() -> bool:
    """请求是否带正确的管理员令牌。"""
    if not _ADMIN_TOKEN:
        return False
    return hmac.compare_digest(request.headers.get("X-Admin-Token", ""), _ADMIN_TOKEN)


def _load_flask_secret() -> str:
    """Flask 会话签名密钥：env FLASK_SECRET 优先，否则读/生成 .flask_secret（0600）。"""
    env = (os.environ.get("FLASK_SECRET") or "").strip()
    if env:
        return env
    path = os.path.join(_BASE_DIR, ".flask_secret")
    try:
        if os.path.exists(path):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            with open(path, encoding="utf-8") as f:
                val = f.read().strip()
            if val:
                return val
        val = secrets.token_urlsafe(32)
        _write_secret_file(path, val)
        return val
    except OSError:
        # 兜底：进程内随机密钥（重启后旧会话 cookie 失效，可接受）
        return secrets.token_urlsafe(32)


app.secret_key = _load_flask_secret()


# ──────────────────────────────────────────
# 多用户会话：每个浏览器一个 sid，对应一份独立的 12306 登录态
# ──────────────────────────────────────────

def _ensure_sid() -> str:
    sid = session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(16)
        session["sid"] = sid
        session.permanent = True
    _touch_session(sid)      # 每次真实请求都刷新「人还在」的活跃时间
    return sid


def current_sid() -> str:
    return session.get("sid") or _ensure_sid()


def current_login():
    """返回当前浏览器会话对应的 12306 登录态（懒创建）。"""
    return order12306.REGISTRY.get_or_create(current_sid())


# ── 会话活跃度 + 空闲驱逐 ──
# 「人是否还在」以浏览器最近一次请求为准（任务自己在后台跑不算）。会话空闲超过
# _IDLE_TTL 就驱逐：停掉并移除其名下抢票/监控任务，丢弃其登录态，避免开了任务
# 就走人后无人看管地一直跑。
_SESSION_SEEN: dict[str, float] = {}
_SESSION_LOCK = threading.Lock()


def _touch_session(sid: str):
    if not sid:
        return
    with _SESSION_LOCK:
        _SESSION_SEEN[sid] = time.monotonic()


def _evict_session(sid: str):
    """驱逐一个会话：停+移除其任务，丢弃登录态。"""
    try:
        ORDER_MANAGER.purge_owner(sid)
        MANAGER.purge_owner(sid)
        order12306.REGISTRY.drop(sid)
    finally:
        with _SESSION_LOCK:
            _SESSION_SEEN.pop(sid, None)


def _session_sweep_loop():
    while True:
        time.sleep(60)
        now = time.monotonic()
        with _SESSION_LOCK:
            stale = [sid for sid, t in _SESSION_SEEN.items()
                     if now - t > _IDLE_TTL]
        for sid in stale:
            _evict_session(sid)


threading.Thread(target=_session_sweep_loop, daemon=True,
                 name="session-sweeper").start()


def _resolve_station(value: str):
    """兼容站名 / 站码，返回 (站码, 站名)。"""
    value = (value or "").strip()
    stations = ticket.load_stations()
    if value in stations["code2name"]:
        return value, stations["code2name"][value]
    code = stations["name2code"].get(value)
    if code:
        return code, value
    return None, value


def _build_order_url(from_name: str, to_name: str, date: str):
    """生成 12306 官方下单页链接。"""
    if not from_name or not to_name or not date:
        return None, "请填写出发地、目的地和日期"

    from_code, from_label = _resolve_station(from_name)
    to_code, to_label = _resolve_station(to_name)
    if not from_code:
        return None, f"未找到出发站「{from_name}」"
    if not to_code:
        return None, f"未找到到达站「{to_name}」"

    # 与前端 bookUrl()、monitor 共用 ticket.book_url()
    url = ticket.book_url(from_label, from_code, to_label, to_code, date)
    return url, ""


def _passenger_key(p: dict) -> str:
    raw = "|".join([
        p.get("name", ""),
        p.get("id_type_code", ""),
        p.get("id_no", ""),
    ])
    return hmac.new(_PASSENGER_KEY_SALT.encode("utf-8"),
                    raw.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _read_json_url(url: str, timeout: float = 3):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _chrome_version():
    try:
        return _read_json_url(f"{_CHROME_DEBUG_URL}/json/version")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _open_official_chrome():
    subprocess.Popen([
        "open", "-na", "Google Chrome", "--args",
        "--remote-debugging-port=9222",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={_CHROME_PROFILE_DIR}",
        "--no-first-run",
        "--new-window",
        _OFFICIAL_LOGIN_URL,
    ])


def _import_chrome_12306_cookies(login) -> tuple[bool, str, int]:
    version = _chrome_version()
    if not version:
        return False, "未检测到官方登录 Chrome，请先点击「打开官方登录页」", 0

    script = r"""
const wsUrl = process.argv[1];
const ws = new WebSocket(wsUrl);
const id = 1;
ws.onopen = () => ws.send(JSON.stringify({id, method: "Storage.getCookies", params: {}}));
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id !== id) return;
  const cookies = (msg.result.cookies || []).filter(c => String(c.domain || "").includes("12306.cn"));
  console.log(JSON.stringify(cookies));
  ws.close();
};
ws.onerror = (err) => {
  console.error(String(err && err.message || err || "WebSocket error"));
  process.exit(2);
};
"""
    try:
        proc = subprocess.run(
            ["node", "-e", script, version.get("webSocketDebuggerUrl", "")],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"读取 Chrome Cookie 失败：{e}", 0
    if proc.returncode != 0:
        return False, (proc.stderr or "读取 Chrome Cookie 失败").strip(), 0
    try:
        cookies = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return False, "Chrome Cookie 返回格式异常", 0
    if not cookies:
        return False, "未读取到 12306 Cookie，请确认官方页面已登录", 0

    order12306._load_cookies(login.s.cookies, cookies)
    login.logged_in = login.check_online(force=True)
    if login.logged_in:
        return True, "已导入官方网页登录态", len(cookies)
    return False, "已读取 Cookie，但 12306 校验未登录，请在官方页面重新扫码确认", len(cookies)


@app.before_request
def _require_token():
    """统一鉴权钩子。

    - 始终放行首页与静态资源（页面要先加载才能录入 token）及预检。
    - 默认读取/生成 .app_token；所有其它请求必须带正确的 X-App-Token 头。
    - 仅当令牌文件也无法读写时，才退回仅允许本机访问。
    """
    if request.method == "OPTIONS":
        return None
    if request.endpoint == "static":
        return None
    if request.endpoint in (None, "index"):
        _ensure_sid()           # 页面加载即种下会话 cookie
        return None
    if not _APP_TOKEN:
        if request.remote_addr in _LOCAL_ADDRS:
            _ensure_sid()
            return None
        return jsonify({"ok": False, "error": "未授权（令牌不可用，仅本机可访问）"}), 403
    sent = request.headers.get("X-App-Token", "")
    if hmac.compare_digest(sent, _APP_TOKEN):
        _ensure_sid()
        return None
    return jsonify({"ok": False, "error": "未授权：请在页面右上角填写访问令牌"}), 401


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/session/ping", methods=["POST"])
def api_session_ping():
    """前端心跳：只要页面开着就定时调一次，刷新会话活跃时间（防被空闲驱逐）。
    before_request 已刷新活跃时间，这里仅回执 + 告知是否管理员。"""
    return jsonify({"ok": True, "admin": is_admin()})


@app.route("/api/stations")
def api_stations():
    """返回全部站名列表，供前端做输入联想。"""
    names = list(ticket.load_stations()["name2code"].keys())
    return jsonify({"ok": True, "stations": names})


@app.route("/api/query", methods=["POST"])
def api_query():
    data = request.get_json(silent=True) or {}
    from_name = (data.get("from") or "").strip()
    to_name   = (data.get("to") or "").strip()
    date      = (data.get("date") or "").strip()
    train_types = data.get("train_types") or []
    seat_types  = data.get("seat_types") or list(ticket.SEAT_INDEX.keys())  # 不选 = 全部
    train_names = data.get("train_names") or []
    extend      = int(data.get("extend", 1))
    with_price  = bool(data.get("with_price"))
    price_max   = data.get("price_max")
    try:
        price_max = float(price_max) if price_max not in (None, "", "null") else None
    except (TypeError, ValueError):
        price_max = None

    if not from_name or not to_name or not date:
        return jsonify({"ok": False, "error": "请填写出发地、目的地和日期"})

    result = ticket.search(
        from_name=from_name,
        to_name=to_name,
        date=date,
        train_types=train_types,
        seat_types=seat_types,
        train_names=train_names,
        extend=max(0, min(extend, 5)),
        # 站数越多，需要查询的延伸区段组合越多，相应放宽查询预算
        max_extend_queries=60 + max(0, min(extend, 5)) * 30,
        with_price=with_price,
        price_max=price_max,
    )
    return jsonify(result)


@app.route("/api/price", methods=["POST"])
def api_price():
    """按需查询单趟车某区段票价（前端懒加载补拉用，结果服务端缓存）。"""
    data = request.get_json(silent=True) or {}
    train_no  = (data.get("train_no") or "").strip()
    no_from   = (data.get("no_from") or "").strip()
    no_to     = (data.get("no_to") or "").strip()
    seat_code = (data.get("seat_code") or "").strip()
    date      = (data.get("date") or "").strip()
    pairs = ticket.query_price(train_no, no_from, no_to, seat_code, date)
    if not pairs:
        return jsonify({"ok": False, "error": "未取到票价（12306 限流，稍后再试）"})
    return jsonify({"ok": True, "prices": {name: price for name, price in pairs}})


@app.route("/api/order-url", methods=["POST"])
def api_order_url():
    """根据当前查询条件生成 12306 官方下单页链接。"""
    data = request.get_json(silent=True) or {}
    from_name = (data.get("from") or "").strip()
    to_name   = (data.get("to") or "").strip()
    date      = (data.get("date") or "").strip()
    url, msg = _build_order_url(from_name, to_name, date)
    if not url:
        return jsonify({"ok": False, "error": msg or "生成下单链接失败"})
    return jsonify({"ok": True, "url": url})


@app.route("/api/notify", methods=["POST"])
def api_notify():
    """转发一条消息到用户配置的推送渠道（微信等）。"""
    data = request.get_json(silent=True) or {}
    channel = (data.get("channel") or "").strip()
    token   = (data.get("token") or "").strip()
    title   = (data.get("title") or "余票提醒").strip()
    body    = (data.get("body") or "").strip()
    url     = (data.get("url") or "").strip()
    items   = data.get("items") or None   # 结构化车次，用于渲染「结果卡片」样式
    if not channel or not token:
        return jsonify({"ok": False, "error": "请先配置推送渠道和 token"})
    ok, msg = notify.push_message(channel, token, title, body, url, items=items)
    return jsonify({"ok": ok, "error": "" if ok else (msg or "推送失败")})


# ──────────────────────────────────────────
# 服务端常驻监控（关掉网页也继续跑）
# ──────────────────────────────────────────

@app.route("/api/monitor/create", methods=["POST"])
def api_monitor_create():
    data = request.get_json(silent=True) or {}
    from_name = (data.get("from") or "").strip()
    to_name   = (data.get("to") or "").strip()
    dates     = [d for d in (data.get("dates") or []) if d]
    if not from_name or not to_name or not dates:
        return jsonify({"ok": False, "error": "请填写出发地、目的地和至少一个日期"})
    if not _resolve_station(from_name)[0]:
        return jsonify({"ok": False, "error": f"未找到出发站「{from_name}」"})
    if not _resolve_station(to_name)[0]:
        return jsonify({"ok": False, "error": f"未找到到达站「{to_name}」"})
    channel = (data.get("channel") or "").strip()
    token   = (data.get("token") or "").strip()
    if not channel or not token:
        return jsonify({"ok": False, "error": "服务端监控需先配置微信推送（渠道 + token）"})

    job = MANAGER.create(data, owner=current_sid())
    return jsonify({"ok": True, "id": job.id, "job": job.summary()})


@app.route("/api/monitor/list")
def api_monitor_list():
    return jsonify({"ok": True, "admin": is_admin(),
                    "jobs": MANAGER.list(owner=current_sid(), admin=is_admin())})


@app.route("/api/monitor/<jid>")
def api_monitor_detail(jid):
    job = MANAGER.get(jid, owner=current_sid(), admin=is_admin())
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"})
    return jsonify({"ok": True, "job": job.detail()})


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    jid = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    ok = MANAGER.stop(jid, owner=current_sid(), admin=is_admin())
    return jsonify({"ok": ok, "error": "" if ok else "任务不存在"})


@app.route("/api/monitor/delete", methods=["POST"])
def api_monitor_delete():
    jid = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    ok = MANAGER.delete(jid, owner=current_sid(), admin=is_admin())
    return jsonify({"ok": ok, "error": "" if ok else "任务不存在"})


# ──────────────────────────────────────────
# 自动抢票下单（扫码登录 + 全自动占座）
# ──────────────────────────────────────────

@app.route("/api/order/login/qr", methods=["POST"])
def api_order_login_qr():
    """生成 12306 登录二维码（base64 图片）。"""
    ok, img, msg = current_login().create_qr()
    if not ok:
        return jsonify({"ok": False, "error": msg or "获取二维码失败"})
    return jsonify({"ok": True, "image": img})


@app.route("/api/order/login/official/open", methods=["POST"])
def api_order_login_official_open():
    """打开官方 12306 Chrome 登录页（推荐登录方式）。"""
    try:
        _open_official_chrome()
    except OSError as e:
        return jsonify({"ok": False, "error": f"打开 Chrome 失败：{e}"})
    return jsonify({"ok": True, "url": _OFFICIAL_LOGIN_URL})


@app.route("/api/order/login/official/import", methods=["POST"])
def api_order_login_official_import():
    """从官方 Chrome 调试端口导入 12306 Cookie。"""
    login = current_login()
    ok, msg, count = _import_chrome_12306_cookies(login)
    return jsonify({
        "ok": ok,
        "logged_in": bool(login.logged_in),
        "cookie_count": count,
        "msg": msg,
        "error": "" if ok else msg,
        "username": login.username,
    })


@app.route("/api/order/login/status", methods=["POST"])
def api_order_login_status():
    """轮询扫码状态：waiting / scanned / success / expired / error。"""
    login = current_login()
    state, msg = login.check_qr()
    return jsonify({"ok": True, "state": state, "msg": msg,
                    "username": login.username})


@app.route("/api/order/login/check")
def api_order_login_check():
    """返回当前登录态（用于页面加载时判断是否已登录）。"""
    login = current_login()
    online = login.check_online()
    return jsonify({"ok": True, "logged_in": online,
                    "username": login.username})


@app.route("/api/order/logout", methods=["POST"])
def api_order_logout():
    sid = current_sid()
    current_login().clear()
    # 退出登录即停掉本会话名下还在跑的抢票任务（登录态已清，再跑也会失败）
    stopped = ORDER_MANAGER.stop_owner(sid)
    return jsonify({"ok": True, "stopped": stopped})


@app.route("/api/order/passengers")
def api_order_passengers():
    """拉取账号下乘车人列表（需已登录）。"""
    ok, ps, msg = current_login().passengers()
    if not ok:
        return jsonify({"ok": False, "error": msg or "读取乘车人失败"})
    # 不把完整身份证号下发前端，只给脱敏号 + 本进程内有效的选择 token。
    safe = [{"name": p["name"], "id": _passenger_key(p),
             "id_no_mask": p["id_no_mask"],
             "id_type_name": p["id_type_name"],
             "passenger_type": p["passenger_type"]} for p in ps]
    return jsonify({"ok": True, "passengers": safe})


@app.route("/api/order/create", methods=["POST"])
def api_order_create():
    data = request.get_json(silent=True) or {}
    login = current_login()
    if not login.logged_in and not login.check_online():
        return jsonify({"ok": False, "error": "请先扫码登录 12306"})

    from_name = (data.get("from") or "").strip()
    to_name   = (data.get("to") or "").strip()
    dates     = [d for d in (data.get("dates") or []) if d]
    if not from_name or not to_name or not dates:
        return jsonify({"ok": False, "error": "请填写出发地、目的地和至少一个日期"})
    from_code, from_label = _resolve_station(from_name)
    to_code, to_label = _resolve_station(to_name)
    if not from_code:
        return jsonify({"ok": False, "error": f"未找到出发站「{from_name}」"})
    if not to_code:
        return jsonify({"ok": False, "error": f"未找到到达站「{to_name}」"})

    if not (data.get("seat_types") or []):
        return jsonify({"ok": False, "error": "请至少勾选一个要抢的坐席"})

    # 用账号真实乘客补全 allEncStr 等下单字段，按前端选择 token 匹配。
    picked_keys = set(data.get("passenger_ids") or [])
    if not picked_keys:
        return jsonify({"ok": False, "error": "请至少选择一位乘车人"})
    ok, live, msg = login.passengers()
    if not ok:
        return jsonify({"ok": False, "error": msg or "读取乘车人失败"})
    passengers = [p for p in live if _passenger_key(p) in picked_keys]
    if not passengers:
        return jsonify({"ok": False, "error": "所选乘车人无效，请重新选择"})

    cfg = dict(data)
    cfg["from"] = from_label
    cfg["to"] = to_label
    cfg["passengers"] = passengers
    job = ORDER_MANAGER.create(cfg, login=login, owner=current_sid())
    return jsonify({"ok": True, "id": job.id, "job": job.summary()})


@app.route("/api/order/list")
def api_order_list():
    return jsonify({"ok": True, "admin": is_admin(),
                    "jobs": ORDER_MANAGER.list(owner=current_sid(), admin=is_admin())})


@app.route("/api/order/<jid>")
def api_order_detail(jid):
    job = ORDER_MANAGER.get(jid, owner=current_sid(), admin=is_admin())
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"})
    return jsonify({"ok": True, "job": job.detail()})


@app.route("/api/order/stop", methods=["POST"])
def api_order_stop():
    jid = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    ok = ORDER_MANAGER.stop(jid, owner=current_sid(), admin=is_admin())
    return jsonify({"ok": ok, "error": "" if ok else "任务不存在"})


@app.route("/api/order/start", methods=["POST"])
def api_order_start():
    jid = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    ok = ORDER_MANAGER.start(jid, owner=current_sid(), admin=is_admin())
    return jsonify({"ok": ok, "error": "" if ok else "任务不存在或已完成"})


@app.route("/api/order/delete", methods=["POST"])
def api_order_delete():
    jid = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    ok = ORDER_MANAGER.delete(jid, owner=current_sid(), admin=is_admin())
    return jsonify({"ok": ok, "error": "" if ok else "任务不存在"})


if __name__ == "__main__":
    host  = os.environ.get("HOST", "127.0.0.1")
    port  = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    if _APP_TOKEN and _APP_TOKEN_SOURCE != "env":
        print("🔑 访问令牌（分享给访客，页面右上角「令牌」填入）：", flush=True)
        print(f"   {_APP_TOKEN}", flush=True)
    if _ADMIN_TOKEN and _ADMIN_TOKEN_SOURCE != "env":
        print("🛠 管理员令牌（只给你自己，可查看/停止所有人的任务）：", flush=True)
        print(f"   {_ADMIN_TOKEN}", flush=True)
    # threaded=True：允许多人同时查询，避免一个人的延伸查询把别人卡住
    app.run(host=host, port=port, debug=debug, threaded=True)
