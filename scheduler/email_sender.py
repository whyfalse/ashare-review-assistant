#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
email_sender.py — 邮件发送模块

通过 SMTP 发送邮件，支持 SSL / STARTTLS，认证信息中的 ${VAR} 从环境变量注入。
可从外部传入 log 回调以统一日志输出。
"""

import os
import re
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from typing import Callable, Optional, Literal


BodyType = Literal["plain", "html"]


def expand_env(value: str) -> str:
    """把字符串里的 ${VAR} 替换为环境变量值；非字符串原样返回。"""
    if not isinstance(value, str):
        return value
    return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), value)


def send_email(
    email_cfg: dict,
    subject: str,
    body: str,
    log: Optional[Callable] = None,
    body_type: BodyType = "plain",
):
    """通过 SMTP 发送邮件。

    Args:
        email_cfg: 邮件配置字典（即 cfg["email"] 子节点）。
        subject:   邮件主题。
        body:      邮件正文（纯文本或 HTML，由 body_type 决定）。
        log:       日志回调；默认使用 print。
        body_type: 正文类型，"plain" 纯文本（默认）或 "html"。
                   注意：HTML 正文在部分邮件客户端（163/QQ/Gmail webmail）
                   会被剥离 flex/grid/CSS 变量/渐变等样式，复杂看板建议改用附件。
    """
    if log is None:
        log = print

    if not email_cfg.get("enabled", False):
        log("邮件发送已禁用(email.enabled=false), 跳过")
        return

    host = email_cfg["smtp_host"]
    port = int(email_cfg["smtp_port"])
    use_ssl = bool(email_cfg.get("use_ssl", True))
    username = expand_env(email_cfg.get("username", ""))
    password = expand_env(email_cfg.get("password", ""))
    sender = expand_env(email_cfg.get("sender", username))
    recipients = [expand_env(r) for r in (email_cfg.get("recipients") or [])]
    if not recipients:
        raise RuntimeError("email.recipients 为空, 无法发送")

    msg = MIMEText(body, body_type, "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    log(f"发送邮件到 {recipients} via {host}:{port} (ssl={use_ssl})")
    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
        server.starttls()
    try:
        if username:
            server.login(username, password)
        server.sendmail(sender, recipients, msg.as_string())
    finally:
        server.quit()
    log("邮件发送成功")
