# -*- coding: utf-8 -*-
"""躬行 Praxis：金融教育模拟盘后端，零第三方依赖。"""
import json
import math
import os
import queue
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from lib import agents, backtest, market, portfolio
from lib.security import RateLimiter

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT") or os.environ.get("ZAOCODE_PREVIEW_PORT") or 8000)
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 64 * 1024
PUBLIC_ORIGIN = os.environ.get("ASTOCK_PUBLIC_ORIGIN", "").rstrip("/")
COOKIE_SECURE = PUBLIC_ORIGIN.startswith("https://")
AI_PATHS = {"/api/diag", "/api/ai_picks", "/api/committee", "/api/court",
            "/api/explain", "/api/gate", "/api/order", "/api/review", "/api/tts"}
AI_HOURLY_LIMIT = int(os.environ.get("ASTOCK_AI_HOURLY_LIMIT", "120"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _picks_cache = None
    _court_attempts = {}
    _court_lock = threading.Lock()
    _limiter = RateLimiter()
    _ai_slots = threading.BoundedSemaphore(4)
    _picks_lock = threading.Lock()

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def _admit(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return True
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._safe_error("不允许跨站调用", 403)
            return False
        origin = self.headers.get("Origin")
        expected = PUBLIC_ORIGIN or "http://" + self.headers.get("Host", "")
        if origin and origin != expected:
            self._safe_error("请求来源不匹配", 403)
            return False
        # Without an explicit public origin, refuse non-local Host values (DNS rebinding).
        host = urlparse("//" + self.headers.get("Host", "")).hostname
        expected_host = urlparse(PUBLIC_ORIGIN).netloc if PUBLIC_ORIGIN else None
        if (expected_host and self.headers.get("Host") != expected_host) or (
            not expected_host and host not in {"localhost", "127.0.0.1", "::1"}
        ):
            self._safe_error("请配置 ASTOCK_PUBLIC_ORIGIN 后再通过域名访问", 403)
            return False
        # Proxy headers are intentionally ignored; clients cannot forge a new rate-limit identity.
        peer = self.client_address[0]
        if not self._limiter.allow(("requests", peer), 180, 60):
            self._json({"error": "请求过于频繁，请稍后再试"}, 429, {"Retry-After": "60"})
            return False
        if path in AI_PATHS:
            if not self._limiter.allow(("ai", peer), 12, 60) or not self._limiter.allow(
                ("ai-global",), AI_HOURLY_LIMIT, 3600
            ):
                self._json({"error": "AI 调用已达频率上限，请稍后再试"}, 429, {"Retry-After": "60"})
                return False
        return True

    def _dispatch(self, callback):
        # Reject unread bodies by closing the connection, including oversized/invalid requests.
        self.close_connection = True
        try:
            if not self._admit():
                return
            expensive = urlparse(self.path).path in AI_PATHS
            if expensive and not self._ai_slots.acquire(blocking=False):
                return self._json({"error": "AI 正忙，请稍后重试"}, 503, {"Retry-After": "10"})
            try:
                return callback()
            finally:
                if expensive:
                    self._ai_slots.release()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except ValueError as exc:
            self._safe_error(str(exc), 400)

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

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
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; "
                         "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(
            code, "application/json; charset=utf-8",
            json.dumps(obj, ensure_ascii=False, default=str, allow_nan=False), extra=extra,
        )

    def _body_json(self):
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            raise ValueError("必须提供唯一的 Content-Length，不支持分块请求")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求类型必须是 application/json")
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("Content-Length 无效")
        if length < 0:
            raise ValueError("Content-Length 无效")
        if length == 0:
            return {}
        if length > MAX_BODY:
            raise OverflowError("请求体过大")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("请求体不完整")
        def invalid_constant(value):
            raise ValueError("不允许非有限数值")
        def finite_float(value):
            result = float(value)
            if not math.isfinite(result):
                raise ValueError("不允许非有限数值")
            return result
        try:
            body = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant, parse_float=finite_float)
        except (UnicodeDecodeError, ValueError):
            raise ValueError("请求体必须是合法 JSON")
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return body

    def _user_id(self):
        if hasattr(self, "_session_user"):
            return self._session_user
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        value = cookie.get("praxis_user")
        self._session_user = portfolio.ensure_user(value.value if value else None)
        return self._session_user

    @staticmethod
    def _cookie_header(user_id):
        return (
            f"praxis_user={user_id}; Path=/; HttpOnly; SameSite=Lax; "
            "Max-Age=31536000" + ("; Secure" if COOKIE_SECURE else "")
        )

    def _get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            if path in {"/api/quote", "/api/committee", "/api/court", "/api/backtest", "/api/backtest_all"}:
                query["code"] = market.normalize(query.get("code"))
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
        except Exception:
            self._safe_error("服务暂时不可用，请稍后重试", 500)

    def _post(self):
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
        except Exception:
            self._safe_error("服务暂时不可用，请稍后重试", 500)

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
        except Exception:
            result.update({"ok": False, "error": "模型连通性测试失败，请检查服务端配置"})
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
        with Handler._picks_lock:
            return self._cached_picks()

    def _cached_picks(self):
        now = time.time()
        cached = Handler._picks_cache
        if cached and now - cached[0] < 300:
            return self._json({**cached[1], "cached": True})
        gainers = market.market_movers("gainers", 16, exclude_limit=True)
        amount = market.market_movers("amount", 12)
        if not gainers and not amount:
            return self._json({"error": "暂时拉取不到行情榜单", "picks": []})
        result = agents.ai_picks(gainers, amount)
        Handler._picks_cache = (now, result)
        return self._json({**result, "cached": False})

    def _sse(self, worker, user_id=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        if user_id:
            self.send_header("Set-Cookie", self._cookie_header(user_id))
        self.end_headers()
        events = queue.Queue()

        def progress(step, label):
            events.put(("progress", {"step": step, "label": label}))

        def run():
            try:
                events.put(("result", worker(progress)))
            except Exception:
                events.put(("result", {"error": "生成失败，请检查模型配置或稍后重试"}))
            finally:
                events.put(("__done__", None))

        threading.Thread(target=run, daemon=True).start()
        connected = True
        while True:
            try:
                event, data = events.get(timeout=10)
            except queue.Empty:
                event, data = "heartbeat", {}
            if event == "__done__":
                break
            chunk = (
                f"event: {event}\n"
                f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            )
            if not connected:
                continue
            try:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                # Keep the admission slot until the model worker really finishes.
                connected = False

    def _committee_sse(self, code):
        if not code:
            return self._json({"error": "缺少 code"}, 400)
        return self._sse(lambda progress: agents.run_committee(code, progress))

    def _court_sse(self, code):
        if not code:
            return self._json({"error": "缺少 code"}, 400)

        user_id = self._user_id()
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
                "user_id": user_id,
                "choices": [item["id"] for item in full["bull"] + full["bear"]],
            }
            with Handler._court_lock:
                cutoff = time.time() - 3600
                Handler._court_attempts = {
                    key: value for key, value in Handler._court_attempts.items()
                    if value["created_at"] > cutoff
                }
                Handler._court_attempts[attempt_id] = secret_answer
                if len(Handler._court_attempts) > 512:
                    Handler._court_attempts.pop(next(iter(Handler._court_attempts)))
            full["attempt_id"] = attempt_id
            return full

        return self._sse(worker, user_id)

    def _court_verdict(self, body):
        attempt_id = str(body.get("attempt_id") or "")
        choice = str(body.get("choice") or "")
        user_id = self._user_id()
        with Handler._court_lock:
            answer = Handler._court_attempts.get(attempt_id)
            if answer and (answer["user_id"] != user_id or answer["created_at"] <= time.time() - 3600):
                answer = None
            if answer and choice not in answer["choices"]:
                return self._json({"error": "请选择本次庭审中的论据"}, 400)
            if answer:
                del Handler._court_attempts[attempt_id]
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
        portfolio.validate_order(body.get("code"), body.get("side"), body.get("qty"))
        if not isinstance(body.get("request_id"), str):
            raise ValueError("缺少订单编号，请刷新页面后重试")
        if not isinstance(body.get("research", {}), dict):
            raise ValueError("研究记录必须是对象")
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
            request_id=body["request_id"],
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


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._connections = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


def main():
    server = BoundedHTTPServer((HOST, PORT), Handler)
    print(f"躬行 Praxis 运行在 http://{HOST}:{PORT} · LLM {_llm_host()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
