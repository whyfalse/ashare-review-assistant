#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮件发送测试脚本 — 发送一封测试邮件验证 SMTP 配置是否正常。"""

import os
import sys
import yaml
from datetime import datetime
from email_sender import send_email

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"[错误] 配置文件不存在: {CONFIG_PATH}")
        print("请从 config.example.yaml 复制 config.yaml 并填入实际的邮件配置")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    cfg = load_config()
    email_cfg = cfg.get("email", {})

    subject_prefix = email_cfg.get("subject_prefix", "[A股复盘]")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"{subject_prefix}邮件发送测试 {now}"
    body = (
        f"这是一封来自 A股价值投资助手 的测试邮件。\n"
        f"发送时间: {now}\n\n"
        f"如果你收到了这封邮件，说明 SMTP 邮件配置正确，邮件发送功能正常工作。\n\n"
        f"---\nA-share Value Investment Assistant"
    )

    print("=" * 60)
    print(f"SMTPhost: {email_cfg.get('smtp_host')}")
    print(f"收件人: {email_cfg.get('recipients')}")
    print(f"主题: {subject}")
    print("=" * 60)

    try:
        send_email(email_cfg, subject, body)
        print("\n测试邮件发送成功!")
    except Exception as e:
        print(f"\n邮件发送失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
