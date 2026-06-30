#!/usr/bin/env python3
"""Import 12306 cookies exported from Chrome DevTools into login_session.json."""

import json
from pathlib import Path

import order12306


COOKIE_FILE = Path("/tmp/qp_12306_cookies.json")


def main() -> int:
    cookies = json.loads(COOKIE_FILE.read_text())
    login = order12306.LOGIN
    order12306._load_cookies(login.s.cookies, cookies)
    login.logged_in = login.check_online()
    login._save()
    print(json.dumps({
        "ok": login.logged_in,
        "cookie_count": len(cookies),
    }, ensure_ascii=False))
    return 0 if login.logged_in else 1


if __name__ == "__main__":
    raise SystemExit(main())
