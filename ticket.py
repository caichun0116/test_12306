
#!/usr/bin/env python3
"""
12306 余票查询核心逻辑（买长乘短）

核心能力：
1. query_tickets   —— 查询某区段 from→to 的余票
2. query_stops     —— 查询某趟车的完整经停站序列
3. search          —— 直达查询 + 「买长乘短」延伸：
                      若 B→C 在关注座位上没票，自动尝试同一车次的
                      A→C（提前上车）、B→D（延后下车）、A→D（两头延伸）
"""

import re
import time
import json
import requests
from functools import lru_cache

# ──────────────────────────────────────────
# 座位类型在 leftTicket 返回数据中的列索引（固定）
# ──────────────────────────────────────────
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

# 车次类型 → 车次名首字母前缀
TRAIN_TYPE_PREFIX = {
    "GC": ("G", "C"),   # 高铁 / 城际
    "D":  ("D",),       # 动车
    "Z":  ("Z",),       # 直达
    "T":  ("T",),       # 特快
    "K":  ("K",),       # 快速
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

EMPTY = ("--", "无", "0", "", "*")


# ──────────────────────────────────────────
# 站点字典（全量，来自 12306 官方 station_name.js）
# ──────────────────────────────────────────

@lru_cache(maxsize=1)
def load_stations() -> dict:
    """
    返回 {"name2code": {站名: 代码}, "code2name": {代码: 站名}}
    优先拉官方全量列表，失败则回退到内置常用站。
    """
    url = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
    name2code, code2name = {}, {}
    try:
        resp = SESSION.get(url, timeout=10)
        resp.raise_for_status()
        # 格式：@bjb|北京北|VAP|beijingbei|bjb|0@...
        for item in resp.text.split("@"):
            parts = item.split("|")
            if len(parts) >= 3 and parts[1] and parts[2]:
                name2code[parts[1]] = parts[2]
                code2name[parts[2]] = parts[1]
    except requests.RequestException:
        pass

    if not name2code:                      # 网络失败时的兜底
        for name, code in _FALLBACK_STATIONS.items():
            name2code[name] = code
            code2name[code] = name

    return {"name2code": name2code, "code2name": code2name}


def code_of(name: str) -> str | None:
    return load_stations()["name2code"].get(name)


def name_of(code: str) -> str:
    return load_stations()["code2name"].get(code, code)


# ──────────────────────────────────────────
# 查询余票
# ──────────────────────────────────────────

# 12306 查票接口会在 query / queryA / queryZ 之间切换；
# 直接请求前必须先访问 init 页面种下会话 Cookie，否则返回 HTML 错误页。
_QUERY_PATHS = ["leftTicket/queryG", "leftTicket/queryA",
                "leftTicket/queryZ", "leftTicket/query"]
_WARMED = {"done": False}


def _warmup():
    """访问 12306 查票首页，获取必要 Cookie。只需成功一次。"""
    if _WARMED["done"]:
        return
    try:
        SESSION.get("https://kyfw.12306.cn/otn/leftTicket/init",
                    params={"linktypeid": "dc"}, timeout=12)
        _WARMED["done"] = True
    except requests.RequestException:
        pass


def query_tickets(from_code: str, to_code: str, date: str) -> list[dict]:
    """调用 leftTicket 查票接口，返回该区段所有列车。"""
    _warmup()
    params = {
        "leftTicketDTO.train_date": date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": "ADULT",
    }
    data = None
    for path in _QUERY_PATHS:
        url = f"https://kyfw.12306.cn/otn/{path}"
        try:
            resp = SESSION.get(url, params=params, timeout=12)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and "data" in data:
            break  # 命中正确的接口路径
        data = None

    if not isinstance(data, dict) or data.get("status") is not True:
        return []

    trains = []
    for raw in data.get("data", {}).get("result", []):
        cols = raw.split("|")
        if len(cols) < 35:
            continue
        seats = {}
        for seat_name, idx in SEAT_INDEX.items():
            val = cols[idx].strip()
            seats[seat_name] = val if val else "--"
        trains.append({
            "train_no":     cols[2],          # 内部车次号（查经停用）
            "train_name":   cols[3],          # 对外车次名 G1/K123
            "from_code":    cols[6],
            "to_code":      cols[7],
            "from_name":    name_of(cols[6]),
            "to_name":      name_of(cols[7]),
            "from_time":    cols[8],
            "to_time":      cols[9],
            "duration":     cols[10],
            "seats":        seats,
        })
    return trains


# ──────────────────────────────────────────
# 查询经停站
# ──────────────────────────────────────────

@lru_cache(maxsize=512)
def query_stops(train_no: str, from_code: str, to_code: str, date: str) -> tuple:
    """
    返回某趟车的经停站名序列（tuple，便于缓存）。
    失败返回空 tuple。
    """
    url = "https://kyfw.12306.cn/otn/czxx/queryByTrainNo"
    params = {
        "train_no": train_no,
        "from_station_telecode": from_code,
        "to_station_telecode": to_code,
        "depart_date": date,
    }
    try:
        resp = SESSION.get(url, params=params, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError):
        return ()

    stops = data.get("data", {}).get("data", [])
    return tuple(s.get("station_name", "") for s in stops if s.get("station_name"))


# ──────────────────────────────────────────
# 判断 / 格式化
# ──────────────────────────────────────────

def has_ticket(seats: dict, watch_types: list[str]) -> bool:
    return any(seats.get(t, "--") not in EMPTY for t in watch_types)


def avail_summary(seats: dict, watch_types: list[str]) -> list[dict]:
    """返回有票的座位明细 [{type, count}]"""
    out = []
    for t in watch_types:
        val = seats.get(t, "--")
        if val not in EMPTY:
            out.append({"type": t, "count": val})
    return out


def match_train_type(train_name: str, train_types: list[str]) -> bool:
    """train_types 为空 = 全部；否则按首字母前缀匹配。"""
    if not train_types:
        return True
    prefixes = ()
    for t in train_types:
        prefixes += TRAIN_TYPE_PREFIX.get(t, ())
    if not prefixes:
        return True
    head = (train_name[:1] or "").upper()
    if head in prefixes:
        return True
    # “其他”类（数字车次 / 其它字母）
    if "OTHER" in train_types and head not in sum(TRAIN_TYPE_PREFIX.values(), ()):
        return True
    return False


# ──────────────────────────────────────────
# 主查询：直达 + 买长乘短延伸
# ──────────────────────────────────────────

def search(from_name: str, to_name: str, date: str,
           train_types: list[str], seat_types: list[str],
           extend: int = 1, max_extend_queries: int = 40,
           train_names: list[str] | None = None) -> dict:
    """
    返回：
    {
      "ok": bool, "error": str,
      "from": from_name, "to": to_name, "date": date,
      "trains": [
         {... 车次基本信息, "seats", "has",            # 直达是否有票
          "alternatives": [ {from_name,to_name,from_time,to_time,
                             avail:[{type,count}], label} ] }
      ]
    }
    """
    from_code = code_of(from_name)
    to_code   = code_of(to_name)
    if not from_code:
        return {"ok": False, "error": f"未找到出发站「{from_name}」"}
    if not to_code:
        return {"ok": False, "error": f"未找到到达站「{to_name}」"}

    direct = query_tickets(from_code, to_code, date)
    if not direct:
        return {"ok": True, "from": from_name, "to": to_name, "date": date,
                "trains": [], "note": "该区段未查询到列车（或被 12306 限速，稍后再试）"}

    # 车次类型过滤
    direct = [t for t in direct if match_train_type(t["train_name"], train_types)]

    # 指定车次过滤（如 ["G1","G3"]，留空=不限）
    if train_names:
        wanted = {n.strip().upper() for n in train_names if n.strip()}
        if wanted:
            direct = [t for t in direct if t["train_name"].upper() in wanted]

    trains_out = []
    seg_cache: dict[tuple, dict] = {}   # (fc,tc) -> {train_no: train}
    query_budget = max_extend_queries

    def seg_query(fc: str, tc: str) -> dict:
        nonlocal query_budget
        key = (fc, tc)
        if key in seg_cache:
            return seg_cache[key]
        if query_budget <= 0:
            return {}
        query_budget -= 1
        time.sleep(0.25)               # 轻微限速，避免触发风控
        mp = {t["train_no"]: t for t in query_tickets(fc, tc, date)}
        seg_cache[key] = mp
        return mp

    for t in direct:
        has = has_ticket(t["seats"], seat_types)
        entry = {
            "train_no":   t["train_no"],
            "train_name": t["train_name"],
            "from_name":  t["from_name"],
            "to_name":    t["to_name"],
            "from_code":  t["from_code"],
            "to_code":    t["to_code"],
            "from_time":  t["from_time"],
            "to_time":    t["to_time"],
            "duration":   t["duration"],
            "avail":      avail_summary(t["seats"], seat_types),
            "has":        has,
            "alternatives": [],
        }

        # 直达没票 → 买长乘短延伸
        if not has and extend > 0 and query_budget > 0:
            stops = query_stops(t["train_no"], t["from_code"], t["to_code"], date)
            if stops:
                try:
                    fi = stops.index(t["from_name"])
                    ti = stops.index(t["to_name"])
                except ValueError:
                    fi = ti = -1
                if 0 <= fi < ti:
                    board_idx = [fi] + [fi - k for k in range(1, extend + 1) if fi - k >= 0]
                    alight_idx = [ti] + [ti + k for k in range(1, extend + 1) if ti + k < len(stops)]
                    for bi in board_idx:
                        for ai in alight_idx:
                            if bi == fi and ai == ti:
                                continue          # 跳过原区段
                            if bi >= ai:
                                continue
                            bname, aname = stops[bi], stops[ai]
                            bcode, acode = code_of(bname), code_of(aname)
                            if not bcode or not acode:
                                continue
                            mp = seg_query(bcode, acode)
                            cand = mp.get(t["train_no"])
                            if not cand:
                                continue
                            if has_ticket(cand["seats"], seat_types):
                                entry["alternatives"].append({
                                    "from_name": bname,
                                    "to_name":   aname,
                                    "from_code": bcode,
                                    "to_code":   acode,
                                    "from_time": cand["from_time"],
                                    "to_time":   cand["to_time"],
                                    "avail":     avail_summary(cand["seats"], seat_types),
                                    "label":     _ext_label(bi, fi, ai, ti),
                                })

        trains_out.append(entry)

    return {"ok": True, "from": from_name, "to": to_name, "date": date,
            "from_code": from_code, "to_code": to_code,
            "trains": trains_out}


def _ext_label(bi: int, fi: int, ai: int, ti: int) -> str:
    parts = []
    if bi < fi:
        parts.append(f"提前{fi - bi}站上车")
    if ai > ti:
        parts.append(f"延后{ai - ti}站下车")
    return " · ".join(parts) or "区段延伸"


# 网络失败时的兜底站点（与原 monitor.py 一致的常用站）
_FALLBACK_STATIONS = {
    "北京": "BJP", "北京南": "VNP", "北京西": "BXP", "北京东": "BOP",
    "上海": "SHH", "上海虹桥": "AOH",
    "广州": "GZQ", "广州南": "IZQ", "深圳": "SZQ", "深圳北": "IOQ",
    "成都": "CDW", "成都东": "ICW", "武汉": "WHN",
    "西安": "XAY", "西安北": "EAY", "杭州": "HZH", "杭州东": "HGH",
    "南京": "NJH", "南京南": "NKH", "天津": "TJP", "重庆": "CQW",
    "长沙": "CSQ", "长沙南": "CNQ", "郑州": "ZZF", "郑州东": "ZAF",
    "济南": "JNK", "济南西": "JIK", "青岛": "QDK", "哈尔滨": "HBB",
    "沈阳": "SYT", "大连": "DLT", "长春": "CCT", "昆明": "KMM",
    "贵阳": "GYQ", "南宁": "NNZ", "厦门": "XMS", "福州": "FZS",
    "合肥": "HFH", "南昌": "NCG", "太原": "TYV", "石家庄": "SJP",
    "苏州": "SZH", "无锡": "WXH", "宁波": "NGH", "温州": "WZS",
    "兰州": "LZJ", "乌鲁木齐": "WLJ", "银川": "YCJ", "呼和浩特": "HHB",
}