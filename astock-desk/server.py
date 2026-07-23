# -*- coding: utf-8 -*-
"""躬行 Praxis：金融教育模拟盘后端，零第三方依赖。"""
import json
import os
import queue
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from lib import agents, backtest, market, portfolio

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("ZAOCODE_PREVIEW_PORT") or 8000)
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _picks_cache = None
    _court_attempts = {}
    _court_lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(
            code, "application/json; charset=utf-8",
            json.dumps(obj, ensure_ascii=False, default=str), extra=extra,
        )

    def _body_json(self):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("Content-Length 无效")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise OverflowError("请求体过大")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("请求体必须是合法 JSON")
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return body

    def _user_id(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        value = cookie.get("praxis_user")
        return portfolio.ensure_user(value.value if value else None)

    @staticmethod
    def _cookie_header(user_id):
        return (
            f"praxis_user={user_id}; Path=/; HttpOnly; SameSite=Lax; "
            "Max-Age=31536000"
        )

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            if path in ("/", "/index.html"):
                return self._file("web/index.html", "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(
                    200, "image/svg+xml",
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                    '<text y="26" font-size="26">🧭</text></svg>',
                )
            if path == "/health":
                return self._json({"ok": True, "service": "praxis"})
            if path == "/api/config":
                return self._config()
            if path == "/api/diag":
                return self._diag()
            if path == "/api/index":
                return self._json({"index": market.index_snapshot()})
            if path == "/api/movers":
                mover_type = query.get("type", "gainers")
                count = min(max(int(query.get("n", 18) or 18), 1), 40)
                return self._json({
                    "type": mover_type,
                    "title": market._MOVER_TITLE.get(mover_type, mover_type),
                    "session": market.market_session(),
                    "list": market.market_movers(mover_type, count),
                })
            if path == "/api/ai_picks":
                return self._ai_picks(force=query.get("f") == "1")
            if path == "/api/search":
                return self._json({"results": market.search(query.get("q", ""))})
            if path == "/api/quote":
                return self._quote(query.get("code", ""))
            if path == "/api/committee":
                return self._committee_sse(query.get("code", ""))
            if path == "/api/court":
                return self._court_sse(query.get("code", ""))
            if path == "/api/explain":
                return self._json(agents.explain(
                    query.get("term", ""), query.get("context", "")
                ))
            if path == "/api/backtest":
                return self._json(backtest.backtest(
                    query.get("code", ""), query.get("strategy", "ma")
                ))
            if path == "/api/backtest_all":
                return self._json(backtest.compare_all(query.get("code", "")))
            if path == "/api/portfolio":
                user_id = self._user_id()
                return self._json(
                    portfolio.valuation(user_id),
                    extra={"Set-Cookie": self._cookie_header(user_id)},
                )
            return self._json({"error": "not found", "path": path}, 404)
        except BrokenPipeError:
            pass
        except ValueError as exc:
            self._safe_error(str(exc), 400)
        except Exception as exc:
            self._safe_error(str(exc), 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body_json()
            if path == "/api/gate":
                return self._json(agents.gate_check(body.get("answers")))
            if path == "/api/order":
                return self._place_order(body)
            if path == "/api/court/verdict":
                return self._court_verdict(body)
            if path == "/api/review":
                return self._review()
            if path == "/api/tts":
                return self._tts(body)
            return self._json({"error": "not found", "path": path}, 404)
        except OverflowError as exc:
            self._safe_error(str(exc), 413)
        except ValueError as exc:
            self._safe_error(str(exc), 400)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._safe_error(str(exc), 500)

    def _safe_error(self, message, status):
        try:
            self._json({"error": message}, status)
        except Exception:
            pass

    def _file(self, relative_path, content_type):
        path = os.path.join(HERE, relative_path)
        if not os.path.isfile(path):
            return self._json({"error": "file missing"}, 404)
        with open(path, "rb") as handle:
            return self._send(200, content_type, handle.read())

    def _config(self):
        from lib import llm
        return self._json({
            "brand": "躬行 Praxis",
            "llm": llm.provider_status(),
            "features": {
                "market": True, "portfolio": True, "court": True,
                "review": True, "tts": bool(llm.STEP_KEY),
            },
        })

    def _diag(self):
        from lib import llm
        result = {"providers": llm.provider_status()}
        started = time.time()
        try:
            sample = llm.chat("你是连通性测试器", "只回复：正常", max_tokens=40)
            result.update({"ok": True, "sample": sample[:40]})
        except Exception as exc:
            result.update({"ok": False, "error": str(exc)})
        result["ms"] = int((time.time() - started) * 1000)
        return self._json(result)

    def _quote(self, code):
        if not code:
            return self._json({"error": "缺少 code"}, 400)
        quote = market.quote(code)
        klines = market.kline(code, "day", 120)
        closes = [item["close"] for item in klines]
        indicators = market.indicators(
            closes, [item["high"] for item in klines], [item["low"] for item in klines]
        ) if closes else {}
        return self._json({
            "quote": quote,
            "indicators": indicators,
            "data_status": market.data_status(quote, klines),
            "server_time": market._beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            "kline_date_range": [klines[0]["date"], klines[-1]["date"]] if klines else None,
            "kline": [
                {key: item[key] for key in ("date", "close", "open", "high", "low")}
                for item in klines[-90:]
            ],
        })

    def _ai_picks(self, force=False):
        now = time.time()
        cached = Handler._picks_cache
        if cached and not force and now - cached[0] < 300:
            return self._json({**cached[1], "cached": True})
        gainers = market.market_movers("gainers", 16, exclude_limit=True)
        amount = market.market_movers("amount", 12)
        if not gainers and not amount:
            return self._json({"error": "暂时拉取不到行情榜单", "picks": []})
        result = agents.ai_picks(gainers, amount)
        Handler._picks_cache = (now, result)
        return self._json({**result, "cached": False})

    def _sse(self, worker):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        events = queue.Queue()

        def progress(step, label):
            events.put(("progress", {"step": step, "label": label}))

        def run():
            try:
                events.put(("result", worker(progress)))
            except Exception as exc:
                events.put(("result", {"error": str(exc)}))
            finally:
                events.put(("__done__", None))

        threading.Thread(target=run, daemon=True).start()
        while True:
            event, data = events.get()
            if event == "__done__":
                break
            chunk = (
                f"event: {event}\n"
                f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            )
            try:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break

    def _committee_sse(self, code):
        if not code:
            return self._json({"error": "缺少 code"}, 400)
        return self._sse(lambda progress: agents.run_committee(code, progress))

    def _court_sse(self, code):
        if not code:
            return self._json({"error": "缺少 code"}, 400)

        def worker(progress):
            full = agents.run_court(code, progress)
            if full.get("error"):
                return full
            attempt_id = secrets.token_urlsafe(18)
            secret_answer = {
                "flaw_id": full.pop("flaw_id", ""),
                "flaw_type": full.pop("flaw_type", ""),
                "flaw_explain": full.pop("flaw_explain", ""),
                "created_at": time.time(),
            }
            with Handler._court_lock:
                cutoff = time.time() - 3600
                Handler._court_attempts = {
                    key: value for key, value in Handler._court_attempts.items()
                    if value["created_at"] > cutoff
                }
                Handler._court_attempts[attempt_id] = secret_answer
            full["attempt_id"] = attempt_id
            return full

        return self._sse(worker)

    def _court_verdict(self, body):
        attempt_id = str(body.get("attempt_id") or "")
        choice = str(body.get("choice") or "")
        with Handler._court_lock:
            answer = Handler._court_attempts.pop(attempt_id, None)
        if not answer:
            return self._json({"error": "本次庭审已过期，请重新开庭"}, 404)
        return self._json({
            "correct": choice == answer["flaw_id"],
            "your_choice": choice,
            "flaw_id": answer["flaw_id"],
            "flaw_type": answer["flaw_type"],
            "flaw_explain": answer["flaw_explain"],
        })

    def _place_order(self, body):
        gate = agents.gate_check(body.get("answers"))
        if not gate.get("passed"):
            return self._json({"error": "三问尚未通过", "gate": gate}, 422)
        user_id = self._user_id()
        result = portfolio.place_order(
            user_id=user_id,
            code=body.get("code", ""),
            side=body.get("side", ""),
            qty=body.get("qty", 0),
            answers=body.get("answers") or {},
            emotion=body.get("emotion", ""),
            research=body.get("research") or {},
        )
        result["gate"] = gate
        return self._json(
            result, extra={"Set-Cookie": self._cookie_header(user_id)}
        )

    def _review(self):
        user_id = self._user_id()
        data = portfolio.valuation(user_id)
        letter = agents.review_letter(list(reversed(data["decisions"])), data["positions"])
        return self._json(
            {"review": letter, "generated_at": market._beijing_now().isoformat()},
            extra={"Set-Cookie": self._cookie_header(user_id)},
        )

    def _tts(self, body):
        from lib import llm
        audio = llm.tts(body.get("text", ""), body.get("voice"))
        return self._send(
            200, "audio/mpeg", audio,
            extra={"Cache-Control": "private, max-age=3600"},
        )


def _llm_host():
    from lib import llm
    status = llm.provider_status()
    primary = status["primary"]
    if primary["configured"]:
        return primary["host"] + " / " + primary["fast_model"]
    backup = status["backup"]
    if backup["configured"]:
        return backup["host"] + " / " + backup["model"] + " (backup)"
    return "not configured"


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"躬行 Praxis 运行在 http://{HOST}:{PORT} · LLM {_llm_host()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
