# -*- coding: utf-8 -*-
"""
多智能体交易委员会（复现并扩展 TradingAgents 思路）：
  分析师层：基本面 / 技术面 / 情绪资金面   —— 三份独立视角
  交易员：综合三份研报，给出买卖方向、置信度、目标价、止损
  风控官：审查交易员方案，给仓位上限 / 否决权 / 风险清单

教学设计：
- 每个 agent 只输出**紧凑** JSON（分数/立场/短要点/一个术语名），避免长响应被代理中途截断。
- 深度金融知识讲解走独立的 explain() 按需接口：用户点某个术语时才单独生成，
  单主题、单调用，稳定不截断，也更符合「边看边学」。

工程要点：
- 各 agent 只拿与自己职责相关的数据（工具隔离），避免信息串味。
- 分析师串行执行（代理在并发/长响应下会截断流），短输出下总耗时可接受。
- 全程强调「教育与研究用途，不构成投资建议」。
"""
import json
from . import market, llm

DISCLAIMER = "本分析为教育与研究用途，不构成投资建议；A股实盘存在滑点、T+1、政策与情绪风险，请独立决策并控制风险。"


# ============ 数据打包 ============
def gather(code):
    q = market.quote(code)
    ks = market.kline(code, "day", 250)
    closes = [k["close"] for k in ks]
    highs = [k["high"] for k in ks]
    lows = [k["low"] for k in ks]
    vols = [k["volume"] for k in ks]
    ind = market.indicators(closes, highs, lows, vols) if closes else {}
    return {
        "code": market.normalize(code),
        "quote": q,
        "kline_tail": ks[-30:],
        "indicators": ind,
        "fundamentals": market.fundamentals(code),
        "money_flow": market.money_flow(code),
        "news": market.news(code, 8),
        "index": market.index_snapshot(),
    }


def _j(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


# ============ 分析师：基本面 ============
FUNDAMENTAL_SYS = """你是资深A股基本面分析师，严谨、只看数据不吹票。
基于估值(PE/PB)、盈利(ROE/毛利率/净利率)、成长(营收/净利同比)、财务健康(负债率)判断价值。
输出紧凑 JSON（每条要点≤25字，最多3条）：
{"score":-100~100整数,"stance":"看多|中性|看空","valuation":"估值高低一句话",
 "highlights":["亮点"],"risks":["风险"],"reasoning":"≤80字逻辑",
 "terms":["本次涉及的关键术语名2-4个,如 PE、ROE"]}"""

def analyst_fundamental(ctx):
    q = ctx["quote"] or {}
    payload = {"名称": q.get("name"), "现价": q.get("price"), "PE_TTM": q.get("pe_ttm"),
               "PB": q.get("pb"), "总市值亿": q.get("total_mv_yi"),
               "财务主要指标_近4期": ctx["fundamentals"]}
    return llm.chat_json(FUNDAMENTAL_SYS, "标的数据：\n" + _j(payload), max_tokens=4000)


# ============ 分析师：技术面 ============
TECH_SYS = """你是A股技术分析师，信奉价格与量能，不预测只跟随趋势。
基于均线排列、MACD、RSI、KDJ、布林带、波动率、近期涨跌判断形态与买卖点。
输出紧凑 JSON（每条≤25字，最多3条）：
{"score":-100~100整数,"stance":"看多|中性|看空","pattern":"形态一句话",
 "entry_zone":"买入价格区间或'暂不介入'","stop_loss_hint":"技术止损位",
 "highlights":["信号"],"risks":["风险"],"reasoning":"≤80字",
 "terms":["涉及的技术指标名2-4个,如 MACD、均线"]}"""

def analyst_technical(ctx):
    payload = {"指标": ctx["indicators"], "量比": (ctx["quote"] or {}).get("vol_ratio"),
               "换手率": (ctx["quote"] or {}).get("turnover")}
    return llm.chat_json(TECH_SYS, "标的技术数据：\n" + _j(payload), max_tokens=4000)


# ============ 分析师：情绪 & 资金面 ============
SENTIMENT_SYS = """你是A股市场情绪与资金面分析师，专盯主力动向、市场热度与消息面。
结合主力/超大单/大单净流入、量比、换手率、新闻标题、大盘环境，判断资金进出与情绪冷热。
输出紧凑 JSON（每条≤25字，最多3条）：
{"score":-100~100整数,"stance":"看多|中性|看空","capital_flow":"主力动向一句话",
 "market_mood":"大盘情绪一句话","news_read":"新闻关键信息≤30字",
 "highlights":["信号"],"risks":["风险"],"reasoning":"≤80字",
 "terms":["涉及的概念名2-4个,如 主力资金、量比"]}"""

def analyst_sentiment(ctx):
    payload = {"资金流": ctx["money_flow"], "量比": (ctx["quote"] or {}).get("vol_ratio"),
               "换手率": (ctx["quote"] or {}).get("turnover"),
               "今日涨跌幅": (ctx["quote"] or {}).get("pct"),
               "大盘": ctx["index"], "新闻标题": [n["title"] for n in ctx["news"]]}
    return llm.chat_json(SENTIMENT_SYS, "标的情绪资金数据：\n" + _j(payload), max_tokens=4000)


# ============ 交易员 ============
TRADER_SYS = """你是交易委员会首席交易员。三位分析师(基本面/技术面/情绪资金面)给了研报。
像真实操盘手权衡三方(可能冲突)，给可执行决策。置信度低就该"观望"。
输出紧凑 JSON：
{"action":"买入|加仓|观望|减仓|卖出","confidence":0-100,
 "buy_zone":"买入区间或'不建议'","target_price":"目标价或null","stop_loss":"止损价",
 "horizon":"持有周期","consensus":"三方一致点≤30字","divergence":"分歧及取舍≤40字",
 "thesis":"≤80字核心逻辑","terms":["涉及概念2-3个"]}"""

def trader_decide(ctx, fund, tech, senti):
    payload = {"现价": (ctx["quote"] or {}).get("price"),
               "基本面研报": fund, "技术面研报": tech, "情绪资金研报": senti}
    return llm.chat_json(TRADER_SYS, "三份研报：\n" + _j(payload), model=llm.MODEL_STRONG, max_tokens=4500)


# ============ 风控官 ============
RISK_SYS = """你是交易委员会风控官，有一票否决权，天生保守，任务是保护本金。
审查交易员方案，识别其忽略的风险(估值过高/追高/流动性/财报暴雷/系统性风险)，
给建议仓位上限(占总资金%)、是否否决、硬性风控纪律。
输出紧凑 JSON（每条≤30字，最多3条）：
{"verdict":"通过|降级执行|否决","max_position_pct":0-100整数,
 "risk_flags":["风险点"],"discipline":["纪律"],
 "adjusted_advice":"最终调整意见≤40字","terms":["涉及概念2-3个,如 仓位管理、最大回撤"]}"""

def risk_review(ctx, trader):
    q = ctx["quote"] or {}
    payload = {"标的": q.get("name"), "现价": q.get("price"), "PE": q.get("pe_ttm"),
               "PB": q.get("pb"), "波动率年化%": ctx["indicators"].get("vol_annual_pct"),
               "交易员方案": trader}
    return llm.chat_json(RISK_SYS, "待审查方案：\n" + _j(payload), model=llm.MODEL_STRONG, max_tokens=4000)


# ============ 按需教学：单术语深讲（独立小调用，稳定不截断） ============
EXPLAIN_SYS = """你是耐心的A股投资导师，面向完全的新手。用大白话把一个金融/股票术语讲清楚。
要求：不堆术语、多打比方、结合A股实际。控制在 260 字内。
输出紧凑 JSON：
{"term":"术语","one_line":"一句话定义","detail":"通俗讲解(可含比方)",
 "how_to_use":"散户实战怎么用","pitfall":"新手最容易踩的坑一句话"}"""

def explain(term, context=""):
    """按需生成某个术语的教学卡片。context 可传当前个股情形让讲解更贴合。"""
    user = f"请讲解术语：{term}"
    if context:
        user += f"\n（结合当前情形：{context}）"
    return llm.chat_json(EXPLAIN_SYS, user, max_tokens=4000)


# ============ 首页：AI 市场精选（研究性，非荐股） ============
PICK_SYS = """你是A股市场扫描分析师。用户给你今日的异动榜单(涨幅榜/成交额榜等，含价、涨跌幅、换手率、成交额、市盈率)。
你的任务：从中挑出 4-6 只【值得进一步研究】的标的，给出研究理由与风险提示。
纪律：
- 这不是荐股，是"值得研究"的线索；对追高、连板、纯题材炒作、高换手高波动要明确警示。
- 优先挑逻辑更均衡的：涨幅温和放量、有成交额支撑、估值不极端的，比单纯暴涨更值得研究。
- reason 讲清"为什么值得看一眼"，risk 讲清"要警惕什么"，都要具体、≤28字。
输出紧凑 JSON：
{"market_note":"一句话今日市场情绪≤30字",
 "picks":[{"code":"代码","name":"名称","tag":"标签(如 趋势放量/量能活跃/超跌反弹/龙头),
           "heat":0-100热度,"reason":"值得研究的理由≤28字","risk":"风险提示≤28字"}]}"""

def ai_picks(gainers, others):
    seen, cand = set(), []
    for src in (gainers, others):
        for it in src:
            if it["code"] in seen:
                continue
            seen.add(it["code"])
            cand.append({k: it.get(k) for k in ("code", "name", "pct", "turnover",
                         "amount_yi", "pe")})
    payload = {"今日异动候选": cand[:22]}
    out = llm.chat_json(PICK_SYS, "榜单数据：\n" + _j(payload), model=llm.MODEL_STRONG, max_tokens=4500)
    if "picks" not in out:
        out = {"market_note": "", "picks": []}
    out["disclaimer"] = DISCLAIMER
    return out


# ============ 委员会总编排 ============
def run_committee(code, progress=None):
    def p(step, label):
        if progress:
            progress(step, label)

    p("gather", "抓取行情/财务/资金/新闻数据…")
    ctx = gather(code)
    if not ctx["quote"]:
        return {"error": f"未找到标的 {code} 的行情数据，请检查代码。"}

    # 三位分析师并行研判（MiMo 端点支持并发，省一半时间）
    p("fundamental", "基本面分析师研判估值/盈利/成长…")
    p("technical", "技术面分析师研判均线/量能/形态…")
    p("sentiment", "情绪资金面分析师研判主力/热度/消息…")
    res = llm.parallel([
        ("fundamental", lambda: analyst_fundamental(ctx)),
        ("technical", lambda: analyst_technical(ctx)),
        ("sentiment", lambda: analyst_sentiment(ctx)),
    ])
    fund, tech, senti = res["fundamental"], res["technical"], res["sentiment"]

    p("trader", "首席交易员综合三方研报做决策…")
    trader = trader_decide(ctx, fund, tech, senti)

    p("risk", "风控官审查方案、核定仓位…")
    risk = risk_review(ctx, trader)

    def sc(a):
        try: return float(a.get("score", 0))
        except: return 0
    composite = round((sc(fund) + sc(tech) + sc(senti)) / 3, 1)

    # 汇总本次出现的所有术语，供前端做「点击深讲」
    all_terms = []
    for a in (fund, tech, senti, trader, risk):
        for t in (a.get("terms") or []):
            if t and t not in all_terms:
                all_terms.append(t)

    return {
        "code": ctx["code"],
        "name": (ctx["quote"] or {}).get("name"),
        "quote": ctx["quote"],
        "indicators": ctx["indicators"],
        "kline_tail": ctx["kline_tail"],
        "composite_score": composite,
        "analysts": {"fundamental": fund, "technical": tech, "sentiment": senti},
        "trader": trader,
        "risk": risk,
        "terms": all_terms,
        "disclaimer": DISCLAIMER,
    }


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    out = run_committee(code, progress=lambda s, l: print(f"[{s}] {l}"))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
