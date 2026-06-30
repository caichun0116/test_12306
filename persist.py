#!/usr/bin/env python3
"""轻量「防抖 JSON 落盘」工具，供 order_service / monitor_service 共用。

问题：任务管理器原本每次状态/日志变化都全量重写整份 JSON。抢票任务高频写日志
时，磁盘 churn 很重。

方案：DebouncedJsonStore 用一个后台 daemon 线程合并写：
  - mark_dirty()  标记「有变化」，由后台线程在合并窗口（默认 ~1s）内合并成一次写盘。
  - flush_now()   立即同步写盘，用于终态（停止 / 完成 / 删除）与进程退出（atexit），
                  保证不丢关键状态。
落盘用「写临时文件 + os.replace」原子替换，避免写一半崩溃留下损坏文件。
"""

import os
import json
import time
import atexit
import threading


def write_json_atomic(path: str, data, *, indent: int | None = 2,
                      mode: int = 0o600) -> None:
    """以固定权限原子写 JSON，避免半写文件和默认 umask 泄露权限。"""
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class DebouncedJsonStore:
    def __init__(self, path: str, serialize, coalesce: float = 1.0):
        """
        path       目标 JSON 文件路径
        serialize  无参可调用，返回可 json.dump 的数据（通常在管理器锁内构造快照）
        coalesce   合并窗口秒数：mark_dirty 后最多等这么久再合并写一次
        """
        self._path = path
        self._serialize = serialize
        self._coalesce = max(0.05, coalesce)
        self._dirty = threading.Event()
        self._write_lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="persist-flush")
        self._thread.start()
        # 进程优雅退出时兜底刷一次（SIGKILL 无法捕获，属可接受取舍）
        atexit.register(self.flush_now)

    def mark_dirty(self):
        """标记有变化，交给后台线程合并写。"""
        self._dirty.set()

    def flush_now(self):
        """立即同步写盘（终态 / 退出用）。"""
        self._dirty.clear()
        self._write()

    # ── 内部 ──
    def _loop(self):
        while True:
            self._dirty.wait()
            # 合并窗口内的多次变化只触发一次写
            time.sleep(self._coalesce)
            self._dirty.clear()
            self._write()

    def _write(self):
        try:
            data = self._serialize()
        except Exception:
            return
        if data is None:
            return
        with self._write_lock:
            try:
                write_json_atomic(self._path, data, indent=2, mode=0o600)
            except OSError:
                pass
