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
import re
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

# 复盘技能 → 落盘报告目录与文件名前缀(技能"输出保存"环节写盘的命名规则)
# 用于在无头 Claude 只回了一段摘要时, 回读技能落盘的完整报告作为邮件正文。
# 注: 各复盘技能(含 weekly-review)报告文件名里的日期均取运行当天,
# 但为兼容手动补跑等情形, 这里仍只按前缀 + 最近修改时间定位, 不假定日期。
SKILL_TO_REPORT = {
    "ashare-morning-brief": ("ashare-morning-brief", "morning_brief"),
    "ashare-intraday-review": ("ashare-intraday-review", "intraday_review"),
    "ashare-evening-review": ("ashare-evening-review", "evening_review"),
    "ashare-weekly-review": ("ashare-weekly-review", "weekly_review"),
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


# ---------- 调用 claude 无头执行(共用底座) ----------
def _run_claude(prompt: str, ccfg: dict, timeout: int, label: str) -> str:
    """以无头模式(`claude -p`)执行一段 prompt, 返回 stdout 文本; 失败抛 RuntimeError。

    run_skill(执行复盘技能) 与 ensure_dashboard(兜底渲染看板) 共用此底座,
    差别只在 prompt 与超时。
    """
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

    log(f"{label} (超时 {timeout}s): {' '.join(cmd[:3])} ...")

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
    return out


# ---------- 无头模式调用 review-orchestrator 的默认 prompt ----------
# 必须显式约束"同步前台执行 + 落盘 + 禁过渡话术"。否则主 Claude 常把"通过 agent
# 执行技能"误解为"后台启动后提前返回", 只回一段"已启动/正在执行/请稍候/完成后会
# 通知"过渡话术, 技能既未真正跑完、报告也不落盘, 邮件正文就成了一封废邮件。
# 该 prompt 已在 2026-07-05 的周复盘实测中验证可靠(完整报告落盘 + 看板生成); 同日
# 用短默认 prompt("请通过 review-orchestrator agent 执行 X 技能，输出完整中文报告")
# 多次实测均失败(过渡话术/未落盘)。config.yaml 的 claude.prompt 可整体覆盖。
_DEFAULT_SKILL_PROMPT = """请通过 review-orchestrator agent 执行 {skill} 技能，输出完整中文报告。

本 prompt 由定时调度器无头调用，无人工交互，以下要求务必遵守：
1. 必须以同步前台方式调用 review-orchestrator agent（等待 agent 完整返回后再继续），不得以后台方式启动后提前结束本轮。你的最终输出会被直接当作邮件正文，因此禁止只回复"已启动代理/正在执行/请稍候/完成后会通知"之类的过渡话术——那不是报告，会发出一封内容只有过渡话术的废邮件。
2. agent 返回后，把完整中文报告正文原样作为最终答复输出；若技能已把报告落盘，请回读落盘文件全文输出，不要只给摘要或文件路径。
3. 报告必须落盘到技能 SKILL.md 指定的 output/ 目录（bypassPermissions 模式下 Write 工具免确认，不得以"权限受限/无法保存"为由跳过落盘）。调度器优先用落盘报告作邮件正文，未落盘会被判定为未完成并发告警邮件。
4. 必须完整执行技能的全部模块（如周复盘的九大模块与技术引擎），不得因耗时/上下文/取数困难而偷工减料或降级为简略摘要。
5. 无头模式下所有编排接力（如周复盘的风险预跑、宏观完整维护）必须自动顺序完成，不得以"是否需要继续？/是否执行宏观维护？"等交互话术收尾等待确认——无头模式没有用户交互，此类话术会被判定为未完成。"""


# ---------- 调用 claude 无头执行技能(通过 review-orchestrator agent 编排) ----------
def run_skill(skill: str, cfg: dict, run_start_ts: float) -> str:
    """通过 review-orchestrator agent 调用 claude 执行技能, 返回 stdout 文本; 失败抛 RuntimeError。

    review-orchestrator 相比直接调 Skill 多了三层自动接力:
      1. 复盘后自动检查宏观更新队列, 有新事件则调用 ashare-macro-context 消费落库
      2. 风险识别接力: 复盘后命中一票否决级红旗苗头等风险信号, 接力调用
         ashare-risk-assessment 对相关标的深挖; 周复盘时先做一次组合风险预跑再执行周复盘
      3. (周复盘时) 自动触发 ashare-macro-context 完整维护模式

    无头模式下主 Claude 偶尔不等 agent 返回就回一段"已启动/请稍候"过渡话术(假完成),
    技能既未跑完、报告也不落盘。本函数对此重试: 以"本次运行期间是否落盘报告"为成功
    判据(各复盘技能 SKILL.md 均要求落盘), 未落盘且 stdout 过短即视为假完成, 最多重试
    max_attempts 次(默认 3)。重试仍失败则返回最后一次输出, 由 main() 的假完成兜底判
    失败发告警邮件, 不发过渡话术废邮件。

    注意: 无头模式下 review-orchestrator 往往只在 stdout 回一段简短摘要, 完整报告由
    技能落盘到 output/。本函数返回的 stdout 仅供日志参考, 邮件正文应优先取落盘报告
    (见 resolve_report_body)。

    看板渲染不在此函数内处理——统一由 ensure_dashboard() 在报告落盘后独立调用,
    避免编排链路过长导致看板步骤被截断。
    """
    ccfg = cfg.get("claude", {}) or {}
    prompt_template = ccfg.get("prompt") or _DEFAULT_SKILL_PROMPT
    prompt = prompt_template.format(skill=skill)
    # 看板渲染已从主 prompt 移除，统一由 ensure_dashboard() 兜底单独调用。
    # 原因是 review-orchestrator 在无头模式下的编排链路太长（数据源预检→风险预跑→
    # 复盘→宏观维护→看板），看板作为最后一步经常来不及完成就返回了，导致首轮
    # 无 HTML 产出。改为复盘+看板分离调用：主调用专注复盘，看板由兜底机制
    # 在报告落盘后独立渲染，每次调用更短更可靠。

    timeout = int(ccfg.get("timeout_seconds", 1800))
    max_attempts = max(1, int(ccfg.get("max_attempts", 3)))
    last_out = ""
    for attempt in range(1, max_attempts + 1):
        out = _run_claude(prompt, ccfg, timeout,
                          f"调用 claude 执行技能 {skill} (第{attempt}/{max_attempts}次)")
        last_out = out
        log(f"技能执行完成 (第{attempt}/{max_attempts}次), stdout 长度 {len(out)} 字符")
        # 成功判据: 技能 SKILL.md 要求落盘, 本次运行期间落盘了报告即视为真完成。
        if find_report_markdown(skill, since_ts=run_start_ts):
            return out
        # 未落盘但 stdout 足够长: 可能是完整报告只没落盘, 接受(交由 resolve_report_body 用 stdout)。
        if len(out) >= 2000:
            log(f"未落盘报告但 stdout 达 {len(out)} 字符, 视为完整报告(仅未落盘), 接受。")
            return out
        # 未落盘且 stdout 过短: 疑似假完成(主 Claude 未等 agent 返回即回过渡话术), 重试。
        if attempt < max_attempts:
            log(f"未落盘报告且 stdout 仅 {len(out)} 字符(疑似假完成), 将重试")
            continue
    log(f"已达最大重试次数 {max_attempts} 仍未落盘报告, 返回最后一次输出交主流程判定。")
    return last_out


# ---------- 数据看板 HTML 文件查找 ----------
# 从文件主干末尾提取 YYYY-MM-DD 日期(如 weekly_review_dashboard_2026-07-05 → 2026-07-05,
# weekly_review_2026-07-05 → 2026-07-05)。报告 md 与看板 html 的文件名均以该日期结尾
# (见 ashare-dashboard 技能 SKILL.md 步骤6: 日期取运行当天, 与各复盘技能报告 md 的日期口径
# 一致), 据此把看板与报告按"同一报告周期"配对。
_DATE_SUFFIX_RE = re.compile(r"(\d{4}-\d{2}-\d{2})$")


def _extract_date_suffix(stem: str) -> Optional[str]:
    """从文件主干末尾提取 YYYY-MM-DD 日期; 没有则 None。"""
    m = _DATE_SUFFIX_RE.search(stem)
    return m.group(1) if m else None


def find_dashboard_html(
    skill: str,
    report_path: Optional[Path] = None,
    since_ts: Optional[float] = None,
    require_fresh: bool = False,
) -> Optional[Path]:
    """在 output/ashare-dashboard/ 中查找指定技能的看板 HTML, 返回最合适的一份。

    匹配分两级, 前者优先:
      1. 本次运行期间新生成的看板(mtime >= since_ts)——最可靠, 直接用。
      2. 与报告同日的看板(文件名末尾日期 == 报告 md 末尾日期)——兜底: 无头模式下
         ashare-dashboard 偶尔会复用已存在的同日看板而不重新写盘, 导致 mtime 不更新,
         若只认 mtime 会漏掉它。同日看板属同一报告周期、口径一致, 采用它远好于退回纯文本。

    都不命中则返回 None(由调用方退回纯文本, 不强行用跨日旧看板, 避免张冠李戴)。

    Args:
        skill: 复盘技能名, 用于匹配模板前缀(如 ashare-weekly-review → weekly_review)
        report_path: 本次落盘报告路径, 用于提取报告日期做同日配对; None 则只走第1级
        since_ts: 可选, "本次运行起点"时间戳, 用于判定看板是否本次新生成(mtime >= since_ts)。
                  仅作优先级判定, 不再作硬过滤(避免漏掉同日复用未写盘的看板)。
        require_fresh: True 时只返回第1级(本次新生成)的看板, 不退回同日旧看板。
                  用于兜底渲染前的短路判定——没新生成就要触发兜底重新渲染, 而不是
                  直接用同日旧看板糊弄。默认 False(兜底渲染后再找时用, 允许退回同日看板)。

    Returns:
        匹配的 HTML 文件路径, 未找到则 None
    """
    dashboard_dir = PROJECT_ROOT / "output" / "ashare-dashboard"
    if not dashboard_dir.is_dir():
        return None

    prefix = SKILL_TO_DASHBOARD_PREFIX.get(skill)
    if not prefix:
        return None

    stem_prefix = f"{prefix}_dashboard_"
    target_date = _extract_date_suffix(report_path.stem) if report_path else None

    fresh_matches = []  # (mtime, path): 本次运行期间新生成
    date_matches = []   # (mtime, path): 与报告同日(任意 mtime)
    for f in dashboard_dir.glob("*.html"):
        if not f.stem.startswith(stem_prefix):
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if since_ts is None or mtime >= since_ts:
            fresh_matches.append((mtime, f))
        if target_date and _extract_date_suffix(f.stem) == target_date:
            date_matches.append((mtime, f))

    if fresh_matches:
        fresh_matches.sort(key=lambda x: x[0], reverse=True)
        log(f"找到数据看板文件(本次运行新生成): {fresh_matches[0][1].name}")
        return fresh_matches[0][1]

    if not require_fresh and date_matches:
        date_matches.sort(key=lambda x: x[0], reverse=True)
        chosen = date_matches[0][1]
        log(
            f"找到数据看板文件(按报告日期 {target_date} 同日匹配): {chosen.name}"
            f" —— mtime 早于本次运行起点, 可能为同日较早生成/被复用未重写; 属同一报告周期, 仍采用。"
        )
        return chosen

    log(f"未找到匹配的数据看板文件 (prefix={prefix}, 报告日期={target_date}, since_ts={since_ts})")
    return None


# ---------- 落盘报告回读 ----------
def find_report_markdown(skill: str, since_ts: Optional[float] = None) -> Optional[Path]:
    """定位指定复盘技能最近一次落盘的报告 markdown。

    各复盘技能在"输出保存"环节把完整报告写到 output/ashare-<skill>/ 下, 文件名形如
    `<prefix>_[日期].md`, 日期均取运行当天。这里按前缀 + 最近修改时间定位, 不假定
    日期, 以兼容手动补跑(当天生成、文件名日期非当天)等情形。

    Args:
        since_ts: 可选, 仅保留 mtime >= since_ts 的文件(用于限定"本次运行期间落盘"的报告,
                  避免本次技能没存盘时误用上一轮的旧报告); None 则不限时间, 取最新一份
    """
    info = SKILL_TO_REPORT.get(skill)
    if not info:
        return None
    report_dir = PROJECT_ROOT / "output" / info[0]
    if not report_dir.is_dir():
        return None
    stem_prefix = f"{info[1]}_"
    matches = []
    for f in report_dir.glob("*.md"):
        if not f.stem.startswith(stem_prefix):
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if since_ts is not None and mtime < since_ts:
            continue
        matches.append((mtime, f))
    matches.sort(key=lambda x: x[0], reverse=True)
    return matches[0][1] if matches else None


def resolve_report_body(skill: str, stdout_report: str, run_start_ts: float) -> str:
    """决定邮件正文用哪份文本: 优先取技能本次落盘的完整报告, stdout 仅作兜底。

    无头模式下调 review-orchestrator agent 做完整复盘时, agent 往往只在 stdout 回一段
    简短摘要(如"已完成, 报告已保存到..."), 真正的完整报告在落盘的 markdown 里。若直接
    用 stdout 作邮件正文, 用户只会收到一两百字的摘要。故当本次落盘报告比 stdout 更长时,
    改用落盘报告全文。

    只认 mtime >= run_start_ts(本次运行期间落盘)的报告, 避免本次技能没存盘时误发上一轮
    旧报告; 本次没有新报告则退回 stdout。
    """
    md_path = find_report_markdown(skill, since_ts=run_start_ts)
    if not md_path:
        return stdout_report
    try:
        saved = md_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        log(f"读取落盘报告失败 {md_path.name}: {e}")
        return stdout_report
    if len(saved) > len(stdout_report):
        log(f"使用落盘报告作为邮件正文: {md_path.name} ({len(saved)} 字符, stdout 仅 {len(stdout_report)} 字符)")
        return saved
    return stdout_report


# ---------- 数据看板兜底生成 ----------
def ensure_dashboard(skill: str, cfg: dict, run_start_ts: float) -> Optional[Path]:
    """确保本次运行产出了数据看板 HTML, 没有则兜底单独跑一次 ashare-dashboard。

    review-orchestrator 的复盘链路较长(周复盘: 风险预跑 → 周复盘 → 宏观完整维护 → 看板),
    无头模式下有时走不到最后的看板步骤就返回了, 导致 output/ashare-dashboard/ 为空。
    看板是纯展示层(读落盘报告 → 套模板 → 出 HTML), 不依赖编排链路的上下文, 因此这里
    在首轮没产出看板时, 单独发一个聚焦的 claude 调用: 直接读落盘报告文件, 调
    ashare-dashboard 渲染。失败不抛错, 由调用方退回纯文本邮件。
    """
    # 先定位本次落盘报告: 既是兜底渲染的数据源, 也用于按报告日期配对看板。
    report_path = find_report_markdown(skill, since_ts=run_start_ts)
    if not report_path:
        log("未找到落盘报告, 无法兜底生成数据看板")
        return None

    # 首轮短路: 若本次运行已新生成看板(mtime >= run_start_ts), 直接用, 不再发起兜底调用。
    # 注意此处 require_fresh=True —— 同日较早的旧看板不短路, 要触发兜底重新渲染以匹配
    # 本次刚落盘的新报告(否则会拿旧报告的看板糊弄过去)。
    existing = find_dashboard_html(skill, report_path=report_path,
                                   since_ts=run_start_ts, require_fresh=True)
    if existing:
        return existing

    ccfg = cfg.get("claude", {}) or {}
    # 看板渲染只是展示层, 给一个比完整复盘短的超时(默认 600s), 也可在 config 单独配。
    timeout = int(ccfg.get("dashboard_timeout_seconds", 600))
    # 显式指定模板目录, 避免 headless 模式下 skill 从项目根找 templates/ 找不到
    # (ashare-dashboard 技能使用 .claude/skills/ashare-dashboard/templates/ 下的模板)。
    template_dir = str(PROJECT_ROOT / ".claude" / "skills" / "ashare-dashboard" / "templates")
    prompt = (
        f"请读取报告文件 {report_path} 的完整内容作为数据源，调用 ashare-dashboard 技能，"
        f"将其渲染为移动端数据看板 HTML 单文件，保存到 output/ashare-dashboard/ 目录。"
        f"文件名按 ashare-dashboard 技能规则从匹配到的模板派生。"
        f"模板目录位于 {template_dir}，其中已有以下模板文件：weekly_review.html、"
        f"evening_review.html、intraday_review.html、morning_brief.html，"
        f"请按报告类型匹配对应模板。"
        f"若 output/ashare-dashboard/ 下已存在同日的同名看板文件，必须重新写入覆盖"
        f"（不要复用旧文件、不要因已存在就跳过写盘），确保看板内容严格基于本次数据源报告。"
        f"只做展示层渲染：不重新取数、不做新分析、不编造数据源里没有的内容，"
        f"数据缺失按模板规则占位。"
        f"必须实际调用 Write 工具把 HTML 写入磁盘，写盘后回读该文件确认存在且非空；"
        f"不得只在回复里描述“已生成/文件路径/大小”而实际未写盘——未真正落盘的看板等同于未生成。"
    )
    log(f"首轮未产出数据看板, 启动看板渲染兜底调用 (超时 {timeout}s)")
    try:
        out = _run_claude(prompt, ccfg, timeout, "看板渲染兜底调用")
        # 记录 stdout 摘要便于排查(完整输出可能很长, 截取前 500 字符)
        preview = out[:500]
        if len(out) > 500:
            preview += f"...(共 {len(out)} 字符)"
        log(f"看板渲染兜底调用输出: {preview}")
    except RuntimeError as e:
        log(f"看板渲染兜底调用失败: {e}")
        return None
    # 兜底后再找: 优先本次新生成(若 claude 确实重写了盘); 若 claude 仍复用旧文件未写盘
    # (mtime 未更新), 则退回与报告同日的看板——属同一报告周期, 好于退回纯文本。
    return find_dashboard_html(skill, report_path=report_path, since_ts=run_start_ts)


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
    # 记录本次运行起点, 用于"只认本次运行期间新生成的看板", 避免误用上一轮遗留文件。
    run_start_ts = now.timestamp()
    try:
        report = run_skill(skill, cfg, run_start_ts)
        if not args.no_email:
            # 无头 Claude 的 stdout 常只是一段摘要, 完整报告在落盘 markdown 里——优先用它。
            report = resolve_report_body(skill, report, run_start_ts)
            # 假完成兜底: 多次重试后仍无落盘报告且 stdout 过短 -> 判失败发告警, 不发过渡话术废邮件。
            # (主 Claude 未等 review-orchestrator 返回即回过渡话术时, 技能不会落盘。)
            if not find_report_markdown(skill, since_ts=run_start_ts) and len(report) < 2000:
                raise RuntimeError(
                    f"技能 {skill} 疑似假完成: 多次调用后仍未落盘报告, stdout 仅 {len(report)} 字符"
                    f"(可能是主 Claude 未等 review-orchestrator 返回即回过渡话术)。请重试或检查编排链路。"
                )
            if dashboard_enabled:
                html_path = ensure_dashboard(skill, cfg, run_start_ts)
                if html_path:
                    html_body = html_path.read_text(encoding="utf-8") + render_risk_appendix_html(date_str)
                    send_email(ecfg, f"{prefix} {skill} {date_str}", html_body, log, body_type="html")
                else:
                    log("数据看板 HTML 文件未找到, 退回纯文本邮件(发送落盘报告全文)")
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
