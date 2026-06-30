#!/usr/bin/env python3
"""
12306 扫码登录 + 自动下单（占座）引擎

能力：
  1. 扫码登录：手机 12306 App 扫码 → 拿到登录态 Cookie，本地持久化（重启免扫）
  2. 查乘车人：拉取账号下已添加的乘客（含 passenger_id 等下单所需字段）
  3. 自动占座：submitOrderRequest → checkOrderInfo → getQueueCount
              → confirmSingleForQueue → queryOrderWaitTime
     抢到后订单进入「待支付」，付款仍需到 12306 App / 官网完成。

设计要点：
  - 与查票的匿名 SESSION 隔离：登录态用独立的 LoginSession（自己的 cookie jar），
    避免污染 ticket.SESSION，也避免被查票的高频请求影响登录态。
  - 仅做「占座」，不碰支付：合规边界与原项目一致（发现有票→占到座→人工付款）。

注意：12306 接口未公开、会变、有风控。本模块尽量贴近官方网页端调用顺序，
失败时返回 (False, 原因)，由上层决定重试/换席别/推送通知。
"""

import os
import re
import json
import time
import threading
from datetime import datetime
from urllib.parse import unquote

import requests

import ticket   # 复用站点字典 / 余票查询 / secretStr 抓取
import cryptobox  # 敏感数据（登录 Cookie）加密落盘


_SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "login_session.json")
_LOGIN_DEBUG_MAX = 80
_LOGIN_DEBUG_LOG: list[str] = []

_BASE = "https://kyfw.12306.cn"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 席别中文名 → 下单接口要的 seatType 单字母代码
SEAT_TYPE_CODE = {
    "商务座":   "9",
    "一等座":   "M",
    "二等座":   "O",
    "高级软卧": "6",
    "软卧":     "4",
    "动卧":     "F",
    "硬卧":     "3",
    "软座":     "2",
    "硬座":     "1",
    "无座":     "1",   # 无座与硬座同价同 seatType，提交时按硬座席位处理
}


def _dump_cookiejar(cookiejar) -> list[dict]:
    out = []
    for c in cookiejar:
        out.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
        })
    return out


def _load_cookies(cookiejar, cookies) -> None:
    cookiejar.clear()
    if isinstance(cookies, dict):
        for name, value in cookies.items():
            cookiejar.set(name, value, domain="kyfw.12306.cn", path="/")
        return
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not name:
            continue
        cookiejar.set(
            name,
            c.get("value", ""),
            domain=c.get("domain") or "kyfw.12306.cn",
            path=c.get("path") or "/",
        )


# ──────────────────────────────────────────
# 登录态会话（独立 cookie jar，与查票隔离）
# ──────────────────────────────────────────

class LoginSession:
    """封装一个 12306 登录态：扫码、保存/恢复 Cookie、判断是否在线。"""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": _UA,
            "Referer": f"{_BASE}/otn/resources/login.html",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            # 12306 的 passport 接口只对「像 AJAX 的请求」返回 JSON，
            # 缺这个头时 uamtk 等会返回空 body，导致 r.json() 报 char 0。
            "X-Requested-With": "XMLHttpRequest",
        })
        self._lock = threading.Lock()
        self.username = ""           # 登录成功后的用户名（来自 uamtk）
        self.logged_in = False
        self._qr_uuid = ""           # 当前二维码的会话标识
        # check_online() 的短 TTL 缓存：避免前端轮询 / 每轮 tick 都打网络
        self._online_cache_val = False
        self._online_cache_at = 0.0
        self._restore()

    # ── Cookie 持久化（敏感，整体加密落盘）──
    def _save(self):
        try:
            payload = json.dumps({
                "cookies": _dump_cookiejar(self.s.cookies),
                "username": self.username,
            }, ensure_ascii=False)
            cryptobox.warn_if_plaintext("12306 登录 Cookie")
            # 能加密则写 {"enc": "<密文>"}；否则降级明文（chmod 0600）
            if cryptobox.available():
                out = {"enc": cryptobox.encrypt_str(payload)}
            else:
                out = json.loads(payload)
            with open(_SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
            try:
                os.chmod(_SESSION_FILE, 0o600)
            except OSError:
                pass
        except OSError:
            pass

    def _restore(self):
        if not os.path.exists(_SESSION_FILE):
            return
        try:
            with open(_SESSION_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        legacy_plaintext = False
        if isinstance(raw, dict) and raw.get("enc"):
            dec = cryptobox.decrypt_str(raw["enc"])
            if not dec:                 # 缺钥/缺库/损坏：视为未登录
                return
            try:
                data = json.loads(dec)
            except json.JSONDecodeError:
                return
        else:
            data = raw                  # 旧明文格式（含 "cookies" 键）
            legacy_plaintext = True
        cookies = data.get("cookies") or {}
        if cookies:
            _load_cookies(self.s.cookies, cookies)
            self.username = data.get("username", "")
            # 旧明文 + 现在能加密：透明升级为加密落盘
            if legacy_plaintext and cryptobox.available():
                self._save()
            # 恢复后后台异步校验，避免阻塞进程启动（断网/慢网时同步探活会卡住
            # Flask 起服务）。初始保守置为未登录，后台探活成功后再翻转为 True；
            # 在此之前的调用方（_tick / api_order_create / submit_order）都会在
            # logged_in 为假时自行回退 check_online()，因此语义安全、可自愈。
            self.logged_in = False
            threading.Thread(target=self.check_online, kwargs={"force": True},
                             daemon=True, name="login-restore-probe").start()

    def clear(self):
        with self._lock:
            self.s.cookies.clear()
            self.username = ""
            self.logged_in = False
            self._qr_uuid = ""
            # 登出后让缓存立即反映「未登录」，不被 30s 内的旧 True 掩盖
            self._online_cache_val = False
            self._online_cache_at = time.monotonic()
        try:
            if os.path.exists(_SESSION_FILE):
                os.remove(_SESSION_FILE)
        except OSError:
            pass

    # ── 预热：访问登录页种基础 Cookie ──
    def _warm_login_page(self):
        try:
            # 关键：先访问 /otn/login/init 种下 JSESSIONID 会话 Cookie。
            # 扫码确认绑定的是「创建二维码时的这个会话」，缺它则扫码后
            # uamtk 拿不到登录态，会 302 到 error.html（表现为非 JSON）。
            self.s.get(f"{_BASE}/otn/login/init", timeout=10)
            self.s.get(f"{_BASE}/otn/resources/login.html", timeout=10)
            self._ensure_device_id()
            self.s.get(f"{_BASE}/passport/web/auth/uamtk-static",
                       data={"appid": "otn"}, timeout=10)
        except requests.RequestException:
            pass

    def _ensure_device_id(self):
        """获取并种下 RAIL_DEVICEID / RAIL_EXPIRATION 设备指纹 Cookie。

        缺这两个 Cookie 时，12306 的 passport 接口（uamtk 等）会把请求当成
        非法环境，直接返回登录页 HTML 而不是 JSON。best-effort：拿不到就算了。
        """
        if self.s.cookies.get("RAIL_DEVICEID"):
            return
        try:
            r = self.s.get(f"{_BASE}/otn/HttpZF/logdevice", timeout=10,
                           headers={"Referer": f"{_BASE}/otn/resources/login.html"})
            text = r.text or ""
        except requests.RequestException:
            return
        dfp = _first(r'"dfp"\s*:\s*"([^"]+)"', text)
        exp = _first(r'"exp"\s*:\s*"([^"]+)"', text)
        if dfp:
            self.s.cookies.set("RAIL_DEVICEID", dfp, domain="kyfw.12306.cn")
        if exp:
            self.s.cookies.set("RAIL_EXPIRATION", exp, domain="kyfw.12306.cn")

    # ──────────────────────────────────────
    # 扫码登录
    # ──────────────────────────────────────
    def create_qr(self) -> tuple[bool, str, str]:
        """生成登录二维码。

        返回 (ok, qr_image_data_uri, msg)。
        qr_image_data_uri 形如 'data:image/png;base64,...'，前端 <img src> 直接用。
        """
        with self._lock:
            self.s.cookies.clear()
            self.username = ""
            self.logged_in = False
            self._qr_uuid = ""
        self._warm_login_page()
        _login_debug(
            "device cookies",
            f"RAIL_DEVICEID:{len(self.s.cookies.get('RAIL_DEVICEID', '') or '')},"
            f"RAIL_EXPIRATION:{len(self.s.cookies.get('RAIL_EXPIRATION', '') or '')}"
        )
        _login_debug("cookies before qr", _cookie_shape(self.s.cookies))
        url = f"{_BASE}/passport/web/create-qr64"
        try:
            r = self.s.post(url, data={"appid": "otn"}, timeout=12)
            r.raise_for_status()
            j = r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            return False, "", f"获取二维码失败：{e}"

        # 成功响应：{"result_code":"0","uuid":"...","image":"<base64 png>"}
        if str(j.get("result_code")) != "0" or not j.get("image"):
            return False, "", j.get("result_message") or "12306 未返回二维码"
        with self._lock:
            self._qr_uuid = j.get("uuid", "")
        _login_debug("cookies after qr", _cookie_shape(self.s.cookies))
        data_uri = "data:image/png;base64," + j["image"]
        return True, data_uri, ""

    def check_qr(self) -> tuple[str, str]:
        """轮询扫码状态。

        返回 (state, msg)：
          state ∈ {"waiting"(待扫), "scanned"(已扫待确认),
                   "success"(已确认登录), "expired"(二维码过期), "error"}
        """
        with self._lock:
            uuid = self._qr_uuid
        if not uuid:
            return "error", "请先获取二维码"

        url = f"{_BASE}/passport/web/checkqr"
        try:
            r = self.s.post(url, data={
                "RAIL_DEVICEID": self.s.cookies.get("RAIL_DEVICEID", ""),
                "RAIL_EXPIRATION": self.s.cookies.get("RAIL_EXPIRATION", ""),
                "uuid": uuid,
                "appid": "otn",
            }, timeout=12)
            r.raise_for_status()
            j = r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            return "error", f"查询扫码状态失败：{e}"

        # result_code: 0=待扫, 1=已扫待确认, 2=已确认, 3=过期
        code = str(j.get("result_code"))
        if code == "0":
            return "waiting", "请用 12306 App 扫码"
        if code == "1":
            return "scanned", "已扫码，请在手机上确认登录"
        if code == "2":
            # 已确认：用返回的 uamtk 走换票流程拿到登录态 Cookie
            _login_debug("checkqr confirmed", _payload_shape(j))
            _login_debug("cookies after checkqr", _cookie_shape(self.s.cookies))
            ok, msg = self._finish_login(j)
            return ("success", "登录成功") if ok else ("error", msg)
        if code == "3":
            return "expired", "二维码已过期，请重新获取"
        return "error", j.get("result_message") or "未知扫码状态"

    def _finish_login(self, qr_result: dict | None = None) -> tuple[bool, str]:
        """扫码确认后：uamtk → uamauthclient 换取登录态会话 Cookie。

        二维码登录成功时 checkqr 返回的是 passport 侧票据；还需要先调用
        auth/uamtk 换成 OTN 可用的 newapptk，再给 uamauthclient 建会话。
        """
        qr_token = _login_token(qr_result or {})

        ok, j, err = self._get_passport_jsonp(
            f"{_BASE}/passport/web/auth/uamtk", {"appid": "otn"})
        _login_debug("cookies after auth/uamtk", _cookie_shape(self.s.cookies))
        if not ok:
            _login_debug("auth/uamtk jsonp failed", err)
            ok, j, err = self._post_passport(
                f"{_BASE}/passport/web/auth/uamtk", {"appid": "otn"})
        if not ok:
            _login_debug("auth/uamtk post failed", err)
            # 回退：部分情况下 uamtk-static 才会吐 newapptk
            ok, j, err = self._post_passport(
                f"{_BASE}/passport/web/auth/uamtk-static", {"appid": "otn"})
        if not ok:
            if qr_token:
                ok2, msg2 = self._finish_with_token(qr_token, "checkqr")
                if ok2:
                    return True, msg2
                _login_debug("checkqr token fallback failed", msg2)
            _login_debug("uamtk-static failed", err)
            return False, (
                f"换取登录令牌失败：{err}"
                f"（checkqr字段：{_payload_shape(qr_result or {})}）"
            )

        _login_debug("uamtk response", _payload_shape(j))
        apptk = _login_token(j)
        if str(j.get("result_code")) != "0" or not apptk:
            msg = j.get("result_message") or "登录令牌无效，请重新扫码"
            _login_debug("uamtk invalid", msg)
            return False, f"{msg}（返回字段：{_payload_shape(j)}）"

        if not apptk:
            return False, (
                "12306 未返回有效登录令牌，请刷新二维码重新扫码"
                f"（checkqr字段：{_payload_shape(qr_result or {})}）"
            )

        return self._finish_with_token(apptk, "newapptk")

    def _finish_with_token(self, apptk: str, label: str) -> tuple[bool, str]:
        ok, j, err = self._post_passport(
            f"{_BASE}/otn/uamauthclient", {"tk": apptk})
        if not ok:
            _login_debug("uamauthclient failed", err)
            return False, f"建立登录会话失败：{err}"
        _login_debug(f"uamauthclient response via {label}", _payload_shape(j))
        if str(j.get("result_code")) != "0":
            msg = j.get("result_message") or "建立登录会话失败"
            _login_debug("uamauthclient invalid", msg)
            return False, f"{msg}（tk长度：{len(apptk)}，返回字段：{_payload_shape(j)}）"
        self.username = j.get("username", "") or self.username
        with self._lock:
            self.logged_in = True
            # 刚登录成功，缓存立即置 True，避免随后探活再打一次网络
            self._online_cache_val = True
            self._online_cache_at = time.monotonic()
        self._save()
        return True, "登录成功"

    def _post_passport(self, url: str, data: dict,
                       attempts: int = 3) -> tuple[bool, dict, str]:
        """POST 一个 12306 passport/otn JSON 接口，容忍偶发空 body 并重试。

        返回 (ok, parsed_json, err_text)。空 body / 非 JSON 视为可重试失败。
        """
        last = ""
        for i in range(attempts):
            try:
                r = self.s.post(url, data=data, timeout=12, allow_redirects=False)
            except requests.RequestException as e:
                last = str(e)
                time.sleep(0.6)
                continue
            if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                # 被重定向到 error.html 等：本质是会话未登录/失效
                loc = r.headers.get("Location", "")
                last = f"会话未登录或已失效（HTTP {r.status_code} -> {loc[:120]}）"
                time.sleep(0.6)
                continue
            body = (r.text or "").strip()
            if not body:                       # 空 body：换个节奏再试
                last = f"空响应（HTTP {r.status_code}）"
                time.sleep(0.6)
                continue
            try:
                return True, r.json(), ""
            except json.JSONDecodeError:
                pass
            # 可能是 JSONP：callbackFunction({...}) —— 抽出括号内 JSON 再解析
            wrapped = _first(r"\((\{.*\})\)", body)
            if wrapped:
                try:
                    return True, json.loads(wrapped), ""
                except json.JSONDecodeError:
                    pass
            # 返回了 HTML（多半被重定向到登录页/风控页）：带片段便于定位
            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()[:80]
            last = f"非 JSON 响应（HTTP {r.status_code}）：{snippet}"
            time.sleep(0.6)
        return False, {}, last

    def _get_passport_jsonp(self, url: str, params: dict,
                            attempts: int = 3) -> tuple[bool, dict, str]:
        """GET 一个 12306 JSONP 接口；官方扫码成功后 auth/uamtk 就是这条路。"""
        params = dict(params)
        params.setdefault("callback", "jQuery12306")
        last = ""
        for _ in range(attempts):
            old_xrw = self.s.headers.pop("X-Requested-With", None)
            try:
                r = self.s.get(url, params=params, timeout=12,
                               allow_redirects=False,
                               headers={
                                   "Referer": f"{_BASE}/otn/resources/login.html",
                               })
            except requests.RequestException as e:
                if old_xrw is not None:
                    self.s.headers["X-Requested-With"] = old_xrw
                last = str(e)
                time.sleep(0.6)
                continue
            finally:
                if old_xrw is not None:
                    self.s.headers["X-Requested-With"] = old_xrw
            if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location", "")
                last = f"会话未登录或已失效（HTTP {r.status_code} -> {loc[:120]}）"
                time.sleep(0.6)
                continue
            body = (r.text or "").strip()
            if not body:
                last = f"空响应（HTTP {r.status_code}）"
                time.sleep(0.6)
                continue
            wrapped = _first(r"\((\{.*\})\)", body)
            try:
                return True, json.loads(wrapped or body), ""
            except json.JSONDecodeError:
                snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()[:80]
                last = f"非 JSONP 响应（HTTP {r.status_code}）：{snippet}"
                time.sleep(0.6)
        return False, {}, last

    # ──────────────────────────────────────
    # 在线状态
    # ──────────────────────────────────────
    _ONLINE_TTL = 30.0   # 秒：探活结果缓存时长

    def check_online(self, force: bool = False) -> bool:
        """探测当前 Cookie 是否仍是登录态。

        带 ~30s TTL 缓存：前端轮询 / 每轮 tick 频繁调用时复用结果，避免高频打网络
        （也降低风控）。需要实时结果的场景（Cookie 导入、登录后校验）传 force=True。
        外部 session 失效最多 TTL 后才被察觉，可接受。
        """
        now = time.monotonic()
        if not force and (now - self._online_cache_at) < self._ONLINE_TTL:
            return self._online_cache_val
        try:
            r = self.s.post(f"{_BASE}/otn/login/checkUser",
                            data={"_json_att": ""}, timeout=10)
            r.raise_for_status()
            j = r.json()
        except (requests.RequestException, json.JSONDecodeError):
            # 网络抖动不污染缓存（不更新时间戳）：登录态以上次成功探测为准
            return self._online_cache_val if not force else False
        flag = bool((j.get("data") or {}).get("flag"))
        self.logged_in = flag
        self._online_cache_val = flag
        self._online_cache_at = now
        return flag

    def status(self) -> dict:
        return {"logged_in": self.logged_in, "username": self.username}

    # ──────────────────────────────────────
    # 乘车人
    # ──────────────────────────────────────
    def passengers(self) -> tuple[bool, list, str]:
        """拉取账号下的乘车人列表。返回 (ok, [passenger...], msg)。"""
        url = f"{_BASE}/otn/confirmPassenger/getPassengerDTOs"
        try:
            r = self.s.post(url, data={"_json_att": ""}, timeout=12)
            r.raise_for_status()
            j = r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            return False, [], f"读取乘车人失败：{e}"

        data = j.get("data") or {}
        if data.get("noLogin") or not self.logged_in:
            self.logged_in = False
            return False, [], "登录已失效，请重新扫码登录"
        normal = data.get("normal_passengers") or []
        out = []
        for p in normal:
            out.append({
                "name":            p.get("passenger_name", ""),
                "id_type_code":    p.get("passenger_id_type_code", "1"),
                "id_type_name":    p.get("passenger_id_type_name", "二代身份证"),
                "id_no":           p.get("passenger_id_no", ""),
                "passenger_type":  p.get("passenger_type", "1"),
                "mobile":          p.get("mobile_no", ""),
                # 下单 confirm 步骤需要的原始串
                "allEncStr":       p.get("allEncStr", ""),
                "id_no_mask":      _mask_id(p.get("passenger_id_no", "")),
            })
        return True, out, ""

    # ──────────────────────────────────────
    # 下单（占座）流程
    # ──────────────────────────────────────
    def submit_order(self, *, secret_str: str, train_date: str,
                     from_name: str, to_name: str,
                     seat_type_name: str, passengers: list) -> tuple[bool, str]:
        """完整走一遍占座流程。passengers 为 self.passengers() 返回的元素列表。

        返回 (ok, msg)。ok=True 表示订单已提交进入待支付/排队成功。
        """
        if not self.logged_in:
            return False, "未登录"
        if not secret_str:
            return False, "缺少车次 secretStr（余票数据过期，需重查）"
        if not passengers:
            return False, "未选择乘车人"

        seat_code = SEAT_TYPE_CODE.get(seat_type_name)
        if not seat_code:
            return False, f"不支持的席别：{seat_type_name}"

        with self._lock:    # 同一登录态的下单流程串行，避免 token 互相覆盖
            return self._submit_locked(
                secret_str, train_date, from_name, to_name,
                seat_code, seat_type_name, passengers)

    def _submit_locked(self, secret_str, train_date, from_name, to_name,
                       seat_code, seat_type_name, passengers) -> tuple[bool, str]:
        # secretStr 在余票数据里是 URL 编码过的，提交前需解码
        secret = unquote(secret_str)
        left_ticket_referer = f"{_BASE}/otn/leftTicket/init?linktypeid=dc"
        confirm_referer = f"{_BASE}/otn/confirmPassenger/initDc"

        try:
            self.s.get(
                f"{_BASE}/otn/leftTicket/init",
                params={"linktypeid": "dc"},
                headers=_ajax_headers(left_ticket_referer),
                timeout=10,
            )
        except requests.RequestException:
            pass

        # 1) submitOrderRequest —— 占下下单会话
        try:
            r = self.s.post(
                f"{_BASE}/otn/leftTicket/submitOrderRequest",
                headers=_ajax_headers(left_ticket_referer),
                data={
                    "secretStr": secret,
                    "train_date": train_date,
                    "back_train_date": train_date,
                    "tour_flag": "dc",
                    "purpose_codes": "ADULT",
                    "query_from_station_name": from_name,
                    "query_to_station_name": to_name,
                    "undefined": "",
                },
                timeout=15,
            )
            j, err = _json_or_error(r, "提交下单请求")
        except requests.RequestException as e:
            return False, f"提交下单请求失败：{e}"
        if err:
            return False, err
        if not j.get("status"):
            return False, _msg(j, "提交下单请求被拒绝（可能票已售罄或需重新登录）")

        # 2) initDc —— 进入确认页，解析隐藏表单参数
        order_ctx = self._init_dc(confirm_referer)
        if not order_ctx.get("token"):
            return False, "进入下单确认页失败（可能登录失效或风控）"

        # 3) checkOrderInfo —— 校验乘客与订单合法性
        passenger_ticket, old_passenger = self._build_passenger_strings(
            passengers, seat_code)
        ok, msg = self._check_order_info(order_ctx["token"], passenger_ticket,
                                         old_passenger)
        if not ok:
            return False, msg

        # 4) getQueueCount —— 查队列/余量
        ok, msg = self._get_queue_count(order_ctx, train_date, seat_code,
                                        from_name, to_name)
        if not ok:
            return False, msg

        # 5) confirmSingleForQueue —— 真正排队占座
        ok, msg = self._confirm_queue(order_ctx, passenger_ticket, old_passenger)
        if not ok:
            return False, msg

        # 6) queryOrderWaitTime —— 等待出票结果（拿订单号）
        return self._wait_order(order_ctx["token"])

    # ── initDc：拉确认页，正则抠出 globalRepeatSubmitToken / key 等 ──
    def _init_dc(self, referer: str = "") -> dict:
        try:
            r = self.s.post(f"{_BASE}/otn/confirmPassenger/initDc",
                            headers=_ajax_headers(referer or
                                                  f"{_BASE}/otn/leftTicket/init?linktypeid=dc"),
                            data={"_json_att": ""}, timeout=15)
            r.raise_for_status()
            html = r.text
        except requests.RequestException:
            return {}

        token = _first(r"globalRepeatSubmitToken\s*=\s*'([^']+)'", html)
        return {
            "token": token,
            "key_check": _js_field(html, "key_check_isChange"),
            "left_ticket": _js_field(html, "leftTicketStr"),
            "train_no": _js_field(html, "train_no"),
            "station_train_code": _js_field(
                html, "station_train_code", "stationTrainCode"),
            "train_location": _js_field(html, "train_location"),
            "from_station_telecode": _js_field(
                html, "from_station_telecode", "fromStationTelecode"),
            "to_station_telecode": _js_field(
                html, "to_station_telecode", "toStationTelecode"),
        }

    def _build_passenger_strings(self, passengers, seat_code):
        """构造 passengerTicketStr / oldPassengerStr（12306 下单核心拼接串）。

        passengerTicketStr 单人格式：
          seatType,0,票种,姓名,证件类型,证件号,手机,N,allEncStr
        oldPassengerStr 单人格式：
          姓名,证件类型,证件号,票种_  （末尾下划线）
        """
        pt, op = [], []
        for p in passengers:
            ticket_type = p.get("passenger_type", "1")   # 1成人
            pt.append(",".join([
                seat_code, "0", ticket_type, p["name"],
                p.get("id_type_code", "1"), p.get("id_no", ""),
                p.get("mobile", ""), "N", p.get("allEncStr", ""),
            ]))
            op.append(",".join([
                p["name"], p.get("id_type_code", "1"),
                p.get("id_no", ""), ticket_type,
            ]) + "_")
        return "_".join(pt), "".join(op)

    def _check_order_info(self, token, passenger_ticket, old_passenger):
        try:
            r = self.s.post(
                f"{_BASE}/otn/confirmPassenger/checkOrderInfo",
                headers=_ajax_headers(f"{_BASE}/otn/confirmPassenger/initDc"),
                data={
                    "cancel_flag": "2",
                    "bed_level_order_num": "000000000000000000000000000000",
                    "passengerTicketStr": passenger_ticket,
                    "oldPassengerStr": old_passenger,
                    "tour_flag": "dc",
                    "randCode": "",
                    "whatsSelect": "1",
                    "_json_att": "",
                    "REPEAT_SUBMIT_TOKEN": token,
                },
                timeout=15,
            )
            j, err = _json_or_error(r, "校验订单")
        except requests.RequestException as e:
            return False, f"校验订单失败：{e}"
        if err:
            return False, err
        data = j.get("data") or {}
        if not j.get("status") or not data.get("submitStatus"):
            return False, _msg(j, data.get("errMsg") or "订单校验未通过")
        return True, ""

    def _get_queue_count(self, order_ctx, train_date, seat_code,
                         from_name, to_name):
        from_code = (order_ctx.get("from_station_telecode") or
                     ticket.code_of(from_name) or from_name)
        to_code = (order_ctx.get("to_station_telecode") or
                   ticket.code_of(to_name) or to_name)
        token = order_ctx.get("token", "")
        try:
            r = self.s.post(
                f"{_BASE}/otn/confirmPassenger/getQueueCount",
                headers=_ajax_headers(f"{_BASE}/otn/confirmPassenger/initDc"),
                data={
                    "train_date": _train_date_gmt8(train_date),
                    "train_no": order_ctx.get("train_no", ""),
                    "stationTrainCode": order_ctx.get("station_train_code", ""),
                    "seatType": seat_code,
                    "fromStationTelecode": from_code,
                    "toStationTelecode": to_code,
                    "leftTicket": order_ctx.get("left_ticket", ""),
                    "purpose_codes": "00",
                    "train_location": order_ctx.get("train_location", ""),
                    "_json_att": "",
                    "REPEAT_SUBMIT_TOKEN": token,
                },
                timeout=15,
            )
            j, err = _json_or_error(r, "查询排队")
        except requests.RequestException as e:
            return False, f"查询排队失败：{e}"
        if err:
            return False, err
        if not j.get("status"):
            return False, _msg(j, "查询排队人数失败")
        data = j.get("data") or {}
        # ticket 字段形如 "10,0"：剩余 / 候补；为 0 视为无票
        if str(data.get("op_2")) == "true":
            return False, "前方排队人数过多，暂未抢到"
        return True, ""

    def _confirm_queue(self, order_ctx, passenger_ticket, old_passenger):
        token = order_ctx.get("token", "")
        try:
            r = self.s.post(
                f"{_BASE}/otn/confirmPassenger/confirmSingleForQueue",
                headers=_ajax_headers(f"{_BASE}/otn/confirmPassenger/initDc"),
                data={
                    "passengerTicketStr": passenger_ticket,
                    "oldPassengerStr": old_passenger,
                    "randCode": "",
                    "purpose_codes": "00",
                    "key_check_isChange": order_ctx.get("key_check", ""),
                    "leftTicketStr": order_ctx.get("left_ticket", ""),
                    "train_location": order_ctx.get("train_location", ""),
                    "choose_seats": "",
                    "seatDetailType": "000",
                    "whatsSelect": "1",
                    "roomType": "00",
                    "dwAll": "N",
                    "_json_att": "",
                    "REPEAT_SUBMIT_TOKEN": token,
                },
                timeout=15,
            )
            j, err = _json_or_error(r, "提交占座")
        except requests.RequestException as e:
            return False, f"提交占座失败：{e}"
        if err:
            return False, err
        data = j.get("data") or {}
        if not j.get("status") or not data.get("submitStatus"):
            return False, _msg(j, data.get("errMsg") or "占座提交未通过")
        return True, ""

    def _wait_order(self, token) -> tuple[bool, str]:
        """轮询出票结果，拿到订单号即成功。最多等约 20 秒。"""
        url = f"{_BASE}/otn/confirmPassenger/queryOrderWaitTime"
        for _ in range(12):
            try:
                r = self.s.get(url, params={
                    "random": str(int(time.time() * 1000)),
                    "tourFlag": "dc",
                    "_json_att": "",
                    "REPEAT_SUBMIT_TOKEN": token,
                }, headers=_ajax_headers(f"{_BASE}/otn/confirmPassenger/initDc"),
                   timeout=12)
                r.raise_for_status()
                j = r.json()
            except (requests.RequestException, json.JSONDecodeError):
                time.sleep(1.5)
                continue
            data = j.get("data") or {}
            order_id = data.get("orderId")
            if order_id:
                ok, extra = self._finalize_order(token, order_id)
                suffix = "" if ok else f"（最终确认未返回成功：{extra}）"
                return True, f"占座成功！订单号 {order_id}{suffix}，请尽快到 12306 App 付款"
            if data.get("errMsg"):
                return False, data["errMsg"]
            # waitTime>0 表示仍在排队，继续等
            time.sleep(1.5)
        return True, "已提交占座，出票结果未确认，请到 12306 App「未完成订单」查看"

    def _finalize_order(self, token, order_id) -> tuple[bool, str]:
        """订单号返回后调用最终确认接口；失败不抹掉已拿到的订单号。"""
        try:
            r = self.s.post(
                f"{_BASE}/otn/confirmPassenger/resultOrderForDcQueue",
                headers=_ajax_headers(f"{_BASE}/otn/confirmPassenger/initDc"),
                data={
                    "orderSequence_no": order_id,
                    "_json_att": "",
                    "REPEAT_SUBMIT_TOKEN": token,
                },
                timeout=12,
            )
            r.raise_for_status()
            j = r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            return False, str(e)
        data = j.get("data") or {}
        if not j.get("status") or data.get("submitStatus") is False:
            return False, _msg(j, data.get("errMsg") or "结果确认未通过")
        return True, ""


# ──────────────────────────────────────────
# 模块级单例（一个进程一个登录态）
# ──────────────────────────────────────────
LOGIN = LoginSession()


# ──────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────

def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else ""


def _js_field(text: str, *names: str) -> str:
    for name in names:
        pattern = rf"['\"]{re.escape(name)}['\"]\s*:\s*['\"]([^'\"]*)['\"]"
        value = _first(pattern, text)
        if value:
            return _unescape_js(value)
    return ""


def _unescape_js(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except (TypeError, json.JSONDecodeError):
        return value


def _ajax_headers(referer: str = "") -> dict:
    headers = {
        "Referer": referer or f"{_BASE}/otn/leftTicket/init?linktypeid=dc",
        "Origin": _BASE,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    return headers


def _json_or_error(resp: requests.Response, action: str) -> tuple[dict, str]:
    try:
        resp.raise_for_status()
    except requests.RequestException as e:
        return {}, f"{action}失败：{e}"
    try:
        return resp.json(), ""
    except json.JSONDecodeError:
        body = (resp.text or "").strip()
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            return {}, f"{action}返回跳转：HTTP {resp.status_code} -> {loc[:120]}"
        if not body:
            return {}, f"{action}返回空响应（HTTP {resp.status_code}）"
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        if len(snippet) > 160:
            snippet = snippet[:160] + "..."
        return {}, f"{action}返回非 JSON（HTTP {resp.status_code}）：{snippet}"


def _login_token(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("uamtk", "newapptk", "apptk", "tk"):
        token = _clean_token(payload.get(key))
        if token:
            return token
    for key in ("data", "result"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            token = _login_token(nested)
            if token:
                return token
    return ""


def _clean_token(value) -> str:
    token = str(value or "").strip()
    if token.lower() in ("", "null", "none", "undefined"):
        return ""
    # 真实 uamtk/newapptk 是较长票据；过滤 result_code 之类的短字段误匹配。
    if len(token) < 16:
        return ""
    return token


def _payload_shape(payload: dict) -> str:
    if not isinstance(payload, dict):
        return type(payload).__name__
    parts = []
    for key, value in payload.items():
        if key in ("uamtk", "newapptk", "apptk", "tk"):
            parts.append(f"{key}:len{len(str(value or '').strip())}")
        elif isinstance(value, dict):
            parts.append(f"{key}:{{{_payload_shape(value)}}}")
        else:
            parts.append(str(key))
    return ",".join(parts[:12]) or "empty"


def _cookie_shape(cookiejar) -> str:
    parts = []
    for c in cookiejar:
        parts.append(f"{c.name}:len{len(c.value or '')}@{c.domain}")
    return ",".join(parts[:20]) or "empty"


def _login_debug(stage: str, info) -> None:
    line = f"{time.strftime('%H:%M:%S')} {stage}: {info}"
    print(f"[12306-login] {line}", flush=True)
    _LOGIN_DEBUG_LOG.append(line)
    del _LOGIN_DEBUG_LOG[:-_LOGIN_DEBUG_MAX]


def login_debug_log() -> list:
    """返回最近的登录诊断日志（供 /api/order/login/status 回传到页面）。"""
    return list(_LOGIN_DEBUG_LOG)


def _train_date_gmt8(day: str) -> str:
    try:
        dt = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return day
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return (f"{weekdays[dt.weekday()]} {months[dt.month - 1]} "
            f"{dt.day:02d} {dt.year} 00:00:00 GMT+0800 (中国标准时间)")


def _msg(j: dict, default: str) -> str:
    msgs = j.get("messages") or j.get("message")
    if isinstance(msgs, list) and msgs:
        return str(msgs[0])
    if isinstance(msgs, str) and msgs:
        return msgs
    return default


def _mask_id(idno: str) -> str:
    idno = (idno or "").strip()
    if len(idno) <= 8:
        return idno
    return idno[:4] + "*" * (len(idno) - 8) + idno[-4:]
