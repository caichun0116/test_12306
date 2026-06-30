#!/usr/bin/env python3
"""敏感数据落盘加密小工具。

用途：把登录 Cookie、推送 token 等敏感串加密后再写入 json，避免明文落盘。

设计：
  - 优先使用 cryptography 的 Fernet（AES-128-CBC + HMAC，带完整性校验）。
  - 密钥存本目录下 .secret.key（仅本机可读，0600）；丢失则旧密文不可解（视为
    未登录，重新扫码即可），不会让程序崩溃。
  - 没装 cryptography 时 available() 返回 False，调用方应回退「明文 + chmod 0600
    + 一次性告警」。绝不退回 XOR 之类无完整性、会泄露结构的「伪加密」。

API：
  available()          -> bool          能否真正加密（库可用且密钥可写）
  encrypt_str(s)       -> str           "enc:v1:<token>"；不可用时原样返回 s
  decrypt_str(s)       -> str           解密；非密文/解密失败原样返回或返回 ""
  is_encrypted(s)      -> bool          是否本工具产出的密文
"""

import os
import threading

_PREFIX = "enc:v1:"
_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret.key")
_lock = threading.Lock()
_fernet = None          # 懒加载的 Fernet 实例
_inited = False
_warned = False

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAVE_LIB = True
except Exception:                       # 库未安装
    Fernet = None
    InvalidToken = Exception
    _HAVE_LIB = False


def _load_or_create_key() -> bytes | None:
    """读取或首次生成密钥文件（0600）。失败返回 None。"""
    try:
        if os.path.exists(_KEY_FILE):
            try:
                os.chmod(_KEY_FILE, 0o600)
            except OSError:
                pass
            with open(_KEY_FILE, "rb") as f:
                return f.read().strip()
        key = Fernet.generate_key()
        # O_EXCL 防并发重复创建；权限 0600
        fd = os.open(_KEY_FILE, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        os.chmod(_KEY_FILE, 0o600)
        return key
    except FileExistsError:             # 竞态：另一个线程刚建好
        try:
            try:
                os.chmod(_KEY_FILE, 0o600)
            except OSError:
                pass
            with open(_KEY_FILE, "rb") as f:
                return f.read().strip()
        except OSError:
            return None
    except OSError:
        return None


def _get_fernet():
    global _fernet, _inited
    if _inited:
        return _fernet
    with _lock:
        if _inited:
            return _fernet
        _inited = True
        if not _HAVE_LIB:
            _fernet = None
            return None
        key = _load_or_create_key()
        if not key:
            _fernet = None
            return None
        try:
            _fernet = Fernet(key)
        except Exception:
            _fernet = None
        return _fernet


def available() -> bool:
    return _get_fernet() is not None


def is_encrypted(s) -> bool:
    return isinstance(s, str) and s.startswith(_PREFIX)


def encrypt_str(s: str) -> str:
    """加密字符串；不可用时原样返回（调用方据此决定是否告警）。"""
    if not s:
        return s
    if is_encrypted(s):
        return s
    f = _get_fernet()
    if not f:
        return s
    try:
        token = f.encrypt(s.encode("utf-8")).decode("ascii")
        return _PREFIX + token
    except Exception:
        return s


def decrypt_str(s: str) -> str:
    """解密；非本工具密文则原样返回；解密失败返回 ""（视为失效）。"""
    if not is_encrypted(s):
        return s
    f = _get_fernet()
    if not f:
        return ""                       # 密文但无法解密（缺库/缺钥）：视为失效
    try:
        return f.decrypt(s[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):
        return ""


def warn_if_plaintext(what: str) -> None:
    """加密不可用时打一次告警，提示用户敏感数据将明文落盘。"""
    global _warned
    if available() or _warned:
        return
    _warned = True
    print(f"[cryptobox] ⚠️ 未安装 cryptography，{what} 将以明文落盘（已尽量 chmod 0600）。"
          f"建议 pip install cryptography 以加密敏感数据。", flush=True)
