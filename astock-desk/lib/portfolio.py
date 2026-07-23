# -*- coding: utf-8 -*-
"""模拟盘存储。只用 Python 标准库 SQLite，适合挂载 Zeabur 持久卷。"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime

from . import market

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("ASTOCK_DATA_DIR", os.path.join(APP_ROOT, "data"))
DB_PATH = os.path.join(DATA_DIR, "praxis.db")
INITIAL_CASH = 100000.0
_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def _now():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _connect():
    global _INITIALIZED
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=12)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if not _INITIALIZED:
        with _INIT_LOCK:
            if not _INITIALIZED:
                _init_schema(conn)
                _INITIALIZED = True
    return conn


def _init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        cash REAL NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS positions (
        user_id TEXT NOT NULL,
        code TEXT NOT NULL,
        qty INTEGER NOT NULL,
        avg_cost REAL NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, code),
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        side TEXT NOT NULL,
        qty INTEGER NOT NULL,
        price REAL NOT NULL,
        answers_json TEXT NOT NULL,
        emotion TEXT,
        research_json TEXT,
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_decisions_user_time
        ON decisions(user_id, id DESC);
    """)
    conn.commit()


def new_user_id():
    return uuid.uuid4().hex


def _valid_user_id(user_id):
    value = str(user_id or "")
    return len(value) == 32 and all(ch in "0123456789abcdef" for ch in value.lower())


def ensure_user(user_id=None):
    if not _valid_user_id(user_id):
        user_id = new_user_id()
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id, cash, created_at) VALUES(?,?,?)",
            (user_id, INITIAL_CASH, _now()),
        )
    return user_id


def _decode_decision(row):
    item = dict(row)
    for column, target in (("answers_json", "answers"), ("research_json", "research")):
        raw = item.pop(column, "") or ""
        try:
            item[target] = json.loads(raw)
        except ValueError:
            item[target] = {} if target == "research" else []
    return item


def load(user_id):
    user_id = ensure_user(user_id)
    with _connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        positions = [
            dict(row) for row in conn.execute(
                "SELECT code, qty, avg_cost, updated_at FROM positions "
                "WHERE user_id=? ORDER BY code", (user_id,),
            ).fetchall()
        ]
        decisions = [
            _decode_decision(row) for row in conn.execute(
                "SELECT id, created_at, code, name, side, qty, price, "
                "answers_json, emotion, research_json FROM decisions "
                "WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,),
            ).fetchall()
        ]
    return {
        "user_id": user_id,
        "cash": round(float(user["cash"]), 2),
        "positions": positions,
        "decisions": decisions,
    }


def valuation(user_id):
    data = load(user_id)
    market_value = 0.0
    cost_value = 0.0
    enriched = []
    for position in data["positions"]:
        quote = market.quote(position["code"]) or {}
        current = float(quote.get("price") or position["avg_cost"])
        value = current * position["qty"]
        cost = position["avg_cost"] * position["qty"]
        market_value += value
        cost_value += cost
        enriched.append({
            **position,
            "name": quote.get("name") or position["code"],
            "current_price": round(current, 3),
            "market_value": round(value, 2),
            "pnl": round(value - cost, 2),
            "pnl_pct": round((current / position["avg_cost"] - 1) * 100, 2)
            if position["avg_cost"] else 0,
            "quote_as_of": quote.get("as_of", ""),
        })
    total_assets = data["cash"] + market_value
    return {
        **data,
        "positions": enriched,
        "market_value": round(market_value, 2),
        "total_assets": round(total_assets, 2),
        "total_pnl": round(total_assets - INITIAL_CASH, 2),
        "total_pnl_pct": round((total_assets / INITIAL_CASH - 1) * 100, 2),
        "initial_cash": INITIAL_CASH,
        "cost_value": round(cost_value, 2),
    }


def place_order(user_id, code, side, qty, answers, emotion="", research=None):
    user_id = ensure_user(user_id)
    code = market.normalize(code)
    side = str(side or "").lower()
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        raise ValueError("数量必须是整数")
    if side not in ("buy", "sell"):
        raise ValueError("side 只能是 buy 或 sell")
    if qty <= 0 or qty > 1000000:
        raise ValueError("数量必须在 1 到 1,000,000 之间")
    if side == "buy" and qty % 100 != 0:
        raise ValueError("A股模拟买入数量需为100股的整数倍")

    quote = market.quote(code)
    if not quote or float(quote.get("price") or 0) <= 0:
        raise ValueError("暂时取不到有效成交价，请稍后再试")
    price = float(quote["price"])
    amount = round(price * qty, 2)

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT cash FROM users WHERE user_id=?", (user_id,)).fetchone()
        position = conn.execute(
            "SELECT qty, avg_cost FROM positions WHERE user_id=? AND code=?",
            (user_id, code),
        ).fetchone()
        old_qty = int(position["qty"]) if position else 0
        old_cost = float(position["avg_cost"]) if position else 0.0
        cash = float(user["cash"])

        if side == "buy":
            if amount > cash + 1e-6:
                raise ValueError(f"模拟资金不足，本次需要 {amount:.2f} 元")
            new_qty = old_qty + qty
            avg_cost = (old_qty * old_cost + amount) / new_qty
            cash -= amount
            conn.execute(
                "INSERT INTO positions(user_id,code,qty,avg_cost,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id,code) DO UPDATE SET "
                "qty=excluded.qty,avg_cost=excluded.avg_cost,updated_at=excluded.updated_at",
                (user_id, code, new_qty, avg_cost, _now()),
            )
        else:
            if qty > old_qty:
                raise ValueError(f"模拟持仓不足，当前只有 {old_qty} 股")
            new_qty = old_qty - qty
            cash += amount
            if new_qty:
                conn.execute(
                    "UPDATE positions SET qty=?,updated_at=? WHERE user_id=? AND code=?",
                    (new_qty, _now(), user_id, code),
                )
            else:
                conn.execute(
                    "DELETE FROM positions WHERE user_id=? AND code=?", (user_id, code)
                )

        conn.execute("UPDATE users SET cash=? WHERE user_id=?", (cash, user_id))
        conn.execute(
            "INSERT INTO decisions(user_id,created_at,code,name,side,qty,price,"
            "answers_json,emotion,research_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, _now(), code, quote.get("name", ""), side, qty, price,
                json.dumps(answers, ensure_ascii=False),
                str(emotion or "")[:20],
                json.dumps(research or {}, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    result = valuation(user_id)
    result["order"] = {
        "code": code, "name": quote.get("name"), "side": side,
        "qty": qty, "price": price, "amount": amount,
    }
    return result
