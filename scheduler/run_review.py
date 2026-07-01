#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_review.py — A股复盘定时调度入口

被操作系统定时器(cron / systemd timer / Windows 任务计划)按固定时刻拉起,
本脚本自身完成:
  1. 按当前本地时间落入哪个时间窗, 选出对应的复盘技能(也可 --skill 强制指定)
  2. 判断当天是否为A股交易日(非交易日且窗口要求交易日 -> 直接跳过)
  3. 以无头模式调用 claude 执行该技能, 捕获其输出报告
  4. 通过 SMTP 把报告发送到配置的邮箱(失败可选发告警邮件)
  5. 全程写日志, 并清理过期日志

用法:
  python scheduler/run_review.py                  # 按当前时间自动选技能
  python scheduler/run_review.py --skill ashare-evening-review
  python scheduler/run_review.py --dry-run        # 只做判断与选择, 不调用claude/不发邮件
  python scheduler/run_review.py --config scheduler/config.yaml --no-email
"""

import argparse
import datetime as dt
import html
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# 以 `python scheduler/run_review.py` 方式启动时, sys.path[0] 是脚本所在目录
# (scheduler/) 而非项目根, 导致 `from scheduler.email_sender import ...` 找不到
# scheduler 包。这里先把项目根插入 sys.path, 使两种启动方式都可用:
#   python scheduler/run_review.py
#   python -m scheduler.run_review
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.email_sender import send_email

try:
    import yaml
except ImportError:
    sys.stderr.write("缺少依赖 PyYAML, 请先 pip install pyyaml\n")
    raise
WEEKDAY_ALIASES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# 复盘技能 → 数据看板模板前缀
SKILL_TO_DASHBOARD_PREFIX = {
    "ashare-morning-brief": "morning_brief",
    "ashare-intraday-review": "intraday_review",
    "ashare-evening-review": "evening_review",
    "ashare-weekly-review": "weekly_review",
}

_LOG_LINES = []


def log(msg):
    """同时写 stdout 与内存缓冲(供落盘和告警邮件复用)。"""
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    _LOG_LINES.append(line)


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"找不到配置文件 {path}\n"
            f"请先复制示例: cp scheduler/config.example.yaml scheduler/config.yaml"
        )
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def parse_hhmm(s: str) -> dt.time:
    h, m = s.strip().split(":")
    return dt.time(int(h), int(m))


def pick_window(windows: list, now: dt.datetime):
    """返回当前时间命中的第一个窗口 dict, 没有则 None。"""
    cur = now.time()
    wd = now.weekday()
    for w in windows:
        days = [WEEKDAY_ALIASES[d.lower()] for d in w.get("days", [])] if w.get("days") else list(range(7))
        if wd not in days:
            continue
        start = parse_hhmm(w["start"])
        end = parse_hhmm(w["end"])
        if start <= cur < end:
            return w
    return None


# ---------- 交易日判断 ----------
def is_trading_day(now: dt.datetime, cfg: dict) -> bool:
    """工作日判断: 排除周六周日。注意: 不识别法定节假日(会在节假日误发)。"""
    today = now.date()
    result = now.weekday() < 5
    log(f"工作日判断: {today} 周{now.weekday()+1} -> {'工作日' if result else '周末'}")
    return result


# ---------- 调用 claude 无头执行技能(通过 review-orchestrator agent 编排) ----------
def run_skill(skill: str, cfg: dict, dashboard_enabled: bool = False) -> str:
    """通过 review-orchestrator agent 调用 claude 执行技能, 返回报告正文; 失败抛 RuntimeError。

    review-orchestrator 相比直接调 Skill 多了三层自动接力:
      1. 复盘后自动检查宏观更新队列, 有新事件则调用 ashare-macro-context 消费落库
      2. 风险识别接力: 复盘后命中一票否决级红旗苗头等风险信号, 接力调用
         ashare-risk-assessment 对相关标的深挖; 周复盘时先做一次组合风险预跑再执行周复盘
      3. (周复盘时) 自动触发 ashare-macro-context 完整维护模式
    """
    ccfg = cfg.get("claude", {}) or {}
    if ccfg.get("prompt"):
        prompt = ccfg["prompt"].format(skill=skill)
    else:
        prompt = (
            f"请通过 review-orchestrator agent 执行 {skill} 技能。"
            f"要求：输出完整中文报告，自动接力宏观记忆更新"
        )
    if dashboard_enabled:
        prompt += (
            "，并调用 ashare-dashboard 将上述报告渲染为移动端数据看板 HTML 文件，"
            "保存到 output/ashare-dashboard/ 目录。"
        )

    # Windows 上 npm 全局安装的 claude 是 claude.cmd 垫片; subprocess 不带 shell=True
    # 时只按 .exe 查找会报 [WinError 2]。用 shutil.which 解析完整路径(PATHEXT 会命中
    # .cmd/.bat), 既跨平台, 又能在未安装时给出清晰提示。
    bin_name = ccfg.get("bin", "claude")
    claude_bin = shutil.which(bin_name)
    if claude_bin is None:
        raise RuntimeError(
            f"在 PATH 中找不到 claude 可执行文件 '{bin_name}'。"
            f"请确认 Claude Code 已安装, 或在 config.yaml 的 claude.bin 填绝对路径。"
        )
    cmd = [claude_bin, "-p", prompt, "--output-format", "text"]

    perm = ccfg.get("permission_mode", "bypassPermissions")
    if perm == "bypassPermissions":
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd += ["--permission-mode", perm]
        allowed = ccfg.get("allowed_tools") or []
        if allowed:
            cmd += ["--allowedTools", ",".join(allowed)]

    if ccfg.get("model"):
        cmd += ["--model", ccfg["model"]]
    cmd += list(ccfg.get("extra_args") or [])

    timeout = int(ccfg.get("timeout_seconds", 1800))
    log(f"调用 claude 执行技能 {skill} (超时 {timeout}s): {' '.join(cmd[:3])} ...")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude 执行超时(>{timeout}s)")

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude 退出码 {proc.returncode}\nstderr:\n{(proc.stderr or '').strip()[:2000]}"
        )
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("claude 输出为空")
    log(f"技能执行完成, 报告长度 {len(out)} 字符")
    return out


# ---------- 数据看板 HTML 文件查找 ----------
def find_dashboard_html(skill: str, date_str: str) -> Optional[Path]:
    """在 output/ashare-dashboard/ 中查找指定技能和日期生成的 HTML 文件。

    Args:
        skill: 复盘技能名, 用于匹配模板前缀 (如 ashare-evening-review → evening_review)
        date_str: 日期字符串 YYYY-MM-DD

    Returns:
        匹配的最新 HTML 文件路径, 未找到则返回 None
    """
    dashboard_dir = PROJECT_ROOT / "output" / "ashare-dashboard"
    if not dashboard_dir.is_dir():
        return None

    prefix = SKILL_TO_DASHBOARD_PREFIX.get(skill)
    if not prefix:
        return None

    pattern = f"{prefix}_dashboard_{date_str}"
    matches = sorted(
        [f for f in dashboard_dir.glob("*.html") if f.stem.startswith(pattern)],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if matches:
        log(f"找到数据看板文件: {matches[0]}")
        return matches[0]
    log(f"未找到匹配的数据看板文件 (prefix={prefix}, date={date_str})")
    return None


# ---------- 风险报告追加(看板邮件用) ----------
def render_risk_appendix_html(date_str: str) -> str:
    """读取当天 output/ashare-risk-assessment/ 下的风险报告, 渲染为可追加到看板邮件正文末尾的 HTML 片段。

    风险报告由 ashare-risk-assessment 技能(经编排层风险接力/周复盘预跑触发)落盘,
    文件名形如 `<代码>_risk_[日期].md` 或 `risk_<对象>_[日期].md`, 均以 `_[日期].md` 结尾。
    本函数只读取磁盘文件并追加为邮件里的"风险识别"板块, 不改动任何技能逻辑;
    无匹配文件时返回空串(本次复盘未触发风险接力)。

    说明: 仅在看板 HTML 邮件分支追加——看板只渲染复盘报告正文、不含风险报告;
    纯文本邮件分支的正文取自编排层合并后的 stdout, 已含风险内容, 不再追加以免重复。
    """
    risk_dir = PROJECT_ROOT / "output" / "ashare-risk-assessment"
    if not risk_dir.is_dir():
        return ""
    matches = sorted(risk_dir.glob(f"*_{date_str}.md"))
    blocks = []
    for f in matches:
        try:
            content = f.read_text(encoding="utf-8").strip()
        except OSError as e:
            log(f"读取风险报告失败 {f.name}: {e}")
            continue
        if content:
            blocks.append((f.stem, content))
    if not blocks:
        return ""
    log(f"向邮件追加 {len(blocks)} 份风险报告: {[name for name, _ in blocks]}")
    parts = [
        '<hr style="margin:24px 0;border:none;border-top:1px solid #444;">',
        '<section style="font-family:PingFang SC,Microsoft YaHei,sans-serif;padding:12px 0;">',
        '<h2 style="font-size:18px;color:#e8c46a;margin:0 0 12px;">🛡️ 风险识别报告（复盘风险接力触发，附加于本邮件）</h2>',
    ]
    for name, content in blocks:
        parts.append(
            f'<h3 style="font-size:14px;color:#9fb4c7;margin:12px 0 4px;">{html.escape(name)}</h3>'
            f'<pre style="white-space:pre-wrap;word-break:break-word;'
            f'background:#1e1e1e;color:#d4d4d4;padding:12px;border-radius:6px;'
            f'font-size:13px;line-height:1.5;margin:0 0 16px;">{html.escape(content)}</pre>'
        )
    parts.append('</section>')
    return "".join(parts)


# ---------- 日志落盘与清理 ----------
def flush_log(cfg: dict, skill: str):
    lcfg = cfg.get("log", {}) or {}
    log_dir = PROJECT_ROOT / lcfg.get("dir", "scheduler/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = log_dir / f"{stamp}_{skill or 'none'}.log"
    fname.write_text("\n".join(_LOG_LINES) + "\n", encoding="utf-8")

    keep_days = int(lcfg.get("keep_days", 30))
    if keep_days > 0:
        cutoff = dt.datetime.now() - dt.timedelta(days=keep_days)
        for old in log_dir.glob("*.log"):
            try:
                if dt.datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                    old.unlink()
            except OSError:
                pass


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(description="A股复盘定时调度入口")
    ap.add_argument("--config", default=str(PROJECT_ROOT / "scheduler" / "config.yaml"),
                    help="配置文件路径(默认 scheduler/config.yaml)")
    ap.add_argument("--skill", default=None, help="强制指定技能, 跳过时间窗判断")
    ap.add_argument("--dry-run", action="store_true",
                    help="只做技能选择与交易日判断, 不调用claude、不发邮件")
    ap.add_argument("--no-email", action="store_true", help="本次不发邮件(仍调用claude)")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    now = dt.datetime.now()
    log(f"启动 run_review, 当前时间 {now.strftime('%Y-%m-%d %H:%M:%S')} 周{now.weekday()+1}")

    # 1. 选技能
    if args.skill:
        skill = args.skill
        window = next((w for w in cfg.get("windows", []) if w.get("skill") == skill), {})
        log(f"强制指定技能: {skill}")
    else:
        window = pick_window(cfg.get("windows", []), now)
        if not window:
            log("当前时间不在任何配置的时间窗内, 退出(非错误)。")
            flush_log(cfg, "none")
            return 0
        skill = window["skill"]
        log(f"命中时间窗 [{window['start']}-{window['end']}] -> 技能 {skill}")

    # 2. 交易日判断
    require_td = window.get("require_trading_day", True) if window else True
    if require_td and not is_trading_day(now, cfg):
        log(f"今天非A股交易日, 技能 {skill} 跳过(非错误)。")
        flush_log(cfg, skill)
        return 0

    if args.dry_run:
        log(f"[dry-run] 将执行技能 {skill} 并发邮件(此处跳过)。")
        flush_log(cfg, skill)
        return 0

    # 3. 执行 + 发邮件
    ecfg = cfg.get("email", {}) or {}
    dashboard_cfg = cfg.get("dashboard", {}) or {}
    dashboard_enabled = bool(dashboard_cfg.get("enabled", False))
    prefix = ecfg.get("subject_prefix", "[A股复盘]")
    date_str = now.strftime("%Y-%m-%d")
    try:
        report = run_skill(skill, cfg, dashboard_enabled=dashboard_enabled)
        if not args.no_email:
            if dashboard_enabled:
                html_path = find_dashboard_html(skill, date_str)
                if html_path:
                    html_body = html_path.read_text(encoding="utf-8") + render_risk_appendix_html(date_str)
                    send_email(ecfg, f"{prefix} {skill} {date_str}", html_body, log, body_type="html")
                else:
                    log("数据看板 HTML 文件未找到, 退回纯文本邮件")
                    send_email(ecfg, f"{prefix} {skill} {date_str}", report, log)
            else:
                send_email(ecfg, f"{prefix} {skill} {date_str}", report, log)
        flush_log(cfg, skill)
        return 0
    except Exception as e:
        log(f"执行失败: {e}")
        if not args.no_email and ecfg.get("send_on_failure", True):
            try:
                send_email(ecfg, f"{prefix} 失败告警 {skill} {date_str}",
                           f"技能 {skill} 执行失败:\n\n{e}\n\n--- 运行日志 ---\n" + "\n".join(_LOG_LINES), log)
            except Exception as e2:
                log(f"告警邮件也发送失败: {e2}")
        flush_log(cfg, skill)
        return 1


if __name__ == "__main__":
    sys.exit(main())
