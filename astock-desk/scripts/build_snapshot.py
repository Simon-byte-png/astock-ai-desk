#!/usr/bin/env python3
"""生成课堂模式行情快照。运行：python3 scripts/build_snapshot.py"""
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)

from lib import market  # noqa: E402

STOCKS = [
    "600519", "300750", "000858", "601318", "600036", "000333",
    "600276", "002594", "601166", "600887", "000651", "601888",
    "600030", "601398", "600900", "000001", "601012", "603288",
    "600309", "002415", "300059", "000725", "002475", "600048",
    "601668", "600050", "601919", "000063", "688981", "300760",
]


def fetch(code):
    quote = market.quote(code)
    klines = market.kline(code, "day", 250)
    fundamentals = market.fundamentals(code)
    if not quote or quote.get("classroom_mode") or len(klines) < 20:
        return code, None
    return code, {"quote": quote, "klines": klines, "fundamentals": fundamentals}


def main():
    market._SNAPSHOT = {}
    records = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch, code): code for code in STOCKS}
        for future in as_completed(futures):
            code, data = future.result()
            if data:
                records[code] = data
                print(f"saved {code} {data['quote'].get('name', '')}")
            else:
                print(f"skipped {code}")

    snapshot = {
        "generated_at": market._beijing_now().replace(microsecond=0).isoformat(),
        "quotes": {code: data["quote"] for code, data in records.items()},
        "klines": {code: data["klines"] for code, data in records.items()},
        "fundamentals": {code: data["fundamentals"] for code, data in records.items()},
        "indices": market.index_snapshot(),
        "movers": {
            key: market.market_movers(key, 20)
            for key in ("gainers", "decliners", "turnover", "amount")
        },
    }
    data_dir = os.path.join(APP_ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    target = os.path.join(data_dir, "snapshot.json")
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=data_dir, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(snapshot, handle, ensure_ascii=False, separators=(",", ":"))
        temporary = handle.name
    os.replace(temporary, target)
    print(f"snapshot ready: {target} ({len(records)}/{len(STOCKS)} stocks)")


if __name__ == "__main__":
    main()
