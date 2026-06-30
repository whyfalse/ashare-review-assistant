#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compute_indicators.py — 计算中长期技术分析所需的指标

输入: 数据获取步骤产出的标准化日线CSV(date,open,high,low,close,volume,amount,turnover,pct_chg)
输出: 一份JSON,包含:
  - latest: 最新一个交易日各项指标的数值与状态flag
  - recent_series: 最近N个交易日的精简时间序列(供模型描述近期走势用,不是给用户看图表)
  - key_levels: 识别出的关键支撑/阻力位
  - signals: 金叉死叉、量价背离等离散事件信号
  - relative_strength: 与基准指数的相对强弱(若提供benchmark csv)

所有计算均为常规公开公式的直接实现,均使用日线数据,
参数选择(20/60/120/250日均线、MACD12-26-9、RSI6/12/24、KDJ9-3-3、BOLL20-2、ATR14、ADX14)
是中国A股市场中长期分析的常见惯例,具体口径见 references/indicators_guide.md。
"""

import argparse
import json
import sys

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------
# 基础指标
# ----------------------------------------------------------------------------
def add_moving_averages(df):
    for w in (5, 10, 20, 60, 120, 250):
        df[f"ma{w}"] = df["close"].rolling(w).mean()
    df["ema12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["ema26"] = df["close"].ewm(span=26, adjust=False).mean()
    return df


def add_macd(df):
    df["macd_dif"] = df["ema12"] - df["ema26"]
    df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = (df["macd_dif"] - df["macd_dea"]) * 2  # 国内常见画法(*2)
    return df


def _rsi(close, window):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.fillna(50)


def add_rsi(df):
    for w in (6, 12, 24):
        df[f"rsi{w}"] = _rsi(df["close"], w)
    return df


def add_kdj(df, n=9, m1=3, m2=3):
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = ((df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100).fillna(50)
    k = np.zeros(len(df))
    d = np.zeros(len(df))
    k_prev, d_prev = 50.0, 50.0
    for i, v in enumerate(rsv.values):
        k_i = (m1 - 1) / m1 * k_prev + 1 / m1 * v
        d_i = (m2 - 1) / m2 * d_prev + 1 / m2 * k_i
        k[i], d[i] = k_i, d_i
        k_prev, d_prev = k_i, d_i
    df["kdj_k"] = k
    df["kdj_d"] = d
    df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]
    return df


def add_boll(df, window=20, n_std=2):
    mid = df["close"].rolling(window).mean()
    std = df["close"].rolling(window).std()
    df["boll_mid"] = mid
    df["boll_upper"] = mid + n_std * std
    df["boll_lower"] = mid - n_std * std
    rng = (df["boll_upper"] - df["boll_lower"]).replace(0, np.nan)
    df["boll_pctb"] = (df["close"] - df["boll_lower"]) / rng
    df["boll_bandwidth"] = rng / mid
    return df


def add_atr(df, window=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / window, adjust=False).mean()
    return df


def add_adx(df, window=14):
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / window, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / window, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / window, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx14"] = dx.ewm(alpha=1 / window, adjust=False).mean()
    df["plus_di14"] = plus_di
    df["minus_di14"] = minus_di
    return df


def add_volume_features(df):
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma5"].shift(1)
    direction = np.sign(df["close"].diff()).fillna(0)
    df["obv"] = (direction * df["volume"]).cumsum()
    return df


def add_bias(df):
    df["bias20"] = (df["close"] - df["ma20"]) / df["ma20"] * 100
    return df


# ----------------------------------------------------------------------------
# 支撑阻力 / 摆动高低点
# ----------------------------------------------------------------------------
def find_swing_points(df, window=5):
    """局部摆动高/低点: 在前后window个交易日内是局部最大/最小"""
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    n = len(df)
    for i in range(window, n - window):
        seg_h = h[i - window: i + window + 1]
        seg_l = l[i - window: i + window + 1]
        if h[i] == seg_h.max():
            highs.append((i, h[i]))
        if l[i] == seg_l.min():
            lows.append((i, l[i]))
    return highs, lows


def cluster_levels(points, tolerance=0.02):
    """把相近(差异<tolerance比例)的价位合并成一个level,返回按出现次数/新近程度排序的列表"""
    if not points:
        return []
    vals = sorted(p[1] for p in points)
    clusters = []
    cur = [vals[0]]
    for v in vals[1:]:
        if abs(v - cur[-1]) / cur[-1] <= tolerance:
            cur.append(v)
        else:
            clusters.append(cur)
            cur = [v]
    clusters.append(cur)
    return [{"level": float(np.mean(c)), "touches": len(c)} for c in clusters]


def key_levels(df, lookback=120, window=5):
    sub = df.iloc[-lookback:].reset_index(drop=True) if len(df) > lookback else df.reset_index(drop=True)
    highs, lows = find_swing_points(sub, window=window)
    res_clusters = sorted(cluster_levels([(i, v) for i, v in highs]), key=lambda x: -x["touches"])
    sup_clusters = sorted(cluster_levels([(i, v) for i, v in lows]), key=lambda x: -x["touches"])
    last_close = df["close"].iloc[-1]
    resistance = sorted([c for c in res_clusters if c["level"] > last_close], key=lambda x: x["level"])[:3]
    support = sorted([c for c in sup_clusters if c["level"] < last_close], key=lambda x: -x["level"])[:3]
    # 补充滚动区间高低点作为兜底
    for w in (20, 60, 250):
        if len(df) >= w:
            hi = float(df["high"].iloc[-w:].max())
            lo = float(df["low"].iloc[-w:].min())
            if hi > last_close:
                resistance.append({"level": hi, "touches": 1, "note": f"近{w}日新高"})
            if lo < last_close:
                support.append({"level": lo, "touches": 1, "note": f"近{w}日新低"})
    return support[:4], resistance[:4]


# ----------------------------------------------------------------------------
# 离散信号: 金叉死叉 / 背离
# ----------------------------------------------------------------------------
def cross_signals(df, lookback=5):
    sig = {}

    def recent_cross(fast, slow):
        diff = df[fast] - df[slow]
        sign = np.sign(diff)
        changed = sign.diff().fillna(0)
        recent = changed.iloc[-lookback:]
        if (recent > 0).any():
            return "golden_cross"
        if (recent < 0).any():
            return "death_cross"
        return "none"

    sig["ma5_ma20"] = recent_cross("ma5", "ma20")
    sig["ma20_ma60"] = recent_cross("ma20", "ma60")
    sig["macd_dif_dea"] = recent_cross("macd_dif", "macd_dea")
    sig["kdj_k_d"] = recent_cross("kdj_k", "kdj_d")

    ma_cols = ["ma5", "ma20", "ma60", "ma120"]
    last = df.iloc[-1]
    if all(c in df.columns and not pd.isna(last[c]) for c in ma_cols):
        vals = [last[c] for c in ma_cols]
        if all(vals[i] > vals[i + 1] for i in range(len(vals) - 1)):
            sig["ma_arrangement"] = "bullish_aligned"  # 多头排列
        elif all(vals[i] < vals[i + 1] for i in range(len(vals) - 1)):
            sig["ma_arrangement"] = "bearish_aligned"  # 空头排列
        else:
            sig["ma_arrangement"] = "mixed"
    else:
        sig["ma_arrangement"] = "unknown"

    sig["price_vs_ma250"] = (
        "above" if not pd.isna(last.get("ma250", np.nan)) and last["close"] > last["ma250"]
        else "below" if not pd.isna(last.get("ma250", np.nan))
        else "unknown"
    )
    return sig


def detect_divergence(df, indicator_col, window=5, lookback=120):
    """简化版背离检测: 比较最近两个摆动高点(或低点)的价格与指标值方向是否一致"""
    sub = df.iloc[-lookback:].reset_index(drop=True) if len(df) > lookback else df.reset_index(drop=True)
    highs, lows = find_swing_points(sub, window=window)
    result = {"bearish_divergence": False, "bullish_divergence": False}
    if len(highs) >= 2:
        (i1, p1), (i2, p2) = highs[-2], highs[-1]
        ind1, ind2 = sub[indicator_col].iloc[i1], sub[indicator_col].iloc[i2]
        if p2 > p1 and ind2 < ind1:
            result["bearish_divergence"] = True  # 价格新高,指标未新高 -> 顶背离
    if len(lows) >= 2:
        (i1, p1), (i2, p2) = lows[-2], lows[-1]
        ind1, ind2 = sub[indicator_col].iloc[i1], sub[indicator_col].iloc[i2]
        if p2 < p1 and ind2 > ind1:
            result["bullish_divergence"] = True  # 价格新低,指标未新低 -> 底背离
    return result


# ----------------------------------------------------------------------------
# 相对强弱
# ----------------------------------------------------------------------------
def relative_strength(df, bench_df):
    if bench_df is None or bench_df.empty:
        return None
    merged = pd.merge(df[["date", "close"]], bench_df[["date", "close"]], on="date", suffixes=("_stock", "_bench"))
    if len(merged) < 25:
        return None
    out = {}
    for w in (20, 60):
        if len(merged) >= w + 1:
            stock_ret = merged["close_stock"].iloc[-1] / merged["close_stock"].iloc[-w - 1] - 1
            bench_ret = merged["close_bench"].iloc[-1] / merged["close_bench"].iloc[-w - 1] - 1
            out[f"alpha_{w}d"] = float(stock_ret - bench_ret)
    return out


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def round_or_none(v, nd=2):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return None
    return round(float(v), nd)


def main():
    parser = argparse.ArgumentParser(description="计算中长期技术分析指标")
    parser.add_argument("--csv", required=True, help="数据获取步骤产出的标准化日线CSV路径")
    parser.add_argument("--benchmark-csv", default=None, help="基准指数CSV路径(可选)")
    parser.add_argument("--out-json", required=True, help="输出JSON路径")
    parser.add_argument("--recent-n", type=int, default=20, help="recent_series包含最近多少个交易日")
    args = parser.parse_args()

    df = pd.read_csv(args.csv)
    if len(df) < 30:
        print(json.dumps({"ok": False, "error": "有效数据不足30个交易日,无法计算中长期指标"}, ensure_ascii=False))
        sys.exit(1)

    df = add_moving_averages(df)
    df = add_macd(df)
    df = add_rsi(df)
    df = add_kdj(df)
    df = add_boll(df)
    df = add_atr(df)
    df = add_adx(df)
    df = add_volume_features(df)
    df = add_bias(df)

    support, resistance = key_levels(df)
    signals = cross_signals(df)
    signals["macd_divergence"] = detect_divergence(df, "macd_dif")
    signals["rsi_divergence"] = detect_divergence(df, "rsi12")

    bench_df = pd.read_csv(args.benchmark_csv) if args.benchmark_csv else None
    rs = relative_strength(df, bench_df)

    last = df.iloc[-1]
    latest = {
        "date": last["date"],
        "close": round_or_none(last["close"]),
        "pct_chg": round_or_none(last.get("pct_chg")),
        "ma5": round_or_none(last["ma5"]), "ma10": round_or_none(last["ma10"]),
        "ma20": round_or_none(last["ma20"]), "ma60": round_or_none(last["ma60"]),
        "ma120": round_or_none(last["ma120"]), "ma250": round_or_none(last["ma250"]),
        "macd_dif": round_or_none(last["macd_dif"]), "macd_dea": round_or_none(last["macd_dea"]),
        "macd_hist": round_or_none(last["macd_hist"]),
        "rsi6": round_or_none(last["rsi6"]), "rsi12": round_or_none(last["rsi12"]), "rsi24": round_or_none(last["rsi24"]),
        "kdj_k": round_or_none(last["kdj_k"]), "kdj_d": round_or_none(last["kdj_d"]), "kdj_j": round_or_none(last["kdj_j"]),
        "boll_upper": round_or_none(last["boll_upper"]), "boll_mid": round_or_none(last["boll_mid"]),
        "boll_lower": round_or_none(last["boll_lower"]), "boll_pctb": round_or_none(last["boll_pctb"], 3),
        "boll_bandwidth": round_or_none(last["boll_bandwidth"], 4),
        "atr14": round_or_none(last["atr14"]),
        "adx14": round_or_none(last["adx14"]), "plus_di14": round_or_none(last["plus_di14"]),
        "minus_di14": round_or_none(last["minus_di14"]),
        "vol_ratio": round_or_none(last["vol_ratio"]),
        "bias20": round_or_none(last["bias20"]),
        "obv_trend_20d": (
            "up" if df["obv"].iloc[-1] > df["obv"].iloc[-20] else "down"
        ) if len(df) >= 20 else "unknown",
    }

    recent_cols = ["date", "close", "pct_chg", "volume", "ma20", "ma60", "rsi12", "macd_dif", "macd_dea"]
    recent_series = df[recent_cols].tail(args.recent_n).to_dict(orient="records")
    recent_series = [{k: (round_or_none(v) if isinstance(v, float) else v) for k, v in row.items()} for row in recent_series]

    out = {
        "ok": True,
        "latest": latest,
        "signals": signals,
        "key_levels": {"support": support, "resistance": resistance},
        "relative_strength": rs,
        "recent_series": recent_series,
    }

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
