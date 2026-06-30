#!/usr/bin/env python3
"""
服务端常驻监控（合规版）

把「定时查余票 → 有票推微信」放到后端线程：关掉网页、电脑不关，监控继续跑。
不自动登录、不自动下单——发现有票只推送官方下单/候补深链，下单仍在 12306 完成。

复用：
  - ticket.search()          ：直达 + 买长乘短查询（含全局限速闸门 _RATE / _PRICE_RATE）
  - ticket.book_url()        ：官方下单/候补深链（与网页一致）
  - notify.push_message()    ：渲染成与网页一致的「结果卡片」推到微信

每个监控任务（Job）一个 daemon 线程 + 停止事件；配置与轻量状态持久化到
monitor_jobs.json，进程重启后自动恢复 running 任务。
"""

import os
import json
import uuid
import random
import threading
from datetime import datetime

import ticket
import notify
import cryptobox
from persist import DebouncedJsonStore

_JOBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "monitor_jobs.json")

# 单任务命中日志保留条数上限，防止长期运行内存无限增长
_FOUND_LOG_MAX = 50
# 每轮查询的延伸预算（与 app.py 的 /api/query 口径一致）
_EXTEND_BUDGET = lambda ext: 60 + max(0, min(ext, 5)) * 30


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Job:
    """单个监控任务：配置 + 运行态 + 后台线程。"""

    def __init__(self, cfg: dict, jid: str | None = None):
        self.id = jid or uuid.uuid4().hex[:12]
        # —— 查询配置 ——
        self.from_name   = (cfg.get("from") or "").strip()
        self.to_name     = (cfg.get("to") or "").strip()
        self.dates       = [d for d in (cfg.get("dates") or []) if d][:5]
        self.train_types = cfg.get("train_types") or []
        self.seat_types  = cfg.get("seat_types") or list(ticket.SEAT_INDEX.keys())
        self.train_names = cfg.get("train_names") or []
        self.extend      = max(0, min(int(cfg.get("extend", 1)), 5))
        self.interval    = max(15, min(int(cfg.get("interval", 30)), 3600))
        self.price_max   = cfg.get("price_max")
        self.with_price  = bool(cfg.get("with_price"))
        # —— 推送配置 ——（token 落盘加密，读取时若是密文则解密）
        self.channel     = (cfg.get("channel") or "").strip()
        self.token       = cryptobox.decrypt_str((cfg.get("token") or "").strip())
        # —— 运行态 ——
        self.status      = "stopped"     # running / stopped / error
        self.cycle       = 0
        self.last_check  = ""
        self.last_error  = ""
        self.last_result = []            # 最近一轮各日期的 {date, data}
        self.found_log   = []            # 命中记录 [{time, date, train, kind, detail}]
        self.notified    = set()         # 已推送的 key，避免重复打扰
        self.created     = cfg.get("created") or _now()

        self._stop = threading.Event()
        self._thread = None

    # ── 序列化（持久化用，剔除线程/事件）──
    def to_config(self) -> dict:
        return {
            "id": self.id, "from": self.from_name, "to": self.to_name,
            "dates": self.dates, "train_types": self.train_types,
            "seat_types": self.seat_types, "train_names": self.train_names,
            "extend": self.extend, "interval": self.interval,
            "price_max": self.price_max, "with_price": self.with_price,
            "channel": self.channel, "token": cryptobox.encrypt_str(self.token),
            "status": self.status, "created": self.created,
            "found_log": self.found_log[-_FOUND_LOG_MAX:],
            "notified": list(self.notified),
        }

    def summary(self) -> dict:
        route = f"{self.from_name} → {self.to_name}"
        return {
            "id": self.id, "route": route, "dates": self.dates,
            "seat_types": self.seat_types, "train_names": self.train_names,
            "extend": self.extend, "interval": self.interval,
            "price_max": self.price_max, "channel": self.channel,
            "status": self.status, "cycle": self.cycle,
            "last_check": self.last_check, "last_error": self.last_error,
            "found_count": len(self.found_log), "created": self.created,
        }

    def detail(self) -> dict:
        d = self.summary()
        d["results"] = self.last_result
        d["found_log"] = self.found_log[-_FOUND_LOG_MAX:][::-1]   # 最新在前
        return d

    # ── 线程控制 ──
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.status = "running"
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"monitor-{self.id}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.status = "stopped"

    # ── 主循环 ──
    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
                self.last_error = ""
            except Exception as e:                      # 单轮异常不杀线程
                self.status = "error"
                self.last_error = str(e)
            else:
                if not self._stop.is_set():
                    self.status = "running"
            self.last_check = _now()
            self.cycle += 1
            # 可被 stop() 立即唤醒；±20% 抖动，错开固定节拍降低风控识别
            jitter = self.interval * 0.2
            self._stop.wait(max(1.0, self.interval + random.uniform(-jitter, jitter)))

    def _tick(self):
        """查一轮所有日期，检测新出现的有票/买长乘短并推送。"""
        results = []
        for date in self.dates:
            data = ticket.search(
                from_name=self.from_name, to_name=self.to_name, date=date,
                train_types=self.train_types, seat_types=self.seat_types,
                train_names=self.train_names, extend=self.extend,
                max_extend_queries=_EXTEND_BUDGET(self.extend),
                with_price=self.with_price, price_max=self.price_max,
            )
            results.append({"date": date, "data": data})
        self.last_result = results

        fresh = self._collect_fresh(results)
        if fresh and self.channel and self.token:
            self._push(fresh)

    def _collect_fresh(self, results: list) -> list:
        """与网页 checkAndNotify 同款：找出本轮「新出现」的有票/买长乘短车次。

        key = date|train_name|kind；票没了从 notified 移除，便于再次有票时再提醒。
        """
        avail_keys, fresh = set(), []
        for item in results:
            date, data = item["date"], item["data"]
            if not data or not data.get("ok"):
                continue
            for t in data.get("trains", []):
                has_alt = bool(t.get("alternatives"))
                if not (t.get("has") or has_alt):
                    continue
                kind = "直达有票" if t.get("has") else f"买长乘短 {len(t['alternatives'])} 个方案"
                key = f"{date}|{t['train_name']}|{kind}"
                avail_keys.add(key)
                if key not in self.notified:
                    self.notified.add(key)
                    fresh.append({"date": date, "t": t, "kind": kind})
        # 清理已无票的，使其下次有票能再次提醒
        self.notified = {k for k in self.notified if k in avail_keys}
        return fresh

    def _push(self, fresh: list):
        """组装与网页一致的结构化卡片并推送微信。"""
        title = f"🎫 有票啦！{self.from_name}→{self.to_name}"
        items, lines = [], []
        for f in fresh:
            t, date, kind = f["t"], f["date"], f["kind"]
            self.found_log.append({
                "time": _now(), "date": date,
                "train": t["train_name"], "kind": kind,
                "detail": self._detail_text(t),
            })
            lines.append(f"{date} {t['train_name']} "
                         f"{t.get('from_time','')}→{t.get('to_time','')} {kind}")
            items.append(self._to_item(t, date))
        del self.found_log[:-_FOUND_LOG_MAX]   # 截断

        body = "\n".join(lines[:8])
        url = items[0].get("book_url") or items[0].get("hb_url") or ""
        try:
            notify.push_message(self.channel, self.token, title, body, url,
                                items=items[:8])
        except Exception as e:
            self.last_error = f"推送失败：{e}"

    # ── 辅助：结构化车次 → notify 卡片 item ──
    def _to_item(self, t: dict, date: str) -> dict:
        link = ticket.book_url(t.get("from_name"), t.get("from_code"),
                               t.get("to_name"), t.get("to_code"), date)
        return {
            "train_name": t.get("train_name"), "date": date,
            "from_name": t.get("from_name"), "from_time": t.get("from_time"),
            "to_name": t.get("to_name"), "to_time": t.get("to_time"),
            "duration": t.get("duration"),
            "has": bool(t.get("has")),
            "avail": [{"type": s["type"], "count": s.get("count")}
                      for s in (t.get("avail") or [])] if t.get("has") else [],
            "book_url": link if t.get("has") else "",
            "hb_url": link if not t.get("has") else "",
            "alternatives": [{
                "label": a.get("label"),
                "from_name": a.get("from_name"), "from_time": a.get("from_time"),
                "to_name": a.get("to_name"), "to_time": a.get("to_time"),
                "avail": [{"type": s["type"], "count": s.get("count")}
                          for s in (a.get("avail") or [])],
                "url": ticket.book_url(a.get("from_name"), a.get("from_code"),
                                       a.get("to_name"), a.get("to_code"), date),
            } for a in (t.get("alternatives") or [])],
        }

    @staticmethod
    def _detail_text(t: dict) -> str:
        if t.get("has"):
            return " ".join(f"{s['type']}{s.get('count','')}"
                            for s in (t.get("avail") or []))
        return " / ".join(a.get("label", "") for a in (t.get("alternatives") or []))


class MonitorManager:
    """进程内监控任务管理器（单例，线程安全）。"""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        # 监控只在 create/stop/delete 时落盘（低频），用共享 store 拿到原子写 + atexit 兜底
        self._store = DebouncedJsonStore(_JOBS_FILE, self._serialize)
        self._load()

    # ── 对外 API ──
    def create(self, cfg: dict) -> Job:
        job = Job(cfg)
        with self._lock:
            self._jobs[job.id] = job
        job.start()
        self._save()
        return job

    def list(self) -> list:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.summary() for j in jobs]

    def get(self, jid: str) -> Job | None:
        with self._lock:
            return self._jobs.get(jid)

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
    def _serialize(self) -> list:
        with self._lock:
            return [j.to_config() for j in self._jobs.values()]

    def _save(self):
        # create/stop/delete 都期望立即可见，直接同步刷盘（原子写）
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
            job = Job(cfg, jid=cfg.get("id"))
            job.found_log = cfg.get("found_log") or []
            job.notified = set(cfg.get("notified") or [])
            self._jobs[job.id] = job
            if cfg.get("status") == "running":   # 重启后自动恢复
                job.start()


# 进程内单例
MANAGER = MonitorManager()
