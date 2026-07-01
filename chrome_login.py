#!/usr/bin/env python3
"""每访客一个真 Chrome 的多人扫码登录管理器。

为什么用真 Chrome：12306 扫码后「auth/uamtk → uamauthclient」换正式登录态的流程，
在纯 requests 会话里会被 302 打回（缺 _passport_session 等、设备指纹算法易碎）。
真 Chrome（macOS `open` 启动、非自动化、不被检测）会自己把这套换态流程跑完，
我们只需通过调试端口（CDP）把二维码取出来、把登录成品 cookie 抠回来。已用真账号
手动实测验证：真 Chrome 扫码 → checkUser 在线、能拉乘车人。

并发：每个浏览器会话（sid）占一个独立端口 + 独立 profile，上限 _MAX_SLOTS。
二维码只在登录阶段需要；登录成功后立刻关掉该 Chrome、释放端口，下单走轻量 requests。

CDP 调用复用项目已有的「node + WebSocket」方式，不新增 Python 依赖（需本机有
Node 与 Google Chrome —— 与原官方登录路径一致）。
"""

import os
import json
import time
import shutil
import threading
import subprocess
import urllib.error
import urllib.request

import order12306

_LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"
# 多用户专用端口段，避开旧官方登录单实例用的 9222，互不干扰
_PORT_BASE = 9322
_MAX_SLOTS = 3
_PORTS = [_PORT_BASE + i for i in range(_MAX_SLOTS)]
_PROFILE_PREFIX = "/tmp/qp-chrome-mu-"
_QR_TTL = 110          # 二维码有效期（秒），超过提示过期重取
_CHROME_APP = "Google Chrome"

# 返回某个 tab（账号登录 / 扫码登录）中心的视口坐标 {x,y}，供 CDP 派发真实鼠标点击。
# 关键：12306 的二维码加载只认「可信用户事件」(isTrusted=true)，JS 的 element.click()
# 产生的是 isTrusted=false，会被忽略→卡「加载中」。必须用 Input.dispatchMouseEvent
# 在真实坐标上点，等同人手点击。
_TAB_XY_JS_TMPL = r"""
(function(){
  var nodes = document.querySelectorAll('a,li,div,span,button,h2,h3');
  for (var i=0;i<nodes.length;i++){
    if ((nodes[i].textContent||'').trim() === '%s'){
      var r = nodes[i].getBoundingClientRect();
      if (r.width>0 && r.height>0) return JSON.stringify({x:r.left+r.width/2, y:r.top+r.height/2});
    }
  }
  return '';
})()
"""
_QR_TAB_XY_JS = _TAB_XY_JS_TMPL % "扫码登录"
_ACCOUNT_TAB_XY_JS = _TAB_XY_JS_TMPL % "账号登录"

# 读二维码图片的 base64 src（实测是 data:image/jpg;base64,...，长度很长）
_READ_QR_JS = r"""
(function(){
  var imgs = document.querySelectorAll('img');
  for (var i=0;i<imgs.length;i++){
    var src = imgs[i].getAttribute('src') || '';
    if (src.indexOf('data:image') === 0 && src.length > 800){
      return src;
    }
  }
  return '';
})()
"""

# node：用 CDP Input.dispatchMouseEvent 派发【真实点击】切换 tab（扫码→账号→扫码），
# 触发 12306 的二维码加载，然后只读轮询等二维码渲染出来。
_NODE_QR = r"""
const ws = new WebSocket(process.argv[1]);
const readJs = process.argv[2];
const qrXyJs = process.argv[3];
const accountXyJs = process.argv[4];
const DEADLINE = Date.now() + 34000;
let nextId = 10;
const pending = {};
function call(method, params){
  return new Promise((resolve) => {
    const id = nextId++;
    pending[id] = resolve;
    ws.send(JSON.stringify({id, method, params: params||{}}));
  });
}
async function evalJs(expr){
  const r = await call("Runtime.evaluate", {expression: expr, returnByValue: true});
  return (r && r.result && r.result.value);
}
async function realClick(xyJs){
  const s = await evalJs(xyJs);
  if (!s) return false;
  let p; try { p = JSON.parse(s); } catch(e){ return false; }
  await call("Input.dispatchMouseEvent", {type:"mouseMoved", x:p.x, y:p.y});
  await call("Input.dispatchMouseEvent", {type:"mousePressed", x:p.x, y:p.y, button:"left", clickCount:1});
  await call("Input.dispatchMouseEvent", {type:"mouseReleased", x:p.x, y:p.y, button:"left", clickCount:1});
  return true;
}
function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending[m.id]) { pending[m.id](m.result); delete pending[m.id]; }
};
ws.onopen = async () => {
  try {
    await call("Runtime.enable", {});
    await call("Page.enable", {});
    // 等 DOM 基本就绪
    for (let i=0;i<20;i++){
      const rs = await evalJs("document.readyState");
      if (rs === "interactive" || rs === "complete") break;
      await sleep(300);
    }
    await sleep(2500);   // 页面就绪后稳定一下，登录脚本挂好事件
    // 真实点击序列：扫码 tab → 账号 tab → 扫码 tab（结尾停在扫码 tab）
    async function loadQrViaSwitch(){
      await realClick(qrXyJs);      await sleep(700);
      await realClick(accountXyJs); await sleep(800);
      await realClick(qrXyJs);      await sleep(600);
    }
    await loadQrViaSwitch();
    let qr = await evalJs(readJs);
    let lastSwitch = Date.now();
    while (!(qr && String(qr).indexOf("data:image") === 0) && Date.now() < DEADLINE){
      if (Date.now() - lastSwitch > 7000){ await loadQrViaSwitch(); lastSwitch = Date.now(); }
      await sleep(700);
      qr = await evalJs(readJs);
    }
    console.log((qr && String(qr).indexOf("data:image") === 0) ? qr : "");
    ws.close();
  } catch(err){ console.error(String(err && err.message || err)); process.exit(2); }
};
ws.onerror = (err) => { console.error(String(err && err.message || err || "ws error")); process.exit(2); };
"""

# node：浏览器级 Storage.getCookies 抠全部 cookie（含 httpOnly），复用原官方登录脚本
_NODE_COOKIES = r"""
const ws = new WebSocket(process.argv[1]);
ws.onopen = () => ws.send(JSON.stringify({id:1, method:"Storage.getCookies", params:{}}));
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id !== 1) return;
  const cookies = (msg.result.cookies || []).filter(c => String(c.domain || "").includes("12306.cn"));
  console.log(JSON.stringify(cookies));
  ws.close();
};
ws.onerror = (err) => { console.error(String(err && err.message || err || "ws error")); process.exit(2); };
"""


def _read_json_url(url: str, timeout: float = 3):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _browser_ws(port: int) -> str:
    """浏览器级调试 WebSocket（用于 Storage.getCookies）。"""
    try:
        return _read_json_url(f"http://127.0.0.1:{port}/json/version").get(
            "webSocketDebuggerUrl", "")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return ""


def _page_ws(port: int) -> str:
    """找到 12306 页面目标的调试 WebSocket（用于 Runtime.evaluate）。"""
    try:
        targets = _read_json_url(f"http://127.0.0.1:{port}/json")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return ""
    pages = [t for t in targets if t.get("type") == "page"]
    for t in pages:
        if "kyfw.12306.cn" in (t.get("url") or ""):
            return t.get("webSocketDebuggerUrl", "")
    return pages[0].get("webSocketDebuggerUrl", "") if pages else ""


def _node(script: str, *args: str, timeout: float = 12) -> tuple[bool, str]:
    try:
        proc = subprocess.run(["node", "-e", script, *args],
                              capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if proc.returncode != 0:
        return False, (proc.stderr or "node error").strip()
    return True, (proc.stdout or "").strip()


class _Instance:
    def __init__(self, sid: str, port: int, profile: str):
        self.sid = sid
        self.port = port
        self.profile = profile
        self.created = time.monotonic()


class ChromeLoginManager:
    """多实例真 Chrome 登录管理器（单例，线程安全）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_sid: dict[str, _Instance] = {}
        self._used_ports: set[int] = set()

    @staticmethod
    def available() -> bool:
        """本机是否具备真 Chrome 登录能力（有 node + Chrome.app）。"""
        if shutil.which("node") is None:
            return False
        return os.path.isdir(f"/Applications/{_CHROME_APP}.app")

    # ── 对外 API ──
    def start(self, sid: str) -> tuple[bool, str, str]:
        """开（或重开）该会话的登录 Chrome，返回 (ok, qr_data_uri, msg)。"""
        if not self.available():
            return False, "", "本机未安装 Node 或 Chrome，无法使用扫码登录"
        # 已有实例：先回收，重新出一张新码
        self.drop(sid)
        with self._lock:
            free = next((p for p in _PORTS if p not in self._used_ports), None)
            if free is None:
                return False, "", "当前登录人数已满，请稍后再试"
            self._used_ports.add(free)
            profile = _PROFILE_PREFIX + sid[:12]
            inst = _Instance(sid, free, profile)
            self._by_sid[sid] = inst
        try:
            self._launch_chrome(inst)
            if not self._wait_ready(inst, deadline=12):
                raise RuntimeError("Chrome 启动超时")
            qr = self._fetch_qr(inst, attempts=8)
            if not qr:
                raise RuntimeError("未能取到二维码（请重试）")
            return True, qr, ""
        except Exception as e:
            self.drop(sid)
            return False, "", f"启动登录失败：{e}"

    def refresh(self, sid: str) -> tuple[bool, str, str]:
        """二维码过期/想换一张时重新获取。"""
        return self.start(sid)

    def poll(self, sid: str) -> tuple[str, str]:
        """轮询登录状态。返回 (state, msg)。
        state ∈ waiting / success / expired / error
        success 时已把成品 cookie 灌进该 sid 的 LoginSession 并关闭 Chrome。
        """
        with self._lock:
            inst = self._by_sid.get(sid)
        if inst is None:
            return "error", "请先获取二维码"
        cookies = self._read_cookies(inst)
        if cookies and self._looks_logged_in(cookies):
            login = order12306.REGISTRY.get_or_create(sid)
            order12306._load_cookies(login.s.cookies, cookies)
            if login.check_online(force=True):
                self.drop(sid)        # 登录完成，关 Chrome 释放端口
                return "success", "登录成功"
        if time.monotonic() - inst.created > _QR_TTL:
            return "expired", "二维码已过期，请点击重新获取"
        return "waiting", "请用 12306 App 扫码并确认"

    def drop(self, sid: str):
        """关闭并清理该会话的 Chrome（登出 / 空闲驱逐 / 登录成功后调用）。"""
        with self._lock:
            inst = self._by_sid.pop(sid, None)
            if inst:
                self._used_ports.discard(inst.port)
        if not inst:
            return
        # 杀掉以该 profile 启动的 Chrome（open -na 的进程命令行里带 user-data-dir）
        try:
            subprocess.run(["pkill", "-f", inst.profile],
                           capture_output=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.4)               # 等进程退出，再删 profile 目录
        shutil.rmtree(inst.profile, ignore_errors=True)

    # ── 内部 ──
    def _launch_chrome(self, inst: _Instance):
        subprocess.Popen([
            "open", "-na", _CHROME_APP, "--args",
            f"--remote-debugging-port={inst.port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={inst.profile}",
            "--no-first-run", "--no-default-browser-check", "--new-window",
            _LOGIN_URL,
        ])

    def _wait_ready(self, inst: _Instance, deadline: float) -> bool:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if _page_ws(inst.port):
                return True
            time.sleep(0.5)
        return False

    def _fetch_qr(self, inst: _Instance, attempts: int) -> str:
        # 等 page 目标出现，再交给 node（内部自轮询点 tab + 读码，约 24s 预算）。
        ws = ""
        for _ in range(attempts):
            ws = _page_ws(inst.port)
            if ws:
                break
            time.sleep(0.6)
        if not ws:
            return ""
        ok, out = _node(_NODE_QR, ws, _READ_QR_JS, _QR_TAB_XY_JS,
                        _ACCOUNT_TAB_XY_JS, timeout=42)
        return out if (ok and out.startswith("data:image")) else ""

    def _read_cookies(self, inst: _Instance) -> list:
        ws = _browser_ws(inst.port)
        if not ws:
            return []
        ok, out = _node(_NODE_COOKIES, ws)
        if not ok or not out:
            return []
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return []

    @staticmethod
    def _looks_logged_in(cookies: list) -> bool:
        names = {c.get("name") for c in cookies}
        # 登录成品票据出现即认为换态完成（再以 check_online 复核）
        return bool(names & {"tk", "uamtk", "uKey"})


MANAGER = ChromeLoginManager()
