#!/usr/bin/env python3
"""
12306 余票监控工具 (合规版)
功能：定期查询余票 → 有票时发送 macOS 本地通知
"""

import time
import json
import subprocess
import sys
import requests
from datetime import datetime

# ──────────────────────────────────────────
# 用户配置区
# ──────────────────────────────────────────
CONFIG = {
    "from_station": "BJP",          # 出发站代码（见下方 STATIONS 字典）
    "to_station":   "SHH",          # 到达站代码
    "date":         "2026-07-01",   # 出行日期 YYYY-MM-DD
    "train_filter": [],              # 只关注这些车次，如 ["G1", "G3"]；空列表=全部
    "seat_types":   ["二等座", "一等座", "商务座"],  # 关注的座位类型
    "interval":     60,              # 查询间隔（秒），建议 ≥ 30，避免被限速
}

# 常用车站代码（完整列表：https://kyfw.12306.cn/otn/resources/js/framework/station_name.js）
STATIONS = {
    "北京":   "BJP",  "北京南": "VNP",  "北京西": "BXP",  "北京东": "BOP",
    "上海":   "SHH",  "上海虹桥": "AOH",
    "广州":   "GZQ",  "广州南": "IZQ",
    "深圳":   "SZQ",  "深圳北": "IOQ",
    "成都":   "CDW",  "成都东": "ICW",
    "武汉":   "WHN",
    "西安":   "XAY",  "西安北": "EAY",
    "杭州":   "HZH",  "杭州东": "HGH",
    "南京":   "NJH",  "南京南": "NKH",
    "天津":   "TJP",  "天津南": "UXP",
    "重庆":   "CQW",  "重庆北": "CUW",
    "长沙":   "CSQ",  "长沙南": "CNQ",
    "郑州":   "ZZF",  "郑州东": "ZAF",
    "济南":   "JNK",  "济南西": "JIK",
    "青岛":   "QDK",  "青岛北": "QEK",
    "哈尔滨": "HBB",  "哈尔滨西": "ZWB",
    "沈阳":   "SYT",  "沈阳北": "SNT",
    "大连":   "DLT",  "大连北": "DDT",
    "长春":   "CCT",
    "昆明":   "KMM",  "昆明南": "KOM",
    "贵阳":   "GYQ",  "贵阳北": "GIQ",
    "南宁":   "NNZ",
    "厦门":   "XMS",  "厦门北": "XBM",
    "福州":   "FZS",  "福州南": "FYS",
    "合肥":   "HFH",  "合肥南": "EQH",
    "南昌":   "NCG",  "南昌西": "NXG",
    "太原":   "TYV",  "太原南": "TTV",
    "石家庄": "SJP",
    "徐州":   "XUH",  "徐州东": "XCH",
    "苏州":   "SZH",  "苏州北": "SQH",
    "无锡":   "WXH",
    "宁波":   "NGH",
    "温州":   "WZS",  "温州南": "YNS",
    "兰州":   "LZJ",  "兰州西": "ZDJ",
    "乌鲁木齐": "WLJ",
    "银川":   "YCJ",
    "呼和浩特": "HHB", "呼和浩特东": "HEB",
}

# 12306 返回数据中座位类型索引（列索引固定）
SEAT_INDEX = {
    "商务座":   32,
    "一等座":   31,
    "二等座":   30,
    "高级软卧": 21,
    "软卧":     23,
    "动卧":     33,
    "硬卧":     28,
    "软座":     24,
    "硬座":     29,
    "无座":     26,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ──────────────────────────────────────────
# 查询余票
# ──────────────────────────────────────────

def query_tickets(from_code: str, to_code: str, date: str) -> list[dict]:
    """
    调用 12306 官网查票接口，返回列车列表。
    每条记录包含：train_no, train_name, from_time, to_time, duration, seats（各类型余票）
    """
    url = "https://kyfw.12306.cn/otn/leftTicket/query"
    params = {
        "leftTicketDTO.train_date": date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": "ADULT",
    }
    try:
        resp = SESSION.get(url, params=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[{now()}] 请求失败: {e}")
        return []

    try:
        data = resp.json()
    except json.JSONDecodeError:
        print(f"[{now()}] 解析响应失败")
        return []

    if data.get("status") is not True:
        msg = data.get("messages") or data.get("c_url") or "未知错误"
        print(f"[{now()}] 接口返回错误: {msg}")
        return []

    result_data = data.get("data", {}).get("result", [])
    trains = []
    for raw in result_data:
        cols = raw.split("|")
        if len(cols) < 34:
            continue
        seats = {}
        for seat_name, idx in SEAT_INDEX.items():
            val = cols[idx].strip()
            seats[seat_name] = val if val else "--"
        trains.append({
            "train_no":   cols[2],
            "train_name": cols[3],
            "from_time":  cols[8],
            "to_time":    cols[9],
            "duration":   cols[10],
            "seats":      seats,
        })
    return trains


# ──────────────────────────────────────────
# 判断是否有票
# ──────────────────────────────────────────

def has_ticket(seats: dict, watch_types: list[str]) -> bool:
    for t in watch_types:
        val = seats.get(t, "--")
        if val not in ("--", "无", "0", ""):
            return True
    return False


def available_seats(seats: dict, watch_types: list[str]) -> str:
    parts = []
    for t in watch_types:
        val = seats.get(t, "--")
        if val not in ("--", "无", "0", ""):
            parts.append(f"{t}:{val}")
    return "  ".join(parts)


# ──────────────────────────────────────────
# macOS 通知
# ──────────────────────────────────────────

def notify(title: str, message: str):
    script = (
        f'display notification "{message}" '
        f'with title "{title}" '
        f'sound name "Ping"'
    )
    subprocess.run(["osascript", "-e", script], check=False)


# ──────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────

def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def print_table(trains: list[dict], watch_types: list[str]):
    print(f"\n{'车次':<8}{'出发':<7}{'到达':<7}{'历时':<8}", end="")
    for t in watch_types:
        print(f"{t:<8}", end="")
    print()
    print("-" * (8 + 7 + 7 + 8 + 8 * len(watch_types)))
    for t in trains:
        print(f"{t['train_name']:<8}{t['from_time']:<7}{t['to_time']:<7}{t['duration']:<8}", end="")
        for seat in watch_types:
            val = t["seats"].get(seat, "--")
            print(f"{val:<8}", end="")
        print()


# ──────────────────────────────────────────
# 主循环
# ──────────────────────────────────────────

def main():
    cfg = CONFIG
    from_name = next((k for k, v in STATIONS.items() if v == cfg["from_station"]), cfg["from_station"])
    to_name   = next((k for k, v in STATIONS.items() if v == cfg["to_station"]),   cfg["to_station"])

    print("=" * 60)
    print(f"  12306 余票监控")
    print(f"  路线：{from_name} → {to_name}")
    print(f"  日期：{cfg['date']}")
    print(f"  关注座位：{', '.join(cfg['seat_types'])}")
    print(f"  查询间隔：{cfg['interval']} 秒")
    if cfg["train_filter"]:
        print(f"  关注车次：{', '.join(cfg['train_filter'])}")
    print("=" * 60)
    print("按 Ctrl+C 退出\n")

    notified = set()   # 已通知的 (train_name, seat_type)，避免重复打扰

    while True:
        trains = query_tickets(cfg["from_station"], cfg["to_station"], cfg["date"])

        if not trains:
            print(f"[{now()}] 暂无数据，等待下次查询...")
        else:
            # 过滤车次
            if cfg["train_filter"]:
                trains = [t for t in trains if t["train_name"] in cfg["train_filter"]]

            print(f"\n[{now()}] 查询到 {len(trains)} 趟列车：")
            print_table(trains, cfg["seat_types"])

            # 检查有票的并发通知
            for t in trains:
                if has_ticket(t["seats"], cfg["seat_types"]):
                    key = t["train_name"]
                    seat_info = available_seats(t["seats"], cfg["seat_types"])
                    if key not in notified:
                        msg = f"{t['train_name']} {t['from_time']}→{t['to_time']}  {seat_info}"
                        notify(f"有票啦！{from_name}→{to_name} {cfg['date']}", msg)
                        print(f"\n  ★ 通知已发送：{msg}")
                        notified.add(key)
                else:
                    # 票没了则重置，下次有票可再次通知
                    notified.discard(t["train_name"])

        try:
            time.sleep(cfg["interval"])
        except KeyboardInterrupt:
            print("\n已退出。")
            sys.exit(0)


if __name__ == "__main__":
    main()
