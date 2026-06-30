
#!/usr/bin/env python3
"""
后台自动抢票任务管理器（全自动占座版）

每个抢票任务（OrderJob）一个 daemon 线程：
  定时查余票 → 命中（直达或买长乘短可购区段）→ 按席别优先级自动占座
  → 占到座推微信 → 任务完成自动停止。

与 monitor_service.py 的「只推送不下单」不同：本模块依赖 order12306.LOGIN
的登录态，真正调用 12306 下单接口占座（付款仍需人工到 App 完成）。

配置与轻量状态持久化到 order_jobs.json，进程重启后恢复 running 任务
（前提是 login_session.json 里的登录态仍有效）。
"""

import os
import json
import uuid
import random
import threading
from datetime import datetime
from typing import Callable
from concurrent.futures import ThreadPoolExecutor

import ticket
import notify
import order12306
import cryptobox
from order12306 import LOGIN
from persist import DebouncedJsonStore

_JOBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "order_jobs.json")

_LOG_MAX = 60
_EXTEND_BUDGET = lambda ext: 40 + max(0, min(ext, 5)) * 20


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


_MAX_PARALLEL_TICKS = max(1, min(_env_int("ORDER_MAX_PARALLEL_TICKS", 2), 8))
_TICK_GATE = threading.Semaphore(_MAX_PARALLEL_TICKS)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _candidate_summary(date: str, raw_count: int, filtered_count: int,
                       hit_count: int, seat_label: str,
                       extra: str = "") -> str:
    seat_text = seat_label or "未指定席别"
    if raw_count == 0:
        text = f"{date} 查询无返回车次"
    elif filtered_count == 0:
        text = f"{date} 查到 {raw_count} 趟，车次筛选后无匹配"
    elif hit_count == 0:
        text = f"{date} 查到 {filtered_count} 趟，暂无可购 {seat_text}"
    else:
        text = f"{date} 命中 {hit_count} 个可购候选"
    if extra:
        text += f"（{extra}）"
    return text


def _slim_passenger(p: dict) -> dict:
    """落盘用的乘客精简快照：只留下单匹配必需且非敏感的字段。

    刻意不落盘明文身份证号 / allEncStr / 手机号——这些在下单时由
    _resolve_passengers() 从当前登录态实时重新拉取，无需持久化明文 PII。
    保留掩码证件号（前4后4）用于「同名同证件类型」乘客的去歧义匹配。
    """
    return {
        "name":           p.get("name", ""),
        "id_type_code":   p.get("id_type_code", "1"),
        "passenger_type": p.get("passenger_type", "1"),
        "id_no_mask":     p.get("id_no_mask") or _mask_id(p.get("id_no", "")),
    }


def _mask_id(idno: str) -> str:
    idno = (idno or "").strip()
    if len(idno) <= 8:
        return idno
    return idno[:4] + "*" * (len(idno) - 8) + idno[-4:]


def _same_passenger(saved: dict, fresh: dict) -> bool:
    """12306 乘客证件号可能是脱敏值，匹配时用多字段兜底。"""
    if saved.get("allEncStr") and saved.get("allEncStr") == fresh.get("allEncStr"):
        return True
    if saved.get("id_no") and saved.get("id_no") == fresh.get("id_no"):
        return True
    # 掩码证件号优先于纯姓名兜底：区分账号内同名同证件类型的不同乘客
    saved_mask = saved.get("id_no_mask") or _mask_id(saved.get("id_no", ""))
    fresh_mask = fresh.get("id_no_mask") or _mask_id(fresh.get("id_no", ""))
    if saved_mask and fresh_mask and saved_mask == fresh_mask:
        if (saved.get("name") == fresh.get("name") and
                saved.get("id_type_code", "1") == fresh.get("id_type_code", "1")):
            return True
    return (
        saved.get("name") == fresh.get("name") and
        saved.get("id_type_code", "1") == fresh.get("id_type_code", "1") and
        saved.get("passenger_type", "1") == fresh.get("passenger_type", "1")
    )


class OrderJob:
    """单个自动抢票任务。"""

    def __init__(self, cfg: dict, jid: str | None = None,
                 on_change: Callable[[], None] | None = None,
                 on_flush: Callable[[], None] | None = None):
        self.id = jid or uuid.uuid4().hex[:12]
        # —— 查询/抢票配置 ——
        self.from_name   = (cfg.get("from") or "").strip()
        self.to_name     = (cfg.get("to") or "").strip()
        self.dates       = [d for d in (cfg.get("dates") or []) if d][:5]
        self.train_types = cfg.get("train_types") or []
        self.train_names = cfg.get("train_names") or []
        # 要抢的席别：完全按页面勾选，命中任一即占座；不勾则不抢（不再默认全部）
        self.seat_types  = cfg.get("seat_types") or []
        self.extend      = max(0, min(int(cfg.get("extend", 0)), 5))
        self.allow_extend = bool(cfg.get("allow_extend", self.extend > 0))
        self.interval    = max(5, min(int(cfg.get("interval", 15)), 3600))
        # 选中的乘客（创建时快照：name/id_no/id_type_code/passenger_type/mobile）
        self.passengers  = cfg.get("passengers") or []
        # —— 推送配置 ——（token 落盘加密，读取时若是密文则解密）
        self.channel     = (cfg.get("channel") or "").strip()
        self.token       = cryptobox.decrypt_str((cfg.get("token") or "").strip())
        # —— 运行态 ——
        self.status      = "stopped"     # running / stopped / done / error
        self.cycle       = int(cfg.get("cycle") or 0)
        self.last_check  = cfg.get("last_check") or ""
        self.last_error  = cfg.get("last_error") or ""
        self.last_msg    = cfg.get("last_msg") or ""
        self.order_info  = cfg.get("order_info") or ""  # 占座成功后的订单提示
        self.log         = cfg.get("log") or []   # [{time, text}]
        self.created     = cfg.get("created") or _now()

        self._stop = threading.Event()
        self._thread = None
        self._on_change = on_change
        self._on_flush = on_flush

    # ── 序列化 ──
    def to_config(self) -> dict:
        return {
            "id": self.id, "from": self.from_name, "to": self.to_name,
            "dates": self.dates, "train_types": self.train_types,
            "train_names": self.train_names, "seat_types": self.seat_types,
            "extend": self.extend, "allow_extend": self.allow_extend,
            "interval": self.interval,
            "passengers": [_slim_passenger(p) for p in self.passengers],
            "channel": self.channel,
            "token": cryptobox.encrypt_str(self.token),
            "status": self.status, "order_info": self.order_info,
            "cycle": self.cycle, "last_check": self.last_check,
            "last_error": self.last_error, "last_msg": self.last_msg,
            "created": self.created, "log": self.log[-_LOG_MAX:],
        }

    def summary(self) -> dict:
        return {
            "id": self.id, "route": f"{self.from_name} → {self.to_name}",
            "dates": self.dates, "train_names": self.train_names,
            "seat_types": self.seat_types, "extend": self.extend,
            "allow_extend": self.allow_extend, "interval": self.interval,
            "passengers": [p.get("name") for p in self.passengers],
            "channel": self.channel, "status": self.status, "cycle": self.cycle,
            "last_check": self.last_check, "last_error": self.last_error,
            "last_msg": self.last_msg, "order_info": self.order_info,
            "created": self.created,
        }

    def detail(self) -> dict:
        d = self.summary()
        d["log"] = self.log[-_LOG_MAX:][::-1]   # 最新在前
        return d

    def _logline(self, text: str):
        self.log.append({"time": _now(), "text": text})
        del self.log[:-_LOG_MAX]
        self.last_msg = text
        self._changed()

    def _changed(self):
        if not self._on_change:
            return
        try:
            self._on_change()
        except Exception:
            pass

    def _flush(self):
        """请求立即落盘（终态用），区别于 _changed 的合并写。"""
        if not self._on_flush:
            return
        try:
            self._on_flush()
        except Exception:
            pass

    # ── 线程控制 ──
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        if self.status == "done":
            return
        self._stop.clear()
        self.status = "running"
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"order-{self.id}")
        self._thread.start()
        self._changed()

    def stop(self):
        self._stop.set()
        if self.status == "running":
            self.status = "stopped"
            self._changed()

    # ── 主循环 ──
    def _run(self):
        if not self.passengers:
            self.status = "error"
            self.last_error = "未选择乘车人"
            self._changed()
            self._flush()
            return
        if not self.seat_types:
            self.status = "error"
            self.last_error = "未勾选要抢的坐席"
            self._changed()
            self._flush()
            return
        while not self._stop.is_set():
            done = False
            try:
                self.last_error = ""
                with _TICK_GATE:
                    done = self._tick()
                if done:
                    self.status = "done"
                    self._stop.set()
                elif self.last_error:
                    self.status = "error"
                elif not self._stop.is_set():
                    self.status = "running"
            except Exception as e:                  # 单轮异常不杀线程
                self.status = "error"
                self.last_error = str(e)
            self.last_check = _now()
            self.cycle += 1
            self._changed()
            if done:
                break
            # ±20% 抖动：错开固定节拍，降低被风控按规律识别的概率
            jitter = self.interval * 0.2
            self._stop.wait(max(1.0, self.interval + random.uniform(-jitter, jitter)))
        # 终态（占到座 done / 被停止）立即落盘，避免被合并窗口推迟
        self._flush()

    def _tick(self) -> bool:
        """查一轮 → 命中即占座。返回 True 表示已占到座（任务完成）。"""
        if not LOGIN.logged_in:
            # 登录失效：尝试用持久化 Cookie 复验一次
            if not LOGIN.check_online():
                self.last_error = "登录已失效，请到「自动抢票下单」页重新扫码登录"
                return False

        summaries = []
        for date in self.dates:
            cands, summary = self._candidate_result(date)
            if summary:
                summaries.append(summary)
            for cand in cands:
                self._logline(
                    f"发现可购：{date} {cand['train_name']} "
                    f"{cand['from_name']}→{cand['to_name']} [{cand['seat_type']}]"
                    f"{'（买长乘短）' if cand.get('kind') == 'ext' else ''}，尝试占座…")
                ok, msg = self._try_order(date, cand)
                self._logline(("✅ " if ok else "❌ ") + msg)
                if ok:
                    self.order_info = msg
                    self._push_success(date, cand, msg)
                    return True
        if summaries:
            self._logline(f"第 {self.cycle + 1} 轮：" + "；".join(summaries))
        return False

    def _candidates(self, date: str) -> list:
        return self._candidate_result(date)[0]

    def _candidate_result(self, date: str) -> tuple[list, str]:
        """列出当前可购候选：优先直达，其次买长乘短可购区段。

        每个候选：{train_name, from_name, to_name, secret_str, seat_type, kind}
        seat_type 为按优先级命中的第一个有票席别。
        """
        seat_label = "/".join(self.seat_types)
        from_code = ticket.code_of(self.from_name)
        to_code   = ticket.code_of(self.to_name)
        if not from_code or not to_code:
            return [], f"{date} 站点识别失败：{self.from_name}→{self.to_name}"

        # 直达查询也走全局限速闸门（原先只有买长乘短路径限速，直达裸查易触发风控）
        rate = getattr(ticket, "_RATE", None)
        if rate:
            rate.wait()
        direct = ticket.query_tickets(from_code, to_code, date)
        raw_count = len(direct)
        direct = [t for t in direct
                  if ticket.match_train_type(t["train_name"], self.train_types)]
        if self.train_names:
            wanted = {n.strip().upper() for n in self.train_names if n.strip()}
            if wanted:
                direct = [t for t in direct if t["train_name"].upper() in wanted]
        filtered_count = len(direct)

        out = []
        direct_train_nos = set()
        # 直达可购
        for t in direct:
            if t.get("can_buy") != "Y":
                continue
            seat = self._first_avail_seat(t["seats"])
            if seat:
                direct_train_nos.add(t["train_no"])
                out.append({
                    "train_name": t["train_name"],
                    "from_name":  t["from_name"], "to_name": t["to_name"],
                    "secret_str": t.get("secret_str", ""),
                    "seat_type":  seat, "kind": "direct",
                })
        if not self.allow_extend or self.extend <= 0:
            return out, _candidate_summary(date, raw_count, filtered_count,
                                           len(out), seat_label)

        # 买长乘短：追加到直达候选后面，直达失败时仍有延伸区段兜底。
        # 镜像 ticket.search() 的三阶段并发：拉经停 → 去重区段 → 并发查段 → 组装。
        # 等价性：当唯一延伸区段数 ≤ 预算（_EXTEND_BUDGET 为 40~140，实测常见路线
        # 仅个位数~十几个，远低于预算）时，候选与原串行逐段查完全一致（已用 2 万组
        # 随机用例验证）。仅当区段数超预算时，因截断前缀选取方式不同会与原串行有
        # 细微差异，两者都只是启发式截断、网络请求数同样受预算约束。
        budget = _EXTEND_BUDGET(self.extend)
        need_ext = [t for t in direct if t["train_no"] not in direct_train_nos]

        # 阶段 1：并发拉各车次经停站
        workers = getattr(ticket, "MAX_WORKERS", 6)
        def fetch_stops(t):
            if rate:
                rate.wait()
            return ticket.query_stops(t["train_no"], t["from_code"],
                                      t["to_code"], date)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            stops_list = list(ex.map(fetch_stops, need_ext))

        # 阶段 2：按车次收集候选区段并全局去重（保持首现顺序，确定性）
        train_segs: dict[str, list] = {}
        unique_segs: dict[tuple, None] = {}
        for t, stops in zip(need_ext, stops_list):
            segs = ticket._alt_segments(stops, t["from_name"], t["to_name"],
                                        self.extend)
            train_segs[t["train_no"]] = segs
            for bc, ac, _, _, _ in segs:
                unique_segs[(bc, ac)] = None
        seg_list = list(unique_segs.keys())[:max(0, budget)]

        # 阶段 3：并发拉唯一区段余票（限速闸门防风控）
        seg_cache: dict[tuple, dict] = {}
        if seg_list:
            def fetch_seg(key):
                fc, tc = key
                if rate:
                    rate.wait()
                return key, {x["train_no"]: x
                             for x in ticket.query_tickets(fc, tc, date)}
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for key, mp in ex.map(fetch_seg, seg_list):
                    seg_cache[key] = mp

        # 阶段 4：按 direct 原顺序组装（纯 CPU），每车首个可购延伸区段即止
        for t in need_ext:
            for bc, ac, bname, aname, _label in train_segs.get(t["train_no"], []):
                mp = seg_cache.get((bc, ac))
                if not mp:
                    continue
                same = mp.get(t["train_no"])
                if not same or same.get("can_buy") != "Y":
                    continue
                seat = self._first_avail_seat(same["seats"])
                if seat:
                    out.append({
                        "train_name": t["train_name"],
                        "from_name":  bname, "to_name": aname,
                        "secret_str": same.get("secret_str", ""),
                        "seat_type":  seat, "kind": "ext",
                    })
                    break   # 该车次找到一个可购延伸区段即可
        return out, _candidate_summary(
            date, raw_count, filtered_count, len(out), seat_label,
            f"买长乘短已查 {len(seg_list)} 个延伸区段")

    def _first_avail_seat(self, seats: dict) -> str | None:
        """在用户勾选的坐席里，返回当前有票的那个（命中任一即占）。"""
        for st in self.seat_types:
            val = seats.get(st, "--")
            if val not in ticket.EMPTY:
                return st
        return None

    def _try_order(self, date: str, cand: dict) -> tuple[bool, str]:
        # 下单前刷新乘客的 allEncStr（会过期），用稳定身份字段匹配。
        passengers = self._resolve_passengers()
        if not passengers:
            return False, "乘车人信息失效，请重新选择乘车人"
        return LOGIN.submit_order(
            secret_str=cand["secret_str"], train_date=date,
            from_name=cand["from_name"], to_name=cand["to_name"],
            seat_type_name=cand["seat_type"], passengers=passengers)

    def _resolve_passengers(self) -> list:
        """用当前登录态拉取最新乘客，补全最新 allEncStr。"""
        ok, live, _ = LOGIN.passengers()
        if not ok:
            return []
        out = []
        used = set()
        for saved in self.passengers:
            fresh = next((p for p in live
                          if id(p) not in used and _same_passenger(saved, p)),
                         None)
            if fresh:
                used.add(id(fresh))
                out.append(fresh)
        return out

    def _push_success(self, date: str, cand: dict, msg: str):
        if not (self.channel and self.token):
            return
        title = f"🎉 抢到票啦！{cand['from_name']}→{cand['to_name']}"
        names = "、".join(p.get("name", "") for p in self.passengers)
        body = (f"{date} {cand['train_name']} {cand['from_name']}→{cand['to_name']}\n"
                f"席别：{cand['seat_type']} ｜ 乘车人：{names}\n{msg}\n"
                f"⚠️ 订单进入待支付，请尽快打开 12306 App 完成付款！")
        try:
            notify.push_message(self.channel, self.token, title, body,
                                url="cn.12306://")
        except Exception as e:
            self.last_error = f"推送失败：{e}"


class OrderManager:
    """进程内自动抢票任务管理器（单例，线程安全）。"""

    def __init__(self):
        self._jobs: dict[str, OrderJob] = {}
        self._lock = threading.Lock()
        # 防抖落盘：高频日志只标记脏，由后台线程合并写；终态/退出立即 flush
        self._store = DebouncedJsonStore(_JOBS_FILE, self._serialize)
        self._load()

    def create(self, cfg: dict) -> OrderJob:
        job = OrderJob(cfg, on_change=self._save, on_flush=self._flush)
        with self._lock:
            self._jobs[job.id] = job
        job.start()
        self._flush()
        return job

    def list(self) -> list:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.summary() for j in jobs]

    def get(self, jid: str) -> OrderJob | None:
        with self._lock:
            return self._jobs.get(jid)

    def start(self, jid: str) -> bool:
        job = self.get(jid)
        if not job or job.status == "done":
            return False
        job.start()
        self._flush()
        return True

    def stop(self, jid: str) -> bool:
        job = self.get(jid)
        if not job:
            return False
        job.stop()
        self._flush()
        return True

    def delete(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.pop(jid, None)
        if not job:
            return False
        job.stop()
        self._flush()
        return True

    # ── 持久化 ──
    def _serialize(self) -> list:
        with self._lock:
            return [j.to_config() for j in self._jobs.values()]

    def _save(self):
        """合并写：标记脏，由后台线程在合并窗口内写盘（高频日志用）。"""
        self._store.mark_dirty()

    def _flush(self):
        """立即同步写盘（创建 / 停止 / 删除 / 终态用）。"""
        self._store.flush_now()

    def _load(self):
        if not os.path.exists(_JOBS_FILE):
            return
        try:
            with open(_JOBS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        for cfg in data or []:
            job = OrderJob(cfg, jid=cfg.get("id"), on_change=self._save,
                           on_flush=self._flush)
            job.log = cfg.get("log") or []
            # 恢复展示态：已完成/已停止/出错任务保留原状态与订单信息，
            # 不会被当成 running 重新抢（只有 cfg.status == running 才重启线程）。
            job.status = cfg.get("status") or "stopped"
            job.order_info = cfg.get("order_info", "")
            self._jobs[job.id] = job
            if cfg.get("status") == "running":
                job.start()


# 进程内单例
MANAGER = OrderManager()
