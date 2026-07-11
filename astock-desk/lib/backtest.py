# -*- coding: utf-8 -*-
"""
严肃回测引擎（纯 python，无第三方依赖）。
目标：让用户在信任任何策略之前，先看到它在历史数据上的**真实赢面与亏损幅度**。

包含：
- 三个可解释的规则策略：均线金叉/死叉、MACD、RSI 超买超卖。
- 逐日模拟：T+1 约束、双边手续费+印花税、按收盘价成交。
- 指标：总收益、年化、最大回撤、夏普、胜率、盈亏比、交易次数，并与「买入持有」基准对比。
- 输出资金曲线，供前端画图。

刻意保持规则透明：多智能体的价值在于"研判+教学"，而回测用于校准预期、
教用户看懂"胜率/回撤/夏普"这些真正重要的指标——这本身就是重要的一课。
"""
import math
from . import market

# A股交易成本（近似）：佣金双边万2.5(最低5元忽略)、印花税卖出千1、过户费忽略
COMM = 0.00025
STAMP = 0.001


def _ma(vals, n):
    return [sum(vals[i - n + 1:i + 1]) / n if i >= n - 1 else None for i in range(len(vals))]


def _signals_ma(closes, short=5, long=20):
    """均线金叉买、死叉卖。返回每日目标仓位 (0/1)。"""
    ms, ml = _ma(closes, short), _ma(closes, long)
    pos, out = 0, []
    for i in range(len(closes)):
        if ms[i] is not None and ml[i] is not None:
            if ms[i] > ml[i]:
                pos = 1
            elif ms[i] < ml[i]:
                pos = 0
        out.append(pos)
    return out


def _ema(vals, n):
    out, k = [], 2 / (n + 1)
    for i, v in enumerate(vals):
        out.append(v if i == 0 else out[-1] + k * (v - out[-1]))
    return out


def _signals_macd(closes):
    """DIF 上穿 DEA(金叉) 买，下穿卖。"""
    dif = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
    dea = _ema(dif, 9)
    pos, out = 0, []
    for i in range(len(closes)):
        if i > 33:
            if dif[i] > dea[i]:
                pos = 1
            elif dif[i] < dea[i]:
                pos = 0
        out.append(pos)
    return out


def _signals_rsi(closes, period=14, buy=30, sell=70):
    """RSI 上穿 30 买、下穿 70 卖（均值回归）。"""
    rsis = [None] * len(closes)
    for i in range(period, len(closes)):
        g = s = 0.0
        for j in range(i - period + 1, i + 1):
            ch = closes[j] - closes[j - 1]
            g += max(ch, 0); s += max(-ch, 0)
        rsis[i] = 100.0 if s == 0 else 100 - 100 / (1 + g / s)
    pos, out = 0, []
    for i in range(len(closes)):
        r = rsis[i]
        if r is not None:
            if r < buy:
                pos = 1
            elif r > sell:
                pos = 0
        out.append(pos)
    return out


STRATEGIES = {
    "ma": ("均线金叉(5/20)", _signals_ma),
    "macd": ("MACD金叉死叉", _signals_macd),
    "rsi": ("RSI超买超卖(14)", _signals_rsi),
}


def _metrics(equity, dates, trades, wins, gross_win, gross_loss):
    n = len(equity)
    total_ret = equity[-1] / equity[0] - 1
    years = max((n / 244.0), 1e-9)
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1 if equity[-1] > 0 else -1
    # 最大回撤
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    # 夏普（日频→年化，无风险利率按0）
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, n)]
    if len(rets) > 2:
        mu = sum(rets) / len(rets)
        sd = (sum((r - mu) ** 2 for r in rets) / len(rets)) ** 0.5
        sharpe = (mu / sd * math.sqrt(244)) if sd > 1e-12 else 0.0
    else:
        sharpe = 0.0
    return {
        "total_return_pct": round(total_ret * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "sharpe": round(sharpe, 2),
        "trades": trades,
        "win_rate_pct": round(wins / trades * 100, 1) if trades else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 1e-9 else None,
    }


def _run_strategy(closes, dates, signals):
    """逐日模拟：signals[i] 为第 i 日收盘后的目标仓位；T+1，次日不立即再卖。
    简化：当日收盘价按信号切换（含成本），持有则跟随价格变动。"""
    cash, shares, invested = 1.0, 0.0, False
    equity = []
    trades = wins = 0
    entry_price = None
    gross_win = gross_loss = 0.0
    for i in range(len(closes)):
        price = closes[i]
        want = signals[i]
        # 先按当前持仓计算净值
        if want == 1 and not invested:
            # 买入：扣佣金
            shares = cash * (1 - COMM) / price
            cash = 0.0
            invested = True
            entry_price = price
            trades += 1
        elif want == 0 and invested:
            # 卖出：扣佣金+印花税
            cash = shares * price * (1 - COMM - STAMP)
            shares = 0.0
            invested = False
            r = price / entry_price - 1
            if r > 0: wins += 1; gross_win += r
            else: gross_loss += -r
            entry_price = None
        equity.append(cash + shares * price)
    # 期末强平用于统计（不额外记为交易胜负，仅结算净值）
    if invested:
        price = closes[-1]
        val = shares * price * (1 - COMM - STAMP)
        equity[-1] = val
        r = price / entry_price - 1
        if r > 0: wins += 1; gross_win += r
        else: gross_loss += -r
    m = _metrics(equity, dates, trades, wins, gross_win, gross_loss)
    return m, equity


def backtest(code, strategy="ma", period="day", limit=250):
    ks = market.kline(code, period, limit)
    if len(ks) < 40:
        return {"error": "历史数据不足，无法回测"}
    closes = [k["close"] for k in ks]
    dates = [k["date"] for k in ks]
    name, fn = STRATEGIES.get(strategy, STRATEGIES["ma"])
    signals = fn(closes)
    m, equity = _run_strategy(closes, dates, signals)

    # 买入持有基准
    bh_equity = [closes[i] / closes[0] for i in range(len(closes))]
    bh = _metrics([1.0] + bh_equity[1:] if False else bh_equity, dates, 1,
                  1 if closes[-1] > closes[0] else 0,
                  max(closes[-1] / closes[0] - 1, 0), max(1 - closes[-1] / closes[0], 0))

    # 抽稀资金曲线（前端画图，最多120点）
    step = max(1, len(equity) // 120)
    curve = [{"date": dates[i], "strategy": round(equity[i], 4), "buyhold": round(bh_equity[i], 4)}
             for i in range(0, len(equity), step)]
    if curve[-1]["date"] != dates[-1]:
        curve.append({"date": dates[-1], "strategy": round(equity[-1], 4), "buyhold": round(bh_equity[-1], 4)})

    return {
        "code": market.normalize(code),
        "strategy": strategy,
        "strategy_name": name,
        "period": f"{dates[0]} ~ {dates[-1]}",
        "bars": len(closes),
        "metrics": m,
        "benchmark": bh,
        "curve": curve,
        "verdict": _verdict(m, bh),
        "note": "回测基于新浪不复权日线与简化成交假设，仅用于校准预期，不代表未来收益。",
    }


def _verdict(m, bh):
    """一句话结论：策略是否跑赢买入持有、回撤是否可接受。"""
    beat = (m["total_return_pct"] or 0) - (bh["total_return_pct"] or 0)
    parts = []
    parts.append(("跑赢" if beat >= 0 else "跑输") + f"买入持有 {abs(round(beat,1))}个百分点")
    if m["max_drawdown_pct"] is not None:
        parts.append(f"最大回撤{m['max_drawdown_pct']}%")
    if m["sharpe"] is not None:
        q = "优秀" if m["sharpe"] > 1.5 else "尚可" if m["sharpe"] > 0.8 else "偏弱"
        parts.append(f"夏普{m['sharpe']}({q})")
    return "；".join(parts)


def compare_all(code, period="day", limit=250):
    """一次性跑三种策略 + 基准，便于横向对比。"""
    out = {}
    for key in STRATEGIES:
        out[key] = backtest(code, key, period, limit)
    return out


if __name__ == "__main__":
    import sys, json
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    for key in STRATEGIES:
        r = backtest(code, key)
        if "error" in r:
            print(key, r["error"]); continue
        print(f"\n== {r['strategy_name']} ({r['period']}, {r['bars']}根) ==")
        print("  策略:", r["metrics"])
        print("  基准:", {k: r["benchmark"][k] for k in ("total_return_pct", "max_drawdown_pct", "sharpe")})
        print("  结论:", r["verdict"])
