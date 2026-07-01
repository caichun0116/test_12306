#!/usr/bin/env python3
"""
微信/消息推送转发

支持三种正规推送渠道（都不需要自动登录个人微信）：
  - pushplus   : PushPlus 推送加，推到个人微信，token = 你的 token
  - serverchan : Server酱 Turbo，推到个人微信，token = SendKey
  - wecom      : 企业微信群机器人，推到企业微信，token = 完整 webhook 网址

消息均支持带「可点击超链接」（url），手机上点击可跳转 12306 购票页。

card_html() / card_markdown()：把查询结果（items）渲染成与网页结果卡片一致的样式，
PushPlus 用 HTML（最还原），Server酱/企业微信用 Markdown（近似）。
"""

import os
import html as _html

import requests


# 站主接收：抢到票 / 监控发现有票时，除了通知访客自己，也给站主抄送一份。
# 凭证只存服务端环境变量，绝不下发前端。留空则不给站主发。
def owner_config() -> tuple[str, str]:
    """返回 (channel, token)；未配置则返回 ("", "")。"""
    return ((os.environ.get("OWNER_NOTIFY_CHANNEL") or "").strip(),
            (os.environ.get("OWNER_NOTIFY_TOKEN") or "").strip())


def push_to_owner(title: str, body: str, url: str = "",
                  items: list = None, skip_if=None) -> None:
    """给站主抄送一份（best-effort，失败不影响主流程）。

    skip_if 为 (channel, token) 时，若与站主配置相同则跳过，避免访客本人就是站主时重复推。
    """
    channel, token = owner_config()
    if not (channel and token):
        return
    if skip_if and skip_if[0] == channel and (skip_if[1] or "").strip() == token:
        return
    try:
        push_message(channel, token, title, body, url, items=items)
    except Exception:
        pass


def _esc(s) -> str:
    return _html.escape(str(s or ""))


# 买长乘短方案上限：与网页一致尽量全列；仅极端情况（如 20+）折叠，防止消息过长
MAX_ALTS = 12


def card_html(items: list) -> str:
    """把车次结果渲染成 HTML 卡片（PushPlus 用），样式对齐网页结果卡片。"""
    cards, last_date = [], None
    for it in items or []:
        date = _esc(it.get("date"))
        if date and date != last_date:
            cards.append(f'<div style="font-size:13px;color:#64748b;margin:4px 0 8px;">📅 {date}</div>')
            last_date = date
        name  = _esc(it.get("train_name"))
        route = (f'{_esc(it.get("from_name"))} {_esc(it.get("from_time"))} → '
                 f'{_esc(it.get("to_name"))} {_esc(it.get("to_time"))}')
        dur   = _esc(it.get("duration"))

        head = (f'<div style="font-size:16px;line-height:1.5;">'
                f'<b style="font-size:17px;">{name}</b>&nbsp;&nbsp;'
                f'<span style="color:#0f172a;">{route}</span>&nbsp;&nbsp;'
                f'<span style="color:#64748b;">历时 {dur}</span></div>')

        body = ""
        if it.get("has"):
            # 直达有票：绿色徽章 + 绿色座位标签 + 橙色下单按钮
            seats = "".join(
                f'<span style="display:inline-block;background:#dcfce7;color:#166534;'
                f'font-size:13px;padding:3px 10px;border-radius:6px;margin:4px 6px 0 0;">'
                f'{_esc(s.get("type"))} {_esc(s.get("count"))}</span>'
                for s in (it.get("avail") or [])
            )
            btn = ""
            if it.get("book_url"):
                btn = (f'<a href="{_esc(it["book_url"])}" style="display:inline-block;'
                       f'background:#f97316;color:#fff;text-decoration:none;font-size:13px;'
                       f'padding:4px 12px;border-radius:6px;margin-top:4px;">去 12306 下单 ↗</a>')
            badge = ('<span style="display:inline-block;background:#16a34a;color:#fff;'
                     'font-size:12px;padding:2px 8px;border-radius:20px;margin-left:8px;">直达有票</span>')
            head = head[:-6] + badge + "</div>"
            body += f'<div style="margin-top:6px;">{seats}&nbsp;{btn}</div>'
        else:
            # 直达无票：头部琥珀徽章（买长乘短 N 个方案）+ 灰字 + 青色候补按钮
            n_alt = len(it.get("alternatives") or [])
            if n_alt:
                badge = (f'<span style="display:inline-block;background:#fef3c7;color:#92400e;'
                         f'font-size:12px;padding:2px 8px;border-radius:20px;margin-left:8px;">'
                         f'买长乘短 {n_alt} 个方案</span>')
                head = head[:-6] + badge + "</div>"
            hb = ""
            if it.get("hb_url"):
                hb = (f'<a href="{_esc(it["hb_url"])}" style="display:inline-block;'
                      f'background:#0d9488;color:#fff;text-decoration:none;font-size:12px;'
                      f'padding:3px 10px;border-radius:6px;">去 12306 候补 ↗</a>')
            body += (f'<div style="margin-top:6px;color:#64748b;font-size:13px;">'
                     f'直达无票，可候补：&nbsp;{hb}</div>')

        # 买长乘短延伸方案（最多显示 MAX_ALTS 个，其余折叠）
        alts = it.get("alternatives") or []
        for a in alts[:MAX_ALTS]:
            label = (f'<span style="display:inline-block;background:#fff7ed;color:#9a3412;'
                     f'font-size:12px;padding:2px 8px;border-radius:6px;">{_esc(a.get("label"))}</span>')
            aroute = (f'<span style="color:#334155;font-weight:600;">'
                      f'{_esc(a.get("from_name"))} {_esc(a.get("from_time"))} → '
                      f'{_esc(a.get("to_name"))} {_esc(a.get("to_time"))}</span>')
            aseats = "".join(
                f'<span style="display:inline-block;background:#dcfce7;color:#166534;'
                f'font-size:13px;padding:3px 10px;border-radius:6px;">'
                f'{_esc(s.get("type"))} {_esc(s.get("count"))}</span>'
                for s in (a.get("avail") or [])
            )
            abtn = ""
            if a.get("url"):
                abtn = (f'<a href="{_esc(a["url"])}" style="display:inline-block;'
                        f'background:#f97316;color:#fff;text-decoration:none;font-size:12px;'
                        f'padding:3px 10px;border-radius:6px;">去 12306 下单 ↗</a>')
            body += (f'<div style="margin-top:8px;padding-top:8px;border-top:1px dashed #e2e8f0;'
                     f'line-height:2;">{label}&nbsp;{aroute}&nbsp;{aseats}&nbsp;{abtn}</div>')
        if len(alts) > MAX_ALTS:
            body += (f'<div style="margin-top:8px;color:#64748b;font-size:12px;">'
                     f'…另有 {len(alts) - MAX_ALTS} 个买长乘短方案，点下方链接到 12306 查看</div>')

        cards.append(
            f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;'
            f'padding:12px 14px;margin-bottom:12px;">{head}{body}</div>'
        )
    return "".join(cards)


def _seat_text(avail) -> str:
    """座位列表 → '软卧16 · 硬卧有 · 硬座有'。"""
    parts = []
    for s in (avail or []):
        c = str(s.get("count", "")).strip()
        parts.append(f'{s.get("type","")}{c}' if c and c != "有" else f'{s.get("type","")}有')
    return " · ".join(parts)


def card_markdown(items: list) -> str:
    """把车次结果渲染成 Markdown（Server酱/企业微信用），干净分块、按日期分组。"""
    blocks, last_date = [], None
    for it in items or []:
        date = it.get("date", "")
        if date and date != last_date:
            blocks.append(f'### 📅 {date}')
            last_date = date

        head = (f'**{it.get("train_name","")}**　'
                f'{it.get("from_name","")} {it.get("from_time","")} → '
                f'{it.get("to_name","")} {it.get("to_time","")}'
                f'　·　历时 {it.get("duration","")}')
        lines = [head]

        if it.get("has"):
            seats = _seat_text(it.get("avail"))
            link = f'　[下单 ↗]({it["book_url"]})' if it.get("book_url") else ""
            lines.append(f'🟢 有票：{seats}{link}')
        else:
            hb = f'[候补 ↗]({it["hb_url"]})' if it.get("hb_url") else "可候补"
            lines.append(f'⚪️ 直达无票 → {hb}')

        alts = it.get("alternatives") or []
        for a in alts[:MAX_ALTS]:
            seats = _seat_text(a.get("avail"))
            link = f'　[下单 ↗]({a["url"]})' if a.get("url") else ""
            lines.append(f'🟠 {a.get("label","")}　{a.get("from_name","")} {a.get("from_time","")} → '
                         f'{a.get("to_name","")} {a.get("to_time","")}')
            lines.append(f'　　{seats}{link}')
        if len(alts) > MAX_ALTS:
            lines.append(f'…另有 {len(alts) - MAX_ALTS} 个买长乘短方案')

        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def push_message(channel: str, token: str, title: str, body: str,
                 url: str = "", items: list = None) -> tuple[bool, str]:
    """返回 (是否成功, 提示信息)。

    items 非空时按渠道渲染成「结果卡片」样式（与网页一致）；否则退化为纯文本 body。
    url 非空时在消息里附可点击链接。
    """
    token = (token or "").strip()
    url = (url or "").strip()
    if not token:
        return False, "未配置 token / webhook"

    web_text = "👉 打开 12306 网页购票（线路日期已带好）"
    app_scheme = "cn.12306://"          # 12306 App 协议，仅能唤起到首页
    app_text = "📱 打开 12306 App"
    # 企业微信/微信内置浏览器会屏蔽 scheme 跳转，需先跳系统浏览器
    tip = "提示：在微信里若打不开 App，点右上角「···」→「在系统浏览器打开」，再点上面的链接。"
    # 卡片模式（items）已自带每趟车的下单/候补链接，页脚只留一条简洁入口，避免消息冗长
    compact = bool(items)

    try:
        if channel == "pushplus":
            # html 模板，链接可点击
            if items:
                content = card_html(items)
            else:
                content = body.replace("\n", "<br/>")
            if url and compact:
                content += f'<br/><a href="{url}">{web_text}</a>'
            elif url:
                content += (f'<br/><br/><a href="{url}">{web_text}</a>'
                            f'<br/><a href="{app_scheme}">{app_text}</a>'
                            f'<br/><br/><span style="color:#888;font-size:13px;">{tip}</span>')
            r = requests.post(
                "https://www.pushplus.plus/send",
                json={"token": token, "title": title, "content": content, "template": "html"},
                timeout=10,
            )
            j = r.json()
            return (j.get("code") == 200, j.get("msg") or "")

        if channel == "serverchan":
            # desp 支持 Markdown，用 [文字](链接) 形式
            desp = card_markdown(items) if items else body
            if url and compact:
                desp += f"\n\n[{web_text}]({url})"
            elif url:
                desp += (f"\n\n[{web_text}]({url})"
                         f"\n\n[{app_text}]({app_scheme})"
                         f"\n\n> {tip}")
            r = requests.post(
                f"https://sctapi.ftqq.com/{token}.send",
                data={"title": title, "desp": desp},
                timeout=10,
            )
            j = r.json()
            return (j.get("code") == 0, j.get("message") or "")

        if channel == "wecom":
            # 企业微信群机器人：用 markdown 类型，链接可点击
            inner = card_markdown(items) if items else body
            content = f"**{title}**\n{inner}"
            if url and compact:
                content += f"\n\n[{web_text}]({url})"
            elif url:
                content += (f"\n[{web_text}]({url})"
                            f"\n[{app_text}]({app_scheme})"
                            f"\n> {tip}")
            r = requests.post(
                token,  # token 就是完整 webhook 网址
                json={"msgtype": "markdown", "markdown": {"content": content}},
                timeout=10,
            )
            j = r.json()
            return (j.get("errcode") == 0, j.get("errmsg") or "")

        return False, f"未知推送渠道：{channel}"

    except requests.RequestException as e:
        return False, f"推送请求失败：{e}"
    except ValueError:
        return False, "推送服务返回异常（非 JSON）"