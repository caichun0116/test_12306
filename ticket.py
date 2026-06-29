
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
import threading
import requests
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:                       # 极老版本兜底
    Retry = None

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

# 座位类型 → 官方 queryTicketPrice 返回里的价格字段 key（按顺序尝试，命中即用）
# 实测响应含 A9/M/O/WZ 等 key，值形如 "¥598.0"。
PRICE_KEY = {
    "商务座":   ("A9", "9"),
    "一等座":   ("M",),
    "二等座":   ("O",),
    "高级软卧": ("A6",),
    "软卧":     ("A4",),
    "动卧":     ("F",),
    "硬卧":     ("A3",),
    "软座":     ("A2",),
    "硬座":     ("A1",),
    "无座":     ("WZ",),
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

# 连接池 + 瞬时错误自动重试（并发查询时复用 TCP 连接，显著降低握手开销）
_pool_kwargs = dict(pool_connections=16, pool_maxsize=16)
if Retry is not None:
    _pool_kwargs["max_retries"] = Retry(
        total=2, backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
_adapter = HTTPAdapter(**_pool_kwargs)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

EMPTY = ("--", "无", "0", "", "*")

# ──────────────────────────────────────────
# 并发与限速配置（买长乘短会产生大量区段查询，需并发但要防风控）
# ──────────────────────────────────────────
MAX_WORKERS = 6            # 同时在途的查询数
_MIN_INTERVAL = 0.12       # 相邻查询「起点」最小间隔（秒），约 8 req/s


class _RateGate:
    """线程安全的最小间隔闸门：错开请求起点，但允许在网络层重叠。"""

    def __init__(self, min_interval: float):
        self._lock = threading.Lock()
        self._min = min_interval
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._min
        if delay:
            time.sleep(delay)


_RATE = _RateGate(_MIN_INTERVAL)
# 票价接口（queryTicketPrice）对非登录会话限流更严，单独用更慢的闸门
_PRICE_RATE = _RateGate(0.5)


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
# 记住上次命中的接口路径，下次优先尝试，避免每次都从头试 4 个
_GOOD_PATH = {"path": None}


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
    # 优先尝试上次命中的路径，其余作为兜底
    good = _GOOD_PATH["path"]
    paths = _QUERY_PATHS if not good else \
        [good] + [p for p in _QUERY_PATHS if p != good]
    for path in paths:
        url = f"https://kyfw.12306.cn/otn/{path}"
        try:
            resp = SESSION.get(url, params=params, timeout=12)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and "data" in data:
            _GOOD_PATH["path"] = path   # 记住命中路径
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
            "no_from":      cols[16],          # 车次内出发站序号（查票价用）
            "no_to":        cols[17],          # 车次内到达站序号（查票价用）
            "seat_code":    cols[35] if len(cols) > 35 else "",  # 座位类型代码串（查票价用）
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
# 查询票价
# ──────────────────────────────────────────

# 票价在整个售票周期内稳定，手写缓存：只缓存「成功」结果，
# 避免把限流导致的空结果永久缓存（lru_cache 会缓存失败，故不用）。
_PRICE_CACHE: dict[tuple, tuple] = {}
_PRICE_LOCK = threading.Lock()


def query_price(train_no: str, no_from: str, no_to: str,
                seat_code: str, date: str) -> tuple:
    """
    返回某车次某区段的票价 (tuple of (座位名, 价格float)，便于缓存)。
    首轮付费、监控后续轮命中缓存免费。失败返回空 tuple（不缓存，下轮可重试）。
    """
    if not (train_no and no_from and no_to and seat_code):
        return ()
    key = (train_no, no_from, no_to, seat_code, date)
    with _PRICE_LOCK:
        hit = _PRICE_CACHE.get(key)
    if hit is not None:
        return hit

    url = "https://kyfw.12306.cn/otn/leftTicket/queryTicketPrice"
    params = {
        "train_no": train_no,
        "from_station_no": no_from,
        "to_station_no": no_to,
        "seat_types": seat_code,
        "train_date": date,
    }
    # 价格接口偶发返回空（限流）：失败时重新预热再试一次
    for attempt in range(2):
        _warmup()
        try:
            resp = SESSION.get(url, params=params, timeout=12)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            data = None

        prices = (data or {}).get("data") if isinstance(data, dict) else None
        if isinstance(prices, dict) and prices:
            out = []
            for seat_name, keys in PRICE_KEY.items():
                for k in keys:
                    val = _parse_price(prices.get(k))
                    if val is not None:
                        out.append((seat_name, val))
                        break
            result = tuple(out)
            if result:                       # 只缓存成功结果
                with _PRICE_LOCK:
                    _PRICE_CACHE[key] = result
                return result
        # 空结果：标记需要重新预热，下一次尝试前 _warmup 会重新种 Cookie
        _WARMED["done"] = False
    return ()


def _parse_price(raw) -> float | None:
    """'¥598.0' / '598.0' / 59800(分) → 598.0；无法解析返回 None。"""
    if raw in (None, "", "--"):
        return None
    s = str(raw).strip().lstrip("¥￥").strip()
    try:
        return float(s)
    except ValueError:
        return None


def price_map(train: dict, date: str) -> dict:
    """对一趟车（query_tickets 的元素）查票价，返回 {座位名: 价格float}。"""
    pairs = query_price(train.get("train_no", ""), train.get("no_from", ""),
                        train.get("no_to", ""), train.get("seat_code", ""), date)
    return dict(pairs)


# ──────────────────────────────────────────
# 12306 官方下单/候补深链
# ──────────────────────────────────────────

def book_url(from_name: str, from_code: str,
             to_name: str, to_code: str, date: str) -> str:
    """生成 12306 官方查票/下单页深链（已登录则直接进登录态）。

    注意：fs/ts 必须用「站名,代码」字面逗号，逗号不能被编码，否则 12306 识别不了。
    与前端 bookUrl()、app._build_order_url() 同构，三处共用。
    """
    if not (from_code and to_code and date):
        return ""
    return (
        "https://kyfw.12306.cn/otn/leftTicket/init"
        f"?linktypeid=dc&fs={from_name},{from_code}&ts={to_name},{to_code}"
        f"&date={date}&flag=N,N,Y"
    )


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
           train_names: list[str] | None = None,
           with_price: bool = False, price_max: float | None = None) -> dict:
    """
    返回：
    {
      "ok": bool, "error": str,
      "from": from_name, "to": to_name, "date": date,
      "trains": [
         {... 车次基本信息, "seats", "has",            # 直达是否有票
          "avail": [{type,count,price?}], "price_min"?,
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

    # ── 基础条目（直达信息，纯 CPU，无网络）──
    entries = []
    for t in direct:
        has = has_ticket(t["seats"], seat_types)
        entries.append({
            "train_no":   t["train_no"],
            "train_name": t["train_name"],
            "from_name":  t["from_name"],
            "to_name":    t["to_name"],
            "from_code":  t["from_code"],
            "to_code":    t["to_code"],
            "from_time":  t["from_time"],
            "to_time":    t["to_time"],
            "duration":   t["duration"],
            # 票价按需查询所需字段（供前端 /api/price 懒加载补拉）
            "no_from":    t["no_from"],
            "no_to":      t["no_to"],
            "seat_code":  t["seat_code"],
            "avail":      avail_summary(t["seats"], seat_types),
            "has":        has,
            "alternatives": [],
            "_raw":       t,
        })

    need_ext = [e for e in entries if not e["has"]] if extend > 0 else []

    if need_ext:
        # ── 阶段 1：并发拉取各车次经停站 ──
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            stops_list = list(ex.map(
                lambda e: query_stops(e["train_no"], e["from_code"],
                                      e["to_code"], date),
                need_ext))

        # ── 阶段 2：跨车次收集候选区段并去重 ──
        train_segs: dict[str, list] = {}   # train_no -> [(bcode,acode,bname,aname,label)]
        unique_segs: dict[tuple, None] = {}
        for e, stops in zip(need_ext, stops_list):
            segs = _alt_segments(stops, e["from_name"], e["to_name"], extend)
            train_segs[e["train_no"]] = segs
            for bc, ac, _, _, _ in segs:
                unique_segs[(bc, ac)] = None

        # 按预算截断唯一区段数量（dict 保持插入顺序，确定性）
        seg_list = list(unique_segs.keys())[:max(0, max_extend_queries)]

        # ── 阶段 3：并发拉取唯一区段余票（限速闸门防风控）──
        seg_cache: dict[tuple, dict] = {}
        if seg_list:
            def fetch_seg(key):
                fc, tc = key
                _RATE.wait()
                return key, {x["train_no"]: x
                             for x in query_tickets(fc, tc, date)}
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                for key, mp in ex.map(fetch_seg, seg_list):
                    seg_cache[key] = mp

        # ── 阶段 4：组装备选（纯 CPU）──
        for e in need_ext:
            for bc, ac, bname, aname, label in train_segs.get(e["train_no"], []):
                mp = seg_cache.get((bc, ac))
                if not mp:
                    continue
                cand = mp.get(e["train_no"])
                if not cand or not has_ticket(cand["seats"], seat_types):
                    continue
                e["alternatives"].append({
                    "from_name": bname,
                    "to_name":   aname,
                    "from_code": bc,
                    "to_code":   ac,
                    "from_time": cand["from_time"],
                    "to_time":   cand["to_time"],
                    "avail":     avail_summary(cand["seats"], seat_types),
                    "label":     label,
                })

    # ── 票价（仅直达车次，可选）──
    # lru_cache 保证监控重复轮免费；并发拉取 + 全局限速闸门防风控。
    # ── 票价（仅直达车次，可选；best-effort）──
    # 价格接口限流严，用更慢的专用闸门尽力拉取；缓存成功结果，
    # 监控后续轮 / 重查命中缓存免费，宽泛路线拉不全的由前端按需补拉。
    if (with_price or price_max is not None) and entries:
        def fetch_price(e):
            _PRICE_RATE.wait()
            return price_map(e["_raw"], date)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            price_list = list(ex.map(fetch_price, entries))
        for e, pm in zip(entries, price_list):
            prices = []
            for s in e["avail"]:
                p = pm.get(s["type"])
                if p is not None:
                    s["price"] = p
                    prices.append(p)
            if prices:
                e["price_min"] = min(prices)

    for e in entries:
        e.pop("_raw", None)

    # ── 价格上限过滤（仅作用于有票车次）──
    # 价格未知（限流未拉到）的车次保留，避免误删；只过滤已知且超价的。
    if price_max is not None:
        kept = []
        for e in entries:
            if not e["has"] or "price_min" not in e:
                kept.append(e)
                continue
            if any(s.get("price") is not None and s["price"] <= price_max
                   for s in e["avail"]):
                kept.append(e)
        entries = kept

    return {"ok": True, "from": from_name, "to": to_name, "date": date,
            "from_code": from_code, "to_code": to_code,
            "trains": entries}


def _alt_segments(stops: tuple, from_name: str, to_name: str,
                  extend: int) -> list:
    """根据经停站序列，列出「买长乘短」候选区段（不含原区段）。

    返回 [(bcode, acode, bname, aname, label)]，与原串行逻辑等价。
    """
    if not stops:
        return []
    try:
        fi = stops.index(from_name)
        ti = stops.index(to_name)
    except ValueError:
        return []
    if not (0 <= fi < ti):
        return []

    board_idx = [fi] + [fi - k for k in range(1, extend + 1) if fi - k >= 0]
    alight_idx = [ti] + [ti + k for k in range(1, extend + 1) if ti + k < len(stops)]
    out = []
    for bi in board_idx:
        for ai in alight_idx:
            if bi == fi and ai == ti:
                continue              # 跳过原区段
            if bi >= ai:
                continue
            bname, aname = stops[bi], stops[ai]
            bcode, acode = code_of(bname), code_of(aname)
            if not bcode or not acode:
                continue
            out.append((bcode, acode, bname, aname, _ext_label(bi, fi, ai, ti)))
    return out


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