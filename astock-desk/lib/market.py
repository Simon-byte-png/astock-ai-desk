# -*- coding: utf-8 -*-
"""
A股数据层：纯 urllib，无第三方依赖。
数据源：腾讯(实时报价) / 东方财富(K线、基本面、资金流、新闻)。
所有网络函数都做了容错，失败返回 None 或空结构，不抛异常打断上层。
"""
import urllib.request, urllib.parse, json, ssl, re, time, datetime


def _beijing_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_cache = {}
def _get(url, headers=None, timeout=8, ttl=0, encoding="utf-8", retries=3):
    key = url
    now = time.time()
    if ttl and key in _cache and now - _cache[key][0] < ttl:
        return _cache[key][1]
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
            r = urllib.request.urlopen(req, timeout=timeout, context=_CTX)
            raw = r.read()
            data = raw.decode(encoding, "ignore")
            if ttl:
                _cache[key] = (now, data)
            return data
        except Exception as e:
            last = e
            time.sleep(0.35 * (attempt + 1))
    return None

# ---------- 代码/市场判定 ----------
def normalize(code):
    """输入 600519 / sh600519 / 600519.SH → 返回 6位纯代码"""
    code = str(code).strip().upper().replace("SH", "").replace("SZ", "").replace("BJ", "")
    code = code.replace(".", "").strip()
    m = re.search(r"\d{6}", code)
    return m.group(0) if m else code

def market_of(code):
    """返回 'SH'/'SZ'/'BJ'"""
    c = normalize(code)
    if c[0] in "6" or c[:3] in ("900", "580") or c[:2] == "50" or c[:2] == "51":
        return "SH"
    if c[:2] in ("83", "87", "43", "88", "92"):
        return "BJ"
    return "SZ"

def secid(code):
    """东财 secid: 1.xxxxxx(沪) / 0.xxxxxx(深/北)"""
    c = normalize(code)
    return ("1." if market_of(c) == "SH" else "0.") + c

def tencent_code(code):
    c = normalize(code)
    mk = market_of(c)
    return ("sh" if mk == "SH" else "sz" if mk == "SZ" else "bj") + c

# ---------- 实时报价（腾讯，字段稳定无需缩放） ----------
def quote(code):
    tc = tencent_code(code)
    txt = _get(f"https://qt.gtimg.cn/q={tc}", headers={"Referer": "https://gu.qq.com"}, ttl=2, encoding="gbk")
    if not txt or "=" not in txt:
        return None
    try:
        payload = txt.split('="', 1)[1].rstrip('";\n')
        f = payload.split("~")
        def num(i, d=0.0):
            try: return float(f[i])
            except: return d
        return {
            "code": normalize(code),
            "name": f[1],
            "price": num(3),
            "prev_close": num(4),
            "open": num(5),
            "volume_lot": num(6),          # 成交量(手)
            "change": num(31),             # 涨跌额
            "pct": num(32),                # 涨跌幅 %
            "high": num(33),
            "low": num(34),
            "amount_wan": num(37),         # 成交额(万)
            "turnover": num(38),           # 换手率 %
            "pe_ttm": num(39),             # 市盈率 TTM
            "amplitude": num(43),          # 振幅 %
            "float_mv_yi": num(44),        # 流通市值(亿)
            "total_mv_yi": num(45),        # 总市值(亿)
            "pb": num(46),                 # 市净率
            "limit_up": num(47),
            "limit_down": num(48),
            "vol_ratio": num(49),          # 量比
            "avg_price": num(51),          # 均价
            "time": f[30] if len(f) > 30 else "",
            **_freshness(f[30] if len(f) > 30 else ""),
        }
    except Exception:
        return None


def _freshness(raw_ts):
    """解析腾讯时间戳(YYYYMMDDHHMMSS)，给出可读时间与新鲜度标记。
    退市/停牌股(如已退市的600001)行情会停留在很久以前，据此告警。"""
    out = {"time_str": "", "quote_date": "", "stale": False, "stale_days": 0, "as_of": ""}
    m = re.match(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", raw_ts or "")
    if not m:
        return out
    y, mo, d, hh, mm, ss = m.groups()
    out["time_str"] = f"{y}-{mo}-{d} {hh}:{mm}:{ss}"
    out["quote_date"] = f"{y}-{mo}-{d}"
    try:
        qd = datetime.date(int(y), int(mo), int(d))
        days = (_beijing_now().date() - qd).days
        out["stale_days"] = days
        out["stale"] = days >= 5          # 超过约一周无更新 → 疑似退市/停牌
        out["as_of"] = f"{y}-{mo}-{d} {hh}:{mm}"
    except Exception:
        pass
    return out

# ---------- 数据新鲜度 / 交易时段 ----------
def market_session():
    """返回当前A股交易时段状态：pre(未开盘)/morning/noon(午休)/afternoon/closed(收盘)/weekend。"""
    now = _beijing_now()
    if now.weekday() >= 5:
        return "weekend"
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 15:
        return "pre"
    if hm < 11 * 60 + 30:
        return "morning"
    if hm < 13 * 60:
        return "noon"
    if hm < 15 * 60:
        return "afternoon"
    return "closed"

def data_status(quote, kline_list):
    """综合报价与最后一根K线判断数据是否新鲜/疑似退市停牌。
    退市股(如600001)量价冻结、K线停在多年前，据此可靠告警。"""
    today = _beijing_now().date()
    st = {"session": market_session(), "server_date": today.isoformat(),
          "stale": False, "reason": "", "last_bar": "", "days_old": None,
          "as_of": (quote or {}).get("time_str", "")}
    if kline_list:
        last = kline_list[-1]["date"][:10]
        st["last_bar"] = last
        try:
            y, m, d = map(int, last.split("-"))
            days = (today - datetime.date(y, m, d)).days
            st["days_old"] = days
            if days >= 7:
                st["stale"] = True
                st["reason"] = f"最新K线停留在 {last}（{days}天前），该标的疑似已退市或长期停牌，数据不可用于交易参考"
        except Exception:
            pass
    q = quote or {}
    if not st["stale"] and (q.get("volume_lot", 0) == 0 and q.get("turnover", 0) == 0
                            and st["session"] in ("morning", "afternoon")):
        st["stale"] = True
        st["reason"] = "盘中零成交、零换手，疑似停牌"
    return st


# ---------- K线：新浪为主，东财兜底 ----------
_SINA_SCALE = {"day": 240, "60m": 60, "30m": 30, "15m": 15, "5m": 5}
_EM_KLT = {"day": 101, "week": 102, "month": 103, "60m": 60, "30m": 30, "15m": 15, "5m": 5}

def _kline_sina(code, period, limit):
    scale = _SINA_SCALE.get(period)
    if not scale:
        return []
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={tencent_code(code)}&scale={scale}&ma=no&datalen={limit}")
    txt = _get(url, headers={"Referer": "https://finance.sina.com.cn"}, ttl=30)
    if not txt:
        return []
    try:
        arr = json.loads(txt)
        out = []
        prev = None
        for r in arr:
            c = float(r["close"])
            out.append({
                "date": r["day"], "open": float(r["open"]), "close": c,
                "high": float(r["high"]), "low": float(r["low"]),
                "volume": float(r.get("volume", 0)), "amount": 0.0,
                "pct": round((c / prev - 1) * 100, 2) if prev else 0.0,
            })
            prev = c
        return out
    except Exception:
        return []

def _kline_em(code, period, limit, fq=1):
    klt = _EM_KLT.get(period, 101)
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid(code)}"
           f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           f"&klt={klt}&fqt={fq}&end=20500101&lmt={limit}")
    txt = _get(url, ttl=30, retries=2)
    if not txt:
        return []
    try:
        d = json.loads(txt).get("data") or {}
        out = []
        for line in d.get("klines", []):
            p = line.split(",")
            out.append({
                "date": p[0], "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "volume": float(p[5]), "amount": float(p[6]),
                "pct": float(p[7]) if len(p) > 7 else 0.0,
            })
        return out
    except Exception:
        return []

def kline(code, period="day", limit=250, fq=1):
    """日/分钟K线。新浪(不复权)优先、东财(前复权)兜底；周月线仅东财。"""
    if period in ("week", "month"):
        return _kline_em(code, period, limit, fq)
    ks = _kline_sina(code, period, limit)
    if len(ks) >= min(limit, 20):
        return ks
    em = _kline_em(code, period, limit, fq)
    return em if em else ks

# ---------- 主力资金流（东财，当日 + 简况） ----------
def money_flow(code):
    fields = "f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid(code)}&fields={fields}"
    txt = _get(url, ttl=10)
    if not txt:
        return None
    try:
        d = json.loads(txt).get("data") or {}
        def yi(v):
            try: return round(float(v) / 1e8, 2)
            except: return None
        return {
            "main_net_yi": yi(d.get("f62")),        # 主力净流入(亿)
            "main_net_pct": d.get("f184"),          # 主力净占比 %
            "super_net_yi": yi(d.get("f66")),       # 超大单净额
            "big_net_yi": yi(d.get("f72")),         # 大单净额
            "mid_net_yi": yi(d.get("f78")),         # 中单净额
            "small_net_yi": yi(d.get("f84")),       # 小单净额
        }
    except Exception:
        return None

# ---------- 基本面：财务主要指标（东财 F10） ----------
def fundamentals(code):
    c = normalize(code)
    secucode = c + ("." + market_of(c))
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_F10_FINANCE_MAINFINADATA&columns=ALL"
           f"&filter=(SECUCODE=%22{secucode}%22)&pageSize=4&sortColumns=REPORT_DATE&sortTypes=-1"
           "&source=HSF10&client=PC")
    txt = _get(url, ttl=3600)
    rows = []
    try:
        j = json.loads(txt) if txt else {}
        for r in (j.get("result") or {}).get("data", []) or []:
            rows.append({
                "report_date": (r.get("REPORT_DATE") or "")[:10],
                "eps": r.get("EPSJB"),                 # 基本每股收益
                "bps": r.get("BPS"),                   # 每股净资产
                "roe": r.get("ROEJQ"),                 # 净资产收益率(加权) %
                "gross_margin": r.get("XSMLL"),        # 销售毛利率 %
                "net_margin": r.get("XSJLL"),          # 销售净利率 %
                "revenue_yi": _yi(r.get("TOTALOPERATEREVE")),
                "revenue_yoy": r.get("TOTALOPERATEREVETZ"),   # 营收同比 %
                "profit_yi": _yi(r.get("PARENTNETPROFIT")),
                "profit_yoy": r.get("PARENTNETPROFITTZ"),     # 净利同比 %
                "debt_ratio": r.get("ZCFZL"),          # 资产负债率 %
            })
    except Exception:
        pass
    return rows

def _yi(v):
    try: return round(float(v) / 1e8, 2)
    except: return None

# ---------- 个股新闻（东财搜索，用于情绪面） ----------
def news(code, limit=8):
    c = normalize(code)
    param = {
        "uid": "", "keyword": c, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                  "pageIndex": 1, "pageSize": limit, "preTag": "", "postTag": ""}},
    }
    url = "https://search-api-web.eastmoney.com/search/jsonp?cb=x&param=" + urllib.parse.quote(json.dumps(param, ensure_ascii=False))
    txt = _get(url, headers={"Referer": "https://so.eastmoney.com/"}, ttl=600)
    out = []
    if not txt:
        return out
    try:
        body = txt[txt.find("(") + 1: txt.rfind(")")]
        j = json.loads(body)
        for it in (j.get("result") or {}).get("cmsArticleWebOld", []) or []:
            title = re.sub("<.*?>", "", it.get("title", "")).strip()
            out.append({"title": title, "date": (it.get("date") or "")[:16],
                        "source": it.get("mediaName", "")})
    except Exception:
        pass
    return out

# ---------- 搜索股票（按名称/代码） ----------
def search(keyword):
    url = ("https://searchapi.eastmoney.com/api/suggest/get?type=14&count=8&input="
           + urllib.parse.quote(str(keyword)))
    txt = _get(url, ttl=600)
    out = []
    try:
        j = json.loads(txt) if txt else {}
        for it in (j.get("QuotationCodeTable") or {}).get("Data", []) or []:
            if it.get("Classify") in ("AStock", "Index") or (it.get("MktNum") in ("0", "1")):
                if it.get("SecurityTypeName") in ("沪A", "深A", "京A", "指数", "科创板", "创业板"):
                    out.append({"code": it.get("Code"), "name": it.get("Name"),
                                "type": it.get("SecurityTypeName")})
    except Exception:
        pass
    return out

# ---------- 技术指标（纯 python 计算） ----------
def _ma(vals, n):
    return [round(sum(vals[i - n + 1:i + 1]) / n, 3) if i >= n - 1 else None for i in range(len(vals))]

def _ema(vals, n):
    out, k = [], 2 / (n + 1)
    for i, v in enumerate(vals):
        out.append(v if i == 0 else round(out[-1] + k * (v - out[-1]), 4))
    return out

def indicators(closes, highs=None, lows=None, vols=None):
    """输入收盘价序列(旧→新)，返回最新一组技术指标 + 若干序列供画图。"""
    n = len(closes)
    if n < 5:
        return {}
    ma = {p: _ma(closes, p)[-1] for p in (5, 10, 20, 60) if n >= p}
    # MACD
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    dif = [round(a - b, 4) for a, b in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    macd = round((dif[-1] - dea[-1]) * 2, 4)
    # RSI(14)
    def rsi(period=14):
        if n <= period: return None
        gains = losses = 0.0
        for i in range(n - period, n):
            ch = closes[i] - closes[i - 1]
            gains += max(ch, 0); losses += max(-ch, 0)
        if losses == 0: return 100.0
        rs = gains / losses
        return round(100 - 100 / (1 + rs), 1)
    # KDJ(9)
    kdj = None
    if highs and lows and n >= 9:
        lown = min(lows[-9:]); highn = max(highs[-9:])
        rsv = (closes[-1] - lown) / (highn - lown) * 100 if highn > lown else 50
        kdj = {"rsv": round(rsv, 1)}
    # 布林带(20,2)
    boll = None
    if n >= 20:
        window = closes[-20:]
        mb = sum(window) / 20
        std = (sum((x - mb) ** 2 for x in window) / 20) ** 0.5
        boll = {"mid": round(mb, 2), "up": round(mb + 2 * std, 2), "low": round(mb - 2 * std, 2)}
    # 波动率(年化，基于近60日日收益)
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(max(1, n - 60), n)]
    vol_ann = None
    if len(rets) > 5:
        mu = sum(rets) / len(rets)
        sd = (sum((r - mu) ** 2 for r in rets) / len(rets)) ** 0.5
        vol_ann = round(sd * (250 ** 0.5) * 100, 1)
    # 趋势排列
    trend = "震荡"
    if all(p in ma for p in (5, 20, 60)):
        if ma[5] > ma[20] > ma[60]: trend = "多头排列"
        elif ma[5] < ma[20] < ma[60]: trend = "空头排列"
    return {
        "price": closes[-1],
        "ma": ma,
        "trend": trend,
        "dif": round(dif[-1], 4), "dea": round(dea[-1], 4), "macd": macd,
        "macd_signal": "金叉/多头" if macd > 0 and dif[-1] > dea[-1] else ("死叉/空头" if macd < 0 else "中性"),
        "rsi14": rsi(14),
        "kdj": kdj,
        "boll": boll,
        "vol_annual_pct": vol_ann,
        "chg_5d": round((closes[-1] / closes[-6] - 1) * 100, 2) if n >= 6 else None,
        "chg_20d": round((closes[-1] / closes[-21] - 1) * 100, 2) if n >= 21 else None,
        "ma_series": {p: _ma(closes, p) for p in (5, 20, 60) if n >= p},
    }

# ---------- 大盘指数快照 ----------
def index_snapshot():
    txt = _get("https://qt.gtimg.cn/q=sh000001,sz399001,sz399006,sh000300", ttl=5, encoding="gbk")
    res = []
    if txt:
        for seg in txt.strip().split("\n"):
            if '="' not in seg: continue
            f = seg.split('="', 1)[1].rstrip('";').split("~")
            try:
                res.append({"name": f[1], "price": float(f[3]), "pct": float(f[32])})
            except Exception:
                pass
    return res


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    print("== quote =="); print(json.dumps(quote(code), ensure_ascii=False, indent=2))
    ks = kline(code, "day", 120)
    print("== kline ==", len(ks), "根, 最新:", ks[-1] if ks else None)
    closes = [k["close"] for k in ks]
    highs = [k["high"] for k in ks]; lows = [k["low"] for k in ks]
    print("== indicators =="); print(json.dumps(indicators(closes, highs, lows), ensure_ascii=False, indent=2))
    print("== money_flow =="); print(json.dumps(money_flow(code), ensure_ascii=False, indent=2))
    print("== fundamentals =="); print(json.dumps(fundamentals(code)[:2], ensure_ascii=False, indent=2))
    print("== news =="); print(json.dumps(news(code, 4), ensure_ascii=False, indent=2))
    print("== index =="); print(json.dumps(index_snapshot(), ensure_ascii=False, indent=2))
