import http.client
import io
import json
import math
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
from lib import backtest, llm, market, portfolio
from lib.security import RateLimiter

ANSWERS = dict(why_company='我想核验最近三年的经营现金流是否持续改善',
               evidence_against='如果利润增长但现金流持续恶化就推翻判断',
               loss_plan='限制模拟仓位并复盘证据与实际情况的差距')
QUOTE = dict(code='600519', name='测试公司', price=10, as_of='2026-10-22')


class TemporaryDatabase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patchers = [patch.object(portfolio, 'DATA_DIR', self.temp.name),
                         patch.object(portfolio, 'DB_PATH', os.path.join(self.temp.name, 'test.db')),
                         patch.object(portfolio, '_INITIALIZED', False)]
        for p in self.patchers:
            p.start()
        self.user = portfolio.ensure_user()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.temp.cleanup()


class OrderSafetyTests(TemporaryDatabase):
    def order(self, **kwargs):
        args = dict(user_id=self.user, code='600519', side='buy', qty=100,
                    answers=ANSWERS, request_id='test-order-123456789')
        args.update(kwargs)
        return portfolio.place_order(**args)

    def test_unknown_session_cannot_choose_its_own_identifier(self):
        self.assertNotEqual(portfolio.ensure_user('a' * 32), 'a' * 32)
        self.assertEqual(portfolio.ensure_user(self.user), self.user)
        self.assertNotIn('user_id', portfolio.load(self.user))

    def test_sequential_replay_does_not_need_market(self):
        with patch.object(market, 'quote', return_value=QUOTE):
            first = self.order()
        with patch.object(market, 'quote', side_effect=AssertionError('must not fetch again')):
            second = self.order()
        self.assertEqual(first['order'], second['order'])
        self.assertTrue(second['replayed'])
        self.assertEqual(second['cash'], 99000)
        self.assertEqual(len(second['decisions']), 1)

    def test_concurrent_replay_only_fills_once(self):
        barrier = threading.Barrier(2)
        def quote(code):
            barrier.wait(timeout=5)
            return QUOTE
        with patch.object(market, 'quote', side_effect=quote), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.order) for _ in range(2)]
            results = [f.result(timeout=10) for f in futures]
        self.assertEqual(sum(not r['replayed'] for r in results), 1)
        self.assertEqual(portfolio.load(self.user)['cash'], 99000)
        self.assertEqual(len(portfolio.load(self.user)['decisions']), 1)

    def test_same_id_different_order_rejected(self):
        with patch.object(market, 'quote', return_value=QUOTE):
            self.order()
            with self.assertRaisesRegex(ValueError, '不同订单'):
                self.order(qty=200)
        self.assertEqual(portfolio.load(self.user)['cash'], 99000)

    def test_fractional_boolean_and_nonfinite_quantities_rejected(self):
        for qty in [100.5, True, float('nan'), float('inf'), '100.5', -100]:
            with self.subTest(qty=qty), self.assertRaises(ValueError):
                self.order(qty=qty)

    def test_nonfinite_or_zero_price_never_changes_cash(self):
        for price in [0, -1, float('nan'), float('inf')]:
            with self.subTest(price=price), patch.object(market, 'quote', return_value={**QUOTE, 'price': price}):
                with self.assertRaises(ValueError):
                    self.order()
        self.assertEqual(portfolio.load(self.user)['cash'], 100000)
        self.assertEqual(portfolio.load(self.user)['decisions'], [])

    def test_insufficient_cash_or_position_rolls_back(self):
        with patch.object(market, 'quote', return_value=QUOTE):
            for args in [dict(qty=20000), dict(side='sell')]:
                with self.assertRaises(ValueError):
                    self.order(**args)
        self.assertEqual(portfolio.load(self.user)['cash'], 100000)
        self.assertEqual(portfolio.load(self.user)['decisions'], [])

    def test_existing_database_is_upgraded_without_losing_users(self):
        import sqlite3
        with sqlite3.connect(portfolio.DB_PATH) as conn:
            conn.execute('DROP TABLE order_requests')
            conn.execute('UPDATE users SET cash=90000 WHERE user_id=?', (self.user,))
        portfolio._INITIALIZED = False
        with patch.object(market, 'quote', return_value=QUOTE):
            result = self.order()
        self.assertEqual(result['cash'], 89000)
        self.assertEqual(portfolio.ensure_user(self.user), self.user)

    def test_snapshot_fill_is_explicit(self):
        with patch.object(market, 'quote', return_value={**QUOTE, 'classroom_mode': True}):
            self.assertTrue(self.order()['order']['classroom_mode'])


class MarketAndBacktestTests(unittest.TestCase):
    def test_tls_verification_enabled(self):
        self.assertTrue(market._CTX.check_hostname)
        self.assertEqual(market._CTX.verify_mode, ssl.CERT_REQUIRED)

    def test_codes_are_strict(self):
        for code in ['600519', 'sh600519', '600519.SH']:
            self.assertEqual(market.normalize(code), '600519')
        for code in ['', None, '600519&input=abc', '1234567', 'a600519', '６００５１９']:
            with self.assertRaises(ValueError):
                market.normalize(code)

    def test_snapshot_metadata_survives_list_fallback(self):
        snapshot = dict(generated_at='2026-01-01', klines={'600519': [{'date': '2026-01-01', 'close': 10}]})
        with patch.object(market, '_SNAPSHOT', snapshot), patch.object(market, '_kline_sina', return_value=[]), patch.object(market, '_kline_em', return_value=[]):
            bars = market.kline('600519')
        self.assertTrue(bars[0]['classroom_mode'])
        self.assertTrue(market.data_status(QUOTE, bars)['classroom_mode'])
        self.assertNotIn('classroom_mode', snapshot['klines']['600519'][0])

    def test_signal_cannot_capture_same_day_price_jump(self):
        metrics, curve = backtest._run_strategy([10, 100, 100, 100], ['a', 'b', 'c', 'd'], [0, 1, 1, 1])
        self.assertEqual(curve[:2], [1, 1])
        self.assertLess(metrics['total_return_pct'], 0)

    def test_flat_price_trade_is_loss_after_fees(self):
        m, curve = backtest._run_strategy([10] * 4, ['a', 'b', 'c', 'd'], [1] * 4)
        expected = (1 - backtest.COMM - backtest.STAMP) / (1 + backtest.COMM)
        self.assertAlmostEqual(curve[-1], expected)
        self.assertEqual(m['trades'], 1)
        self.assertEqual(m['win_rate_pct'], 0)
        self.assertEqual(m['total_return_pct'], round((expected - 1) * 100, 2))

    def test_no_last_day_buy_and_same_day_liquidation(self):
        m, curve = backtest._run_strategy([10] * 3, ['a', 'b', 'c'], [0, 1, 1])
        self.assertEqual(curve, [1, 1, 1])
        self.assertEqual(m['trades'], 0)

    def test_benchmark_includes_fees_and_snapshot_notice(self):
        bars = [dict(date=f'2026-{i:03}', close=10, classroom_mode=True) for i in range(40)]
        with patch.object(market, 'kline', return_value=bars):
            result = backtest.backtest('600519')
        self.assertLess(result['benchmark']['total_return_pct'], 0)
        self.assertTrue(result['classroom_mode'])
        self.assertIn('课堂快照', result['note'])

    def test_invalid_strategy_rejected_before_network(self):
        with patch.object(market, 'kline', side_effect=AssertionError('no network')):
            with self.assertRaises(ValueError):
                backtest.backtest('600519', 'unknown')

    def test_nonfinite_history_rejected(self):
        with patch.object(market, 'kline', return_value=[dict(close=float('nan'))] * 40):
            self.assertIn('error', backtest.backtest('600519'))


class LlmAndRateLimitTests(unittest.TestCase):
    def test_provider_error_does_not_expose_body_or_url(self):
        error = urllib.error.HTTPError('https://secret.example', 401, 'secret-key', {}, io.BytesIO(b'secret-key'))
        self.assertEqual(llm._error_text(error), 'HTTP 401')
        self.assertNotIn('secret', llm._error_text(RuntimeError('secret-key')))

    def test_redirect_rejected(self):
        with self.assertRaises(RuntimeError):
            llm._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example')

    def test_insecure_provider_url_rejected(self):
        with self.assertRaises(ValueError):
            llm._open(urllib.request.Request('http://example.com'), 1)

    def test_limit_expires_and_memory_is_bounded(self):
        limiter = RateLimiter(capacity=2)
        with patch('lib.security.time.monotonic', return_value=0):
            self.assertTrue(limiter.allow('a', 1, 60))
            self.assertFalse(limiter.allow('a', 1, 60))
            self.assertTrue(limiter.allow('b', 1, 60))
            self.assertFalse(limiter.allow('c', 1, 60))
        with patch('lib.security.time.monotonic', return_value=61):
            self.assertTrue(limiter.allow('c', 1, 60))
        self.assertLessEqual(len(limiter.entries), 2)


class HttpSafetyTests(TemporaryDatabase):
    def setUp(self):
        super().setUp()
        self.http_patchers = [patch.object(server, 'PUBLIC_ORIGIN', ''),
                              patch.object(server, 'COOKIE_SECURE', False),
                              patch.object(server.Handler, '_limiter', RateLimiter()),
                              patch.object(server.Handler, '_court_attempts', {})]
        for p in self.http_patchers:
            p.start()
        self.httpd = server.BoundedHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_port

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        for p in reversed(self.http_patchers):
            p.stop()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        headers = {'Content-Type': 'application/json', **(headers or {})}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        conn.close()
        return result

    def test_health_and_security_headers(self):
        status, headers, body = self.request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_cross_site_rejected_before_ai(self):
        with patch.object(server.agents, 'gate_check', side_effect=AssertionError('must not run')):
            status, _, _ = self.request('POST', '/api/gate', '{}', {'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)

    def test_fetch_metadata_blocks_cross_site_get(self):
        status, _, _ = self.request('GET', '/api/diag', headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(status, 403)

    def test_dns_rebinding_host_rejected(self):
        status, _, _ = self.request('GET', '/api/portfolio', headers={'Host': 'evil.example'})
        self.assertEqual(status, 403)

    def test_public_origin_and_secure_cookie(self):
        with patch.object(server, 'PUBLIC_ORIGIN', 'https://desk.example'), patch.object(server, 'COOKIE_SECURE', True):
            status, headers, body = self.request('GET', '/api/portfolio', headers={'Host': 'desk.example', 'Origin': 'https://desk.example'})
        self.assertEqual(status, 200)
        self.assertIn('; Secure', headers['Set-Cookie'])
        self.assertNotIn('user_id', json.loads(body))

    def test_wrong_content_type_and_nonfinite_json_rejected(self):
        for body, headers in [('{}', {'Content-Type': 'text/plain'}), ('{"qty":NaN}', {}), ('{"qty":1e999}', {}), ('[]', {})]:
            with self.subTest(body=body, headers=headers):
                self.assertEqual(self.request('POST', '/api/gate', body, headers)[0], 400)

    def test_oversized_body_rejected_and_connection_closed(self):
        status, headers, _ = self.request('POST', '/api/gate', '{}' * 40000)
        self.assertEqual(status, 413)
        self.assertEqual(headers['Connection'], 'close')

    def test_invalid_code_returns_400_without_network(self):
        with patch.object(market, 'quote', side_effect=AssertionError('no network')):
            self.assertEqual(self.request('GET', '/api/quote?code=garbage')[0], 400)

    def test_backend_exception_is_not_returned(self):
        with patch.object(market, 'quote', side_effect=RuntimeError('private-key-and-path')):
            status, _, body = self.request('GET', '/api/quote?code=600519')
        self.assertEqual(status, 500)
        self.assertNotIn('private-key', body)

    def test_rate_limit_applies_even_to_diagnostic(self):
        with patch.object(server, 'AI_HOURLY_LIMIT', 1), patch.object(llm, 'chat', return_value='ok'):
            self.assertEqual(self.request('GET', '/api/diag')[0], 200)
            status, headers, _ = self.request('GET', '/api/diag')
        self.assertEqual(status, 429)
        self.assertIn('Retry-After', headers)

    def test_order_replay_over_http(self):
        body = json.dumps(dict(code='600519', side='buy', qty=100, answers=ANSWERS, request_id='http-order-12345678'))
        cookie = {'Cookie': 'praxis_user=' + self.user}
        with patch.object(market, 'quote', return_value=QUOTE), patch.object(server.agents, 'gate_check', return_value={'passed': True}):
            first = self.request('POST', '/api/order', body, cookie)
            second = self.request('POST', '/api/order', body, cookie)
        self.assertEqual(first[0], 200)
        self.assertTrue(json.loads(second[2])['replayed'])
        self.assertEqual(portfolio.load(self.user)['cash'], 99000)

    def test_court_answer_owner_expiry_and_one_use(self):
        server.Handler._court_attempts['attempt'] = dict(user_id=self.user, created_at=time.time(),
            flaw_id='bull-1', flaw_type='test', flaw_explain='test', choices=['bull-1'])
        body = json.dumps({'attempt_id': 'attempt', 'choice': 'bull-1'})
        self.assertEqual(self.request('POST', '/api/court/verdict', body)[0], 404)
        cookie = {'Cookie': 'praxis_user=' + self.user}
        self.assertEqual(self.request('POST', '/api/court/verdict', body, cookie)[0], 200)
        self.assertEqual(self.request('POST', '/api/court/verdict', body, cookie)[0], 404)
        server.Handler._court_attempts['expired'] = dict(user_id=self.user, created_at=time.time()-3601,
            flaw_id='bull-1', choices=['bull-1'])
        self.assertEqual(self.request('POST', '/api/court/verdict', json.dumps({'attempt_id': 'expired', 'choice': 'bull-1'}), cookie)[0], 404)

    def test_sse_closes_and_hides_answer(self):
        full = dict(bull=[{'id': 'bull-1', 'text': 'argument'}], bear=[], flaw_id='bull-1',
                    flaw_type='private-type', flaw_explain='private-answer')
        with patch.object(server.agents, 'run_court', return_value=full):
            status, headers, body = self.request('GET', '/api/court?code=600519', headers={'Cookie': 'praxis_user=' + self.user})
        self.assertEqual(status, 200)
        self.assertIn('event: result', body)
        self.assertIn('attempt_id', body)
        self.assertNotIn('private-answer', body)
        self.assertNotIn('flaw_id', body)
        self.assertEqual(headers['Connection'], 'close')


if __name__ == '__main__':
    unittest.main()
