#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
score.py — 多维度技术面打分与结论生成

输入:
  --indicators-json  compute_indicators.py 的输出
  --meta-json        数据获取步骤产出的meta JSON(用于读取数据质量flags做风险否决)

输出: 一份JSON,包含:
  - dimension_scores: 五个维度各自的分数(-2..2)与理由列表
  - total_score: 加权总分(-2..2)
  - verdict: 结论文字(中文)
  - confidence: 高/中/低
  - risk_overrides: 触发了哪些风险否决规则
  - stop_loss_candidates / target_candidates / risk_reward_ratio
  - holding_period_note: 持仓周期建议(定性,非精确天数预测)

具体打分细则与依据见 references/scoring_framework.md,本脚本是该文档的可执行版本。
"""

import argparse
import json


DEFAULT_WEIGHTS = {
    "trend": 0.35,
    "momentum": 0.20,
    "volume": 0.15,
    "volatility_position": 0.15,
    "relative_strength": 0.15,
}


def clip(v, lo=-2.0, hi=2.0):
    return max(lo, min(hi, v))


# ----------------------------------------------------------------------------
# 五个维度的打分函数
# ----------------------------------------------------------------------------
def score_trend(latest, signals):
    score, reasons = 0.0, []
    arrangement = signals.get("ma_arrangement")
    if arrangement == "bullish_aligned":
        score += 1.5
        reasons.append("均线呈多头排列(MA5>MA20>MA60>MA120),中期趋势向上")
    elif arrangement == "bearish_aligned":
        score -= 1.5
        reasons.append("均线呈空头排列(MA5<MA20<MA60<MA120),中期趋势向下")
    else:
        reasons.append("均线排列纠缠或不完全一致,中期趋势方向尚不清晰")

    pvm = signals.get("price_vs_ma250")
    if pvm == "above":
        score += 0.5
        reasons.append("股价位于年线(MA250)上方,长期格局仍偏多")
    elif pvm == "below":
        score -= 0.5
        reasons.append("股价位于年线(MA250)下方,长期格局偏弱")

    adx = latest.get("adx14")
    if adx is not None:
        if adx >= 25:
            if (latest.get("plus_di14") or 0) > (latest.get("minus_di14") or 0):
                score += 0.5
                reasons.append(f"ADX={adx}显示趋势较强,且+DI>-DI,上行动能占优")
            else:
                score -= 0.5
                reasons.append(f"ADX={adx}显示趋势较强,但-DI>+DI,下行动能占优")
        else:
            reasons.append(f"ADX={adx}<25,当前更接近震荡而非单边趋势,趋势类信号可信度打折")

    if signals.get("ma20_ma60") == "golden_cross":
        score += 0.3
        reasons.append("MA20上穿MA60,中期均线金叉")
    elif signals.get("ma20_ma60") == "death_cross":
        score -= 0.3
        reasons.append("MA20下穿MA60,中期均线死叉")

    return clip(score), reasons


def score_momentum(latest, signals):
    score, reasons = 0.0, []
    rsi12 = latest.get("rsi12")
    if rsi12 is not None:
        if rsi12 >= 80:
            score -= 1.2
            reasons.append(f"RSI12={rsi12}处于极度超买区间,短期追高风险较大")
        elif rsi12 >= 70:
            score -= 0.6
            reasons.append(f"RSI12={rsi12}进入超买区间,需警惕短期回调")
        elif rsi12 <= 20:
            score += 1.2
            reasons.append(f"RSI12={rsi12}处于极度超卖区间,存在技术性反弹动能")
        elif rsi12 <= 30:
            score += 0.6
            reasons.append(f"RSI12={rsi12}进入超卖区间,下跌动能可能减弱")
        else:
            reasons.append(f"RSI12={rsi12}处于中性区间,暂无明显超买超卖信号")

    if signals.get("macd_dif_dea") == "golden_cross":
        score += 0.6
        reasons.append("MACD出现金叉(DIF上穿DEA),短中期动能转强")
    elif signals.get("macd_dif_dea") == "death_cross":
        score -= 0.6
        reasons.append("MACD出现死叉(DIF下穿DEA),短中期动能转弱")
    else:
        dif, dea = latest.get("macd_dif"), latest.get("macd_dea")
        if dif is not None and dea is not None:
            if dif > dea:
                score += 0.3
                reasons.append("MACD的DIF位于DEA上方,动能偏多")
            else:
                score -= 0.3
                reasons.append("MACD的DIF位于DEA下方,动能偏空")

    kj = latest.get("kdj_j")
    if kj is not None:
        if kj > 100:
            score -= 0.3
            reasons.append(f"KDJ的J值={kj}超过100,短线层面超买")
        elif kj < 0:
            score += 0.3
            reasons.append(f"KDJ的J值={kj}低于0,短线层面超卖")

    rsi_div = signals.get("rsi_divergence", {})
    macd_div = signals.get("macd_divergence", {})
    if rsi_div.get("bearish_divergence") or macd_div.get("bearish_divergence"):
        score -= 0.5
        reasons.append("价格创出新高但RSI/MACD未同步新高,存在顶背离风险")
    if rsi_div.get("bullish_divergence") or macd_div.get("bullish_divergence"):
        score += 0.5
        reasons.append("价格创出新低但RSI/MACD未同步新低,存在底背离信号")

    return clip(score), reasons


def score_volume(latest):
    score, reasons = 0.0, []
    vr, pct = latest.get("vol_ratio"), latest.get("pct_chg")
    if vr is not None and pct is not None:
        if vr >= 1.5 and pct > 0:
            score += 1.0
            reasons.append(f"量比={vr},放量上涨,资金配合度高,上涨更具持续性")
        elif vr >= 1.5 and pct < 0:
            score -= 1.0
            reasons.append(f"量比={vr},放量下跌,抛压较重")
        elif vr <= 0.7 and pct > 0:
            score += 0.3
            reasons.append(f"量比={vr},缩量上涨,资金参与度一般,持续性有待观察")
        elif vr <= 0.7 and pct < 0:
            score += 0.2
            reasons.append(f"量比={vr},缩量下跌,抛压有限,下跌动能可能减弱")
        else:
            reasons.append("量能处于正常水平,未见明显放量或缩量特征")

    obv_trend = latest.get("obv_trend_20d")
    if obv_trend == "up":
        score += 0.3
        reasons.append("OBV近20日呈上升趋势,量能与价格基本同步")
    elif obv_trend == "down":
        score -= 0.3
        reasons.append("OBV近20日呈下降趋势,需留意是否存在量价背离")

    return clip(score), reasons


def score_volatility_position(latest):
    score, reasons = 0.0, []
    pctb = latest.get("boll_pctb")
    if pctb is not None:
        if pctb >= 1:
            score -= 0.8
            reasons.append(f"股价位于布林带上轨之上(%b={pctb}),短期涨幅可能已较充分,新进场性价比下降")
        elif pctb >= 0.8:
            score -= 0.3
            reasons.append(f"股价接近布林带上轨(%b={pctb}),短期存在获利回吐压力")
        elif pctb <= 0:
            score += 0.5
            reasons.append(f"股价位于布林带下轨之下(%b={pctb}),存在超跌修复可能,但也需警惕趋势破位")
        elif pctb <= 0.2:
            score += 0.2
            reasons.append(f"股价接近布林带下轨(%b={pctb}),处于相对低位")

    bias = latest.get("bias20")
    if bias is not None:
        if bias >= 15:
            score -= 0.8
            reasons.append(f"股价偏离20日均线达{bias}%,乖离率偏高,短期有均值回归(回调)压力")
        elif bias <= -15:
            score += 0.5
            reasons.append(f"股价偏离20日均线达{bias}%(向下),存在技术性反弹需求,但需结合趋势判断是否为下跌中继")

    bw = latest.get("boll_bandwidth")
    if bw is not None and bw < 0.05:
        reasons.append(f"布林带带宽={bw},处于近期收窄状态,后续可能面临变盘(方向未定,需结合趋势/量能进一步判断)")

    return clip(score), reasons


def score_relative_strength(rs):
    if rs is None:
        return None, ["未获取到基准指数数据,本维度跳过,权重已分配给其余维度"]
    score, reasons = 0.0, []
    a20, a60 = rs.get("alpha_20d"), rs.get("alpha_60d")
    if a20 is not None:
        if a20 > 0.05:
            score += 1.0
            reasons.append(f"近20个交易日跑赢基准指数约{a20 * 100:.1f}个百分点,相对强势")
        elif a20 < -0.05:
            score -= 1.0
            reasons.append(f"近20个交易日跑输基准指数约{abs(a20) * 100:.1f}个百分点,相对弱势")
        else:
            reasons.append("近20个交易日相对基准指数表现接近,无明显相对强弱")
    if a60 is not None:
        if a60 > 0.1:
            score += 0.5
            reasons.append(f"近60个交易日跑赢基准指数约{a60 * 100:.1f}个百分点")
        elif a60 < -0.1:
            score -= 0.5
            reasons.append(f"近60个交易日跑输基准指数约{abs(a60) * 100:.1f}个百分点")
    return clip(score), reasons


# ----------------------------------------------------------------------------
# 综合
# ----------------------------------------------------------------------------
def verdict_from_score(total):
    if total >= 1.0:
        return "技术面偏多,可考虑买入/逢低布局"
    if total >= 0.3:
        return "技术面温和偏多,信号强度一般,建议分批/谨慎参与"
    if total > -0.3:
        return "技术面中性,信号不明确,建议观望等待更清晰信号"
    if total > -1.0:
        return "技术面温和偏空,建议规避或减仓,谨慎参与"
    return "技术面明显偏空,建议规避/逢高减仓"


def main():
    parser = argparse.ArgumentParser(description="多维度技术面打分")
    parser.add_argument("--indicators-json", required=True)
    parser.add_argument("--meta-json", required=True)
    parser.add_argument("--out-json", required=True)
    args = parser.parse_args()

    with open(args.indicators_json, "r", encoding="utf-8") as f:
        ind = json.load(f)
    with open(args.meta_json, "r", encoding="utf-8") as f:
        meta = json.load(f)

    latest = ind["latest"]
    signals = ind["signals"]
    rs = ind.get("relative_strength")
    flags = meta.get("flags", {})

    dims = {}
    dims["trend"] = score_trend(latest, signals)
    dims["momentum"] = score_momentum(latest, signals)
    dims["volume"] = score_volume(latest)
    dims["volatility_position"] = score_volatility_position(latest)
    dims["relative_strength"] = score_relative_strength(rs)

    weights = dict(DEFAULT_WEIGHTS)
    if dims["relative_strength"][0] is None:
        dropped = weights.pop("relative_strength")
        total_remaining = sum(weights.values())
        weights = {k: v / total_remaining for k, v in weights.items()}

    total_score = 0.0
    agree_count, scored_dims = 0, 0
    dimension_scores = {}
    for name, (score, reasons) in dims.items():
        dimension_scores[name] = {
            "score": None if score is None else round(score, 2),
            "weight": round(weights.get(name, 0.0), 3),
            "reasons": reasons,
        }
        if score is not None and name in weights:
            total_score += score * weights[name]
            scored_dims += 1

    for name, (score, _) in dims.items():
        if score is not None and name in weights:
            if (score > 0 and total_score > 0) or (score < 0 and total_score < 0) or (abs(score) < 0.1):
                agree_count += 1
    agree_ratio = agree_count / scored_dims if scored_dims else 0.0

    verdict = verdict_from_score(total_score)

    # 置信度
    if abs(total_score) >= 0.8 and agree_ratio >= 0.8:
        confidence = "高"
    elif abs(total_score) >= 0.3 and agree_ratio >= 0.6:
        confidence = "中"
    else:
        confidence = "低"

    # 风险否决规则
    # hard_override: 数据本身不可靠,强制改观望; soft_override: 仅压低置信度,不改方向
    risk_overrides = []
    hard_override = False
    if flags.get("is_st"):
        risk_overrides.append("标的存在ST/*ST标记,退市与财务异常风险显著,技术分析参考价值大幅降低")
        hard_override = True
    if flags.get("insufficient_history"):
        risk_overrides.append("历史数据不足60个交易日,中长期均线/趋势结论可信度低")
        hard_override = True
    if flags.get("suspected_suspension_gaps", 0) > 0:
        risk_overrides.append("数据中存在疑似长期停牌缺口,技术形态被打断,结论需谨慎对待")
        hard_override = True
    if flags.get("recent_limit_move_days", 0) >= 3:
        risk_overrides.append("近期频繁出现涨跌停,波动极端,指标可能失真")

    if hard_override:
        verdict = f"⚠ 数据质量受限,建议观望,暂不依据当前技术信号操作(原始信号倾向: {verdict})"
        confidence = "低"
    elif risk_overrides:
        confidence = "低"

    # 止损/止盈参考
    close = latest.get("close")
    atr = latest.get("atr14") or 0
    support = ind.get("key_levels", {}).get("support", [])
    resistance = ind.get("key_levels", {}).get("resistance", [])
    nearest_support = max((s["level"] for s in support if s["level"] < close), default=None) if close else None
    nearest_resistance = min((r["level"] for r in resistance if r["level"] > close), default=None) if close else None

    stop_loss_candidates = {
        "support_based": round(nearest_support, 2) if nearest_support else None,
        "atr_based_2x": round(close - 2 * atr, 2) if close else None,
    }
    target_candidates = {
        "resistance_based": round(nearest_resistance, 2) if nearest_resistance else None,
        "atr_based_3x": round(close + 3 * atr, 2) if close else None,
    }

    rr_stop = stop_loss_candidates["support_based"] or stop_loss_candidates["atr_based_2x"]
    rr_target = target_candidates["resistance_based"] or target_candidates["atr_based_3x"]
    risk_reward_ratio = None
    if close and rr_stop and rr_target and (close - rr_stop) > 0:
        risk_reward_ratio = round((rr_target - close) / (close - rr_stop), 2)

    out = {
        "ok": True,
        "dimension_scores": dimension_scores,
        "total_score": round(total_score, 3),
        "verdict": verdict,
        "confidence": confidence,
        "risk_overrides": risk_overrides,
        "stop_loss_candidates": stop_loss_candidates,
        "target_candidates": target_candidates,
        "risk_reward_ratio_illustrative": risk_reward_ratio,
        "holding_period_note": (
            "本结论基于日线中长期指标(均线周期20-250日),信号有效性通常需要数个交易日到数周才能验证,"
            "本skill设计用于中长期决策参考,不适用于日内/超短线操作。"
            "建议结合下方关键支撑/阻力位进行参考,而非依赖单一时间点的判断。"
        ),
    }

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
