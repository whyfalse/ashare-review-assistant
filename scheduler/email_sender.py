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


# ---------- CSS 变量内联 ----------

def _parse_root_variables(html: str) -> dict[str, str]:
    """从 HTML 的 :root{} 块中提取所有 --name: value 变量定义。"""
    root_match = re.search(r':root\s*\{([^}]+)\}', html, re.DOTALL)
    if not root_match:
        return {}

    vars_dict: dict[str, str] = {}
    raw_decls = [d.strip() for d in root_match.group(1).split(';') if d.strip()]
    for decl in raw_decls:
        if ':' in decl:
            name, value = decl.split(':', 1)
            name = name.strip()
            if name.startswith('--'):
                vars_dict[name] = value.strip()
    return vars_dict


def _resolve_nested_vars(vars_dict: dict[str, str], max_depth: int = 5) -> dict[str, str]:
    """递归解析变量值中嵌套的 var() 引用（如 --x: var(--y)）。"""
    for _ in range(max_depth):
        changed = False
        for name, value in list(vars_dict.items()):
            new_value = re.sub(
                r'var\((--[\w-]+)\)',
                lambda m: vars_dict.get(m.group(1), m.group(0)),
                value,
            )
            if new_value != value:
                vars_dict[name] = new_value
                changed = True
        if not changed:
            break
    return vars_dict


def inline_css_variables(html: str) -> str:
    """将 HTML 中 :root{} 定义的 CSS 变量展开为具体值。

    邮件客户端 (163/QQ/Gmail) 会剥离或破坏 <style> 中的
    CSS 自定义属性，导致大量 var(--xxx) 引用失效、样式丢失。
    此函数在发送前将变量内联为具体值，消除对 CSS 变量的依赖。
    """
    vars_dict = _parse_root_variables(html)
    if not vars_dict:
        return html

    vars_dict = _resolve_nested_vars(vars_dict)

    def _resolve(match: re.Match) -> str:
        inner = match.group(1)
        # 处理 var(--name, fallback)：在括号深度为0处找第一个逗号
        depth = 0
        comma_at = -1
        for i, ch in enumerate(inner):
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif ch == ',' and depth == 0:
                comma_at = i
                break

        if comma_at > 0:
            var_name = inner[:comma_at].strip()
            fallback = inner[comma_at + 1:].strip()
        else:
            var_name = inner.strip()
            fallback = None

        if var_name in vars_dict:
            return vars_dict[var_name]
        return fallback if fallback is not None else match.group(0)

    return re.sub(r'var\(([^()]*(?:\([^()]*\)[^()]*)*)\)', _resolve, html)


# ---------- CSS 修复 ----------

def _sanitize_css(html: str) -> str:
    """修复 AI 生成 HTML 时可能引入的 CSS 笔误，并补回邮件客户端会丢失的关键样式。

    当前修复：
    1. scroll-be_hav_ior → scroll-behavior（AI 偶尔把 behavior 拆成 be_hav_ior）
    2. 若缺少 @media (min-width:480px) 则补回（AI 生成时可能遗漏）
    """
    # 1. 修复已知的 CSS 属性名笔误
    html = html.replace('scroll-be_hav_ior', 'scroll-behavior')

    # 2. 若 @media 块缺失，在 </style> 前补回
    if '@media' not in html:
        media_block = (
            '\n'
            '  @media (min-width:480px){\n'
            '    .kpi-row{grid-template-columns:repeat(3,1fr);}\n'
            '  }\n'
            '</style>'
        )
        html = html.replace('</style>', media_block)

    return html


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
                   HTML 正文在发送前会自动内联 CSS 变量，减少
                   邮件客户端剥离样式的影响。
    """
    if log is None:
        log = print

    if not email_cfg.get("enabled", False):
        log("邮件发送已禁用(email.enabled=false), 跳过")
        return

    if body_type == "html":
        body = inline_css_variables(body)
        log(f"CSS 变量已内联 (var() 剩余: {body.count('var(--')} 处)")
        had_media = '@media' in body
        had_corruption = 'scroll-be_hav_ior' in body
        body = _sanitize_css(body)
        fixes = []
        if not had_media:
            fixes.append('补回@media')
        if had_corruption:
            fixes.append('修复scroll-be_hav_ior')
        if fixes:
            log(f"CSS 修复: {', '.join(fixes)}")

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
