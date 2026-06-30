
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
import threading
from datetime import datetime
from typing import Callable

import ticket
import notify
import order12306
from order12306 import LOGIN

_JOBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "order_jobs.json")

_LOG_MAX = 60
_EXTEND_BUDGET = lambda ext: 40 + max(0, min(ext, 5)) * 20


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


def _same_passenger(saved: dict, fresh: dict) -> bool:
    """12306 乘客证件号可能是脱敏值，匹配时用多字段兜底。"""
    if saved.get("allEncStr") and saved.get("allEncStr") == fresh.get("allEncStr"):
        return True
    if saved.get("id_no") and saved.get("id_no") == fresh.get("id_no"):
        return True
    return (
        saved.get("name") == fresh.get("name") and
        saved.get("id_type_code", "1") == fresh.get("id_type_code", "1") and
        saved.get("passenger_type", "1") == fresh.get("passenger_type", "1")
    )


class OrderJob:
    """单个自动抢票任务。"""

    def __init__(self, cfg: dict, jid: str | None = None,
                 on_change: Callable[[], None] | None = None):
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
        # —— 推送配置 ——
        self.channel     = (cfg.get("channel") or "").strip()
        self.token       = (cfg.get("token") or "").strip()
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

    # ── 序列化 ──
    def to_config(self) -> dict:
        return {
            "id": self.id, "from": self.from_name, "to": self.to_name,
            "dates": self.dates, "train_types": self.train_types,
            "train_names": self.train_names, "seat_types": self.seat_types,
            "extend": self.extend, "allow_extend": self.allow_extend,
            "interval": self.interval, "passengers": self.passengers,
            "channel": self.channel, "token": self.token,
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
            return
        if not self.seat_types:
            self.status = "error"
            self.last_error = "未勾选要抢的坐席"
            self._changed()
            return
        while not self._stop.is_set():
            done = False
            try:
                self.last_error = ""
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
            self._stop.wait(self.interval)

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
        budget = _EXTEND_BUDGET(self.extend)
        queries = 0
        seg_cache: dict[tuple, list] = {}
        rate = getattr(ticket, "_RATE", None)
        for t in direct:
            if t["train_no"] in direct_train_nos:
                continue
            stops = ticket.query_stops(t["train_no"], t["from_code"],
                                       t["to_code"], date)
            segs = ticket._alt_segments(stops, t["from_name"], t["to_name"],
                                        self.extend)
            for bc, ac, bname, aname, _label in segs:
                if queries >= budget:
                    return out, _candidate_summary(
                        date, raw_count, filtered_count, len(out), seat_label,
                        f"买长乘短已查 {queries} 个延伸区段")
                key = (bc, ac)
                if key not in seg_cache:
                    if rate:
                        rate.wait()
                    seg_cache[key] = ticket.query_tickets(bc, ac, date)
                    queries += 1
                rows = seg_cache[key]
                same = next((x for x in rows
                             if x["train_no"] == t["train_no"]), None)
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
            f"买长乘短已查 {queries} 个延伸区段")

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
        self._load()

    def create(self, cfg: dict) -> OrderJob:
        job = OrderJob(cfg, on_change=self._save)
        with self._lock:
            self._jobs[job.id] = job
        job.start()
        self._save()
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
        self._save()
        return True

    def stop(self, jid: str) -> bool:
        job = self.get(jid)
        if not job:
            return False
        job.stop()
        self._save()
        return True

    def delete(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.pop(jid, None)
        if not job:
            return False
        job.stop()
        self._save()
        return True

    # ── 持久化 ──
    def _save(self):
        with self._lock:
            data = [j.to_config() for j in self._jobs.values()]
        try:
            with open(_JOBS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _load(self):
        if not os.path.exists(_JOBS_FILE):
            return
        try:
            with open(_JOBS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        for cfg in data or []:
            job = OrderJob(cfg, jid=cfg.get("id"), on_change=self._save)
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
