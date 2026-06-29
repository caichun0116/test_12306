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

from flask import Flask, request, jsonify, render_template

import ticket
import notify
from monitor_service import MANAGER

app = Flask(__name__)


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


@app.route("/")
def index():
    return render_template("index.html")


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

    job = MANAGER.create(data)
    return jsonify({"ok": True, "id": job.id, "job": job.summary()})


@app.route("/api/monitor/list")
def api_monitor_list():
    return jsonify({"ok": True, "jobs": MANAGER.list()})


@app.route("/api/monitor/<jid>")
def api_monitor_detail(jid):
    job = MANAGER.get(jid)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"})
    return jsonify({"ok": True, "job": job.detail()})


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    jid = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    ok = MANAGER.stop(jid)
    return jsonify({"ok": ok, "error": "" if ok else "任务不存在"})


@app.route("/api/monitor/delete", methods=["POST"])
def api_monitor_delete():
    jid = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    ok = MANAGER.delete(jid)
    return jsonify({"ok": ok, "error": "" if ok else "任务不存在"})


if __name__ == "__main__":
    host  = os.environ.get("HOST", "127.0.0.1")
    port  = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    # threaded=True：允许多人同时查询，避免一个人的延伸查询把别人卡住
    app.run(host=host, port=port, debug=debug, threaded=True)
