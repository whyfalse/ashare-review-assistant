#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发送看板邮件测试脚本 — 读取 output/ashare-dashboard/ 下第一个 HTML 文件并发送。"""

import os
import sys
import glob
import yaml
from datetime import datetime
from email_sender import send_email

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
DASHBOARD_DIR = os.path.join(PROJECT_ROOT, "output", "ashare-dashboard")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"[错误] 配置文件不存在: {CONFIG_PATH}")
        print("请从 config.example.yaml 复制 config.yaml 并填入实际的邮件配置")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def find_first_dashboard():
    """返回 output/ashare-dashboard/ 下第一个 .html 文件的路径。"""
    if not os.path.isdir(DASHBOARD_DIR):
        print(f"[错误] 看板目录不存在: {DASHBOARD_DIR}")
        sys.exit(1)

    files = sorted(glob.glob(os.path.join(DASHBOARD_DIR, "*.html")))
    if not files:
        print(f"[错误] 看板目录下没有 .html 文件: {DASHBOARD_DIR}")
        sys.exit(1)

    return files[0]


def main():
    cfg = load_config()
    email_cfg = cfg.get("email", {})

    filepath = find_first_dashboard()
    filename = os.path.basename(filepath)
    print(f"读取看板文件: {filepath}")

    with open(filepath, "r", encoding="utf-8") as f:
        html_body = f.read()

    subject_prefix = email_cfg.get("subject_prefix", "[A股复盘]")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"{subject_prefix}看板测试 {filename}"

    print("=" * 60)
    print(f"SMTP host: {email_cfg.get('smtp_host')}")
    print(f"收件人: {email_cfg.get('recipients')}")
    print(f"主题: {subject}")
    print(f"HTML 大小: {len(html_body)} 字符")
    print("=" * 60)

    try:
        send_email(email_cfg, subject, html_body, body_type="html")
        print("\n看板邮件发送成功!")
    except Exception as e:
        print(f"\n邮件发送失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
