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

app = Flask(__name__)


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
    )
    return jsonify(result)


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


if __name__ == "__main__":
    host  = os.environ.get("HOST", "127.0.0.1")
    port  = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    # threaded=True：允许多人同时查询，避免一个人的延伸查询把别人卡住
    app.run(host=host, port=port, debug=debug, threaded=True)