# -*- coding: utf-8 -*-
"""
A股 AI 交易委员会 —— Web 盯盘看板后端（stdlib，无第三方依赖）。
绑定 $HOST:$PORT（平台注入）。API：
  GET /                      看板页面
  GET /api/index             大盘指数快照
  GET /api/search?q=         股票搜索
  GET /api/quote?code=       实时报价 + 技术指标 + K线
  GET /api/committee?code=   [SSE] 召开委员会，流式推送进度与最终结论
  GET /api/explain?term=&context=   按需术语教学卡片
  GET /api/backtest?code=&strategy=  单策略回测
  GET /api/backtest_all?code=        三策略对比
"""
import os, json, threading, queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from lib import market, agents, backtest

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("ZAOCODE_PREVIEW_PORT") or 8000)
HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False, default=str))

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path in ("/", "/index.html"):
                return self._file("web/index.html", "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(200, "image/svg+xml",
                                  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><text y="26" font-size="26">🏛️</text></svg>')
            if path == "/api/index":
                return self._json({"index": market.index_snapshot()})
            if path == "/api/search":
                return self._json({"results": market.search(q.get("q", ""))})
            if path == "/api/quote":
                return self._quote(q.get("code", ""))
            if path == "/api/committee":
                return self._committee_sse(q.get("code", ""))
            if path == "/api/explain":
                return self._json(agents.explain(q.get("term", ""), q.get("context", "")))
            if path == "/api/backtest":
                return self._json(backtest.backtest(q.get("code", ""), q.get("strategy", "ma")))
            if path == "/api/backtest_all":
                return self._json(backtest.compare_all(q.get("code", "")))
            return self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def _file(self, rel, ctype):
        p = os.path.join(HERE, rel)
        if not os.path.exists(p):
            return self._json({"error": "file missing"}, 404)
        with open(p, "rb") as f:
            self._send(200, ctype, f.read())

    def _quote(self, code):
        if not code:
            return self._json({"error": "缺少 code"}, 400)
        qd = market.quote(code)
        ks = market.kline(code, "day", 120)
        closes = [k["close"] for k in ks]
        ind = market.indicators(closes, [k["high"] for k in ks], [k["low"] for k in ks]) if closes else {}
        status = market.data_status(qd, ks)
        return self._json({
            "quote": qd,
            "indicators": ind,
            "data_status": status,
            "server_time": market._beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            "kline_date_range": [ks[0]["date"], ks[-1]["date"]] if ks else None,
            "kline": [{"date": k["date"], "close": k["close"], "open": k["open"],
                       "high": k["high"], "low": k["low"]} for k in ks[-90:]],
        })

    def _committee_sse(self, code):
        """SSE：先推送若干 progress 事件，最后推送 result 事件。"""
        if not code:
            return self._json({"error": "缺少 code"}, 400)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.protocol_version = "HTTP/1.1"

        evq = queue.Queue()

        def worker():
            def progress(step, label):
                evq.put(("progress", {"step": step, "label": label}))
            try:
                out = agents.run_committee(code, progress=progress)
                evq.put(("result", out))
            except Exception as e:
                evq.put(("result", {"error": str(e)}))
            finally:
                evq.put(("__done__", None))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        while True:
            ev, data = evq.get()
            if ev == "__done__":
                break
            try:
                chunk = f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"A股AI交易委员会看板 运行在 http://{HOST}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
