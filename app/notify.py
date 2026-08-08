"""
通知：企业微信 / 自定义 Webhook / PushPlus。配置从 settings 表读取（运行期可改）。
从原 wecom_notify.py 迁移。
"""

from __future__ import annotations

import json
from datetime import datetime

import requests

from app.logging_conf import logger
from app.repository import get_setting


def _truncate(content: str, limit: int = 3800) -> str:
    if content is None:
        return ""
    content = str(content)
    if len(content) <= limit:
        return content
    return f"{content[: max(0, limit - 900)]}\n\n...(内容过长已截断)...\n\n{content[-800:]}"


def send_wecom_webhook(webhook_key: str, content: str, *, title: str | None = None, timeout: int = 10) -> bool:
    if not webhook_key:
        return False
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={webhook_key}"
    text = f"{title or '网易云运行日志'}\n\n{_truncate(content or '')}".strip()
    try:
        resp = requests.post(url, json={"msgtype": "text", "text": {"content": text}}, timeout=timeout)
        if resp.status_code != 200:
            return False
        data = resp.json() if resp.content else {}
        return isinstance(data, dict) and data.get("errcode", 0) == 0
    except Exception:
        return False

def send_pushplus(
    token: str,
    content: str,
    *,
    title: str | None = None,
    topic: str | None = None,
    timeout: int = 10,
) -> bool:
    """通过 PushPlus 官方 send 接口发送消息。"""
    if not token:
        return False
    payload = {
        "token": token,
        "title": title or "网易音乐人任务",
        "content": _truncate(content or ""),
        "template": "txt",
    }
    if topic:
        payload["topic"] = topic
    try:
        resp = requests.post("https://www.pushplus.plus/send", json=payload, timeout=timeout)
        if not 200 <= resp.status_code < 300:
            return False
        data = resp.json() if resp.content else {}
        return isinstance(data, dict) and str(data.get("code", "")) == "200"
    except Exception:
        return False


def _parse_headers(headers_text: str) -> dict[str, str]:
    if not headers_text:
        return {}
    try:
        h = json.loads(headers_text)
        if isinstance(h, dict):
            return {str(k): str(v) for k, v in h.items()}
    except Exception:
        pass
    out: dict[str, str] = {}
    for item in headers_text.split(";"):
        if ":" in item:
            k, v = item.split(":", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _render_template_value(value, title: str, content: str):
    """Render placeholders after JSON parsing so multiline text stays valid JSON."""
    if isinstance(value, str):
        return value.replace("${title}", title).replace("${content}", content)
    if isinstance(value, list):
        return [_render_template_value(item, title, content) for item in value]
    if isinstance(value, dict):
        return {key: _render_template_value(item, title, content) for key, item in value.items()}
    return value


def _render_body_template(body_tpl: str, title: str, content: str):
    try:
        template_data = json.loads(body_tpl)
    except (TypeError, json.JSONDecodeError):
        return body_tpl.replace("${title}", title).replace("${content}", content)
    return _render_template_value(template_data, title, content)


def send_custom_webhook(
    webhook_url: str,
    content: str,
    *,
    title: str | None = None,
    timeout: int = 10,
    event: str = "notification",
    extra: dict | None = None,
) -> bool:
    if not webhook_url:
        return False
    title_str = title or "网易音乐人任务"
    content_str = content or ""
    payload = {
        "event": event,
        "title": title_str,
        "content": content_str,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        payload["extra"] = extra

    method = (get_setting("custom_webhook_method", "POST") or "POST").upper()
    headers = _parse_headers(get_setting("custom_webhook_headers", "") or "")
    headers.setdefault("Content-Type", "application/json")

    body_tpl = get_setting("custom_webhook_body", "") or ""
    if body_tpl:
        body_data = _render_body_template(body_tpl, title_str, content_str)
    else:
        body_data = payload

    try:
        if method == "GET":
            resp = requests.get(
                webhook_url,
                params=body_data if isinstance(body_data, dict) else payload,
                headers=headers,
                timeout=timeout,
            )
        else:
            kwargs = {"json": body_data} if isinstance(body_data, dict) else {"data": body_data.encode()}
            resp = requests.request(method, webhook_url, headers=headers, timeout=timeout, **kwargs)
        return 200 <= resp.status_code < 300
    except Exception:
        return False


def send_configured_notification(
    content: str,
    *,
    title: str | None = None,
    timeout: int = 10,
    event: str = "notification",
    extra: dict | None = None,
) -> bool:
    """按设置选择通知渠道；auto 模式兼容旧版优先级。"""
    method = (get_setting("notification_method", "none") or "none").lower()
    custom_url = get_setting("custom_webhook_url", "") or ""
    wecom_key = get_setting("wecom_webhook_key", "") or ""
    pushplus_token = get_setting("pushplus_token", "") or ""
    try:
        if method == "none":
            return False
        if method == "custom":
            return send_custom_webhook(custom_url, content, title=title, timeout=timeout, event=event, extra=extra)
        if method == "wecom":
            return send_wecom_webhook(wecom_key, content, title=title, timeout=timeout)
        if method == "pushplus":
            return send_pushplus(
                pushplus_token,
                content,
                title=title,
                topic=get_setting("pushplus_topic", "") or "",
                timeout=timeout,
            )
        # auto：保留已有配置的行为，并允许仅配置 PushPlus 的新部署直接工作。
        if custom_url:
            return send_custom_webhook(custom_url, content, title=title, timeout=timeout, event=event, extra=extra)
        if pushplus_token:
            return send_pushplus(
                pushplus_token,
                content,
                title=title,
                topic=get_setting("pushplus_topic", "") or "",
                timeout=timeout,
            )
        if wecom_key:
            return send_wecom_webhook(wecom_key, content, title=title, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"发送通知失败：{e}")
    return False
