"""Download TAIFEX TXO daily market CSV files into a local SQLite database."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from market_time import is_trading_day


BASE_URL = "https://www.taifex.com.tw/cht/3/"
FIELDS = {
    "交易日期": "trade_date",
    "契約": "symbol",
    "到期月份(週別)": "contract_month",
    "履約價": "strike",
    "買賣權": "option_type",
    "開盤價": "open",
    "最高價": "high",
    "最低價": "low",
    "收盤價": "close",
    "成交量": "volume",
    "結算價": "settlement_price",
    "未沖銷契約數": "open_interest",
    "最後最佳買價": "best_bid",
    "最後最佳賣價": "best_ask",
    "交易時段": "trading_session",
    "契約到期日": "contract_expiry",
}
REQUIRED = {"trade_date", "symbol", "contract_month", "strike", "option_type", "volume"}
NUMERIC = {
    "strike", "open", "high", "low", "close", "volume", "settlement_price",
    "open_interest", "best_bid", "best_ask",
}
STORE_COLUMNS = (
    "trade_date", "symbol", "contract_month", "contract_expiry", "strike", "option_type",
    "trading_session", "open", "high", "low", "close", "volume", "settlement_price",
    "open_interest", "best_bid", "best_ask", "source_json",
)
PRIMARY_KEY = ("trade_date", "contract_month", "contract_expiry", "strike", "option_type", "trading_session")


class DuplicateCSVPrimaryKey(ValueError):
    def __init__(self, primary_key: tuple, first_record: dict, duplicate_record: dict):
        self.record_key = dict(zip(PRIMARY_KEY, primary_key))
        self.source_records = [first_record, duplicate_record]
        super().__init__(f"CSV 來源含重複主鍵 {self.record_key}；原始列：{self.source_records!r}")

    def evidence(self) -> dict:
        return {"record_key": self.record_key, "source_records": self.source_records}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.execute("""
        CREATE TABLE IF NOT EXISTS txo_daily (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            contract_month TEXT NOT NULL,
            contract_expiry TEXT NOT NULL DEFAULT '',
            strike REAL NOT NULL,
            option_type TEXT NOT NULL,
            trading_session TEXT NOT NULL DEFAULT '',
            open REAL, high REAL, low REAL, close REAL,
            volume INTEGER NOT NULL,
            settlement_price REAL, open_interest INTEGER,
            best_bid REAL, best_ask REAL,
            source_json TEXT NOT NULL,
            PRIMARY KEY (trade_date, contract_month, contract_expiry, strike, option_type, trading_session)
        )
    """)
    return db


def clean_number(value: str | None, integer: bool = False):
    value = (value or "").strip().replace(",", "")
    if not value or value in {"-", "--", "N/A"}:
        return None
    if integer:
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"整數欄位格式無效：{value!r}") from exc
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError(f"整數欄位須為有限整數：{value!r}")
        if number < -(2**63) or number > 2**63 - 1:
            raise ValueError(f"整數欄位超出 SQLite 範圍：{value!r}")
        return int(number)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"數值必須為有限值：{value!r}")
    return number


def parse_csv(data: bytes) -> list[dict]:
    text = data.decode("cp950", errors="strict")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not any(name.strip().lstrip("\ufeff") for name in reader.fieldnames if name):
        raise ValueError("TAIFEX 回傳空白 CSV")
    normalized_names = [name.strip().lstrip("\ufeff") if name else name for name in reader.fieldnames]
    reader.fieldnames = normalized_names
    names = [(name, FIELDS.get(name)) for name in normalized_names if name]
    if not REQUIRED.issubset({dest for _, dest in names}):
        raise ValueError(f"CSV 欄位不符，收到欄位：{reader.fieldnames}")

    rows, primary_keys = [], {}
    for source in reader:
        if not any((value or "").strip() for value in source.values() if value is not None):
            continue
        if not source.get("交易日期"):
            raise ValueError(f"資料列缺少交易日期：{source}")
        row = {dest: (source.get(src) or "").strip() for src, dest in names if dest}
        if row.get("symbol", "").strip() != "TXO":
            continue
        row["trade_date"] = dt.date.fromisoformat(row["trade_date"].replace("/", "-")).isoformat()
        expiry = row.get("contract_expiry", "").strip()
        if expiry.isdigit() and len(expiry) == 8:
            expiry = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
        row["contract_expiry"] = expiry
        row["contract_month"] = row["contract_month"].strip()
        row["option_type"] = row["option_type"].strip()
        row["trading_session"] = row.get("trading_session", "").strip()
        for key in NUMERIC:
            raw = row.get(key, "")
            row[key] = clean_number(raw, integer=key in {"volume", "open_interest"})
        if row["strike"] is None or row["volume"] is None or not row["contract_month"] or not row["option_type"]:
            raise ValueError(f"資料列缺少必要欄位：{source}")
        row["source_json"] = json.dumps(source, ensure_ascii=False)
        primary_key = tuple(row[key] for key in (
            "trade_date", "contract_month", "contract_expiry", "strike", "option_type", "trading_session",
        ))
        if primary_key in primary_keys:
            raise DuplicateCSVPrimaryKey(primary_key, primary_keys[primary_key], source)
        primary_keys[primary_key] = source
        rows.append(row)
    return rows


def store(db: sqlite3.Connection, rows: list[dict]) -> int:
    columns = STORE_COLUMNS
    select = f"SELECT {','.join(columns)} FROM txo_daily WHERE " + " AND ".join(f"{key}=?" for key in PRIMARY_KEY)
    insert = f"INSERT INTO txo_daily ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
    db.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            values = tuple(row.get(column, "") for column in columns)
            key = tuple(row[name] for name in PRIMARY_KEY)
            existing = db.execute(select, key).fetchone()
            if existing is None:
                db.execute(insert, values)
                continue
            if tuple(existing) != values:
                existing_record = dict(zip(columns, existing))
                differences = {
                    name: {"database": existing_record[name], "incoming": row.get(name, "")}
                    for name in columns if existing_record[name] != row.get(name, "")
                }
                raise ValueError(
                    "重疊行情與 SQLite 既有資料不一致，拒絕覆寫："
                    f"record_key={dict(zip(PRIMARY_KEY, key))}; differences={differences}; "
                    f"database_source_record={existing_record['source_json']}; incoming_source_record={row.get('source_json', '')}"
                )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return len(rows)


def save_raw(raw_path: Path, data: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{raw_path.name}.", suffix=".tmp", dir=raw_path.parent)
    try:
        output = os.fdopen(fd, "wb")
        fd = None
        with output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temp_name, raw_path)
        except FileExistsError:
            if raw_path.read_bytes() != data:
                raise ValueError(f"原始 CSV 已存在且內容不同，拒絕覆寫或匯入：{raw_path}")
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


class Taifex:
    def __init__(self):
        self.opener = urllib.request.build_opener()
        self.headers = {"User-Agent": "txo-daily-downloader/1.0", "Referer": BASE_URL + "optDailyMarketView"}
        self.opener.open(BASE_URL + "optDailyMarketView", timeout=30).close()

    def csv(self, start: dt.date, end: dt.date) -> bytes:
        date_args = {
            "queryStartDate": start.strftime("%Y/%m/%d"),
            "queryEndDate": end.strftime("%Y/%m/%d"),
        }
        query = urllib.parse.urlencode(date_args)
        with self.opener.open(BASE_URL + "getOptcontractDl.do?" + query, timeout=30) as response:
            contracts = json.load(response).get("commodityList", [])
        txo = next((item for item in contracts if item.get("marketid", "").strip() == "TXO"), None)
        if not txo:
            raise RuntimeError(f"TAIFEX 未提供 {start} 至 {end} 的 TXO 契約")
        body = urllib.parse.urlencode({
            "down_type": "1", "commodity_id": txo["commodity_id"], "commodity_id2": "",
            **date_args,
        }).encode()
        request = urllib.request.Request(BASE_URL + "dlOptDataDown", data=body, headers=self.headers)
        with self.opener.open(request, timeout=60) as response:
            return response.read()

    def block_trades_csv(self, start: dt.date, end: dt.date) -> bytes:
        """TAIFEX 鉅額交易「議價申報成交」逐序列下載（Big5 CSV）。"""
        body = urllib.parse.urlencode({
            "queryStartDate": start.strftime("%Y/%m/%d"), "queryEndDate": end.strftime("%Y/%m/%d"),
        }).encode()
        headers = {**self.headers, "Referer": BASE_URL + "dlBargainOrderView"}
        request = urllib.request.Request(BASE_URL + "dlBargainOrderDown", data=body, headers=headers)
        with self.opener.open(request, timeout=60) as response:
            return response.read()


def chunks(start: dt.date, end: dt.date):
    current = start
    while current <= end:
        month_end = (current.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        chunk_end = min(month_end, end)
        yield current, chunk_end
        current = chunk_end + dt.timedelta(days=1)


def download(start: dt.date, end: dt.date, db_path: Path, raw_dir: Path) -> int:
    if start > end:
        raise ValueError("開始日期不可晚於結束日期")
    client = Taifex()
    total = 0
    db = connect(db_path)
    try:
        for first, last in chunks(start, end):
            data = client.csv(first, last)
            rows = parse_csv(data)
            outside = [row["trade_date"] for row in rows if not first.isoformat() <= row["trade_date"] <= last.isoformat()]
            if outside:
                raise ValueError(f"下載資料超出要求日期 {first} 至 {last}：{outside[0]}")
            if not rows and any(
                is_trading_day(first + dt.timedelta(days=offset))
                for offset in range((last - first).days + 1)
            ):
                raise ValueError(f"TAIFEX 對包含交易日的區間 {first} 至 {last} 回傳空行情")
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / f"{first:%Y%m%d}_{last:%Y%m%d}.csv"
            save_raw(raw_path, data)
            total += store(db, rows)
            print(f"{first} 至 {last}: {len(rows):,} 筆")
    finally:
        db.close()
    print(f"完成：共匯入 {total:,} 筆至 {db_path}")
    return total


def check(db_path: Path) -> int:
    if not db_path.exists():
        raise FileNotFoundError(f"資料庫不存在：{db_path}")
    with sqlite3.connect(db_path) as db:
        count, dates, bad_keys = db.execute("""
            SELECT COUNT(*), COUNT(DISTINCT trade_date),
                SUM(CASE WHEN trade_date='' OR symbol!='TXO' OR contract_month='' OR option_type='' THEN 1 ELSE 0 END)
            FROM txo_daily
        """).fetchone()
    if not count:
        raise ValueError("資料庫沒有 TXO 行情資料")
    if bad_keys:
        raise ValueError(f"完整性檢查失敗：{bad_keys} 筆資料缺少必要欄位")
    print(f"完整性檢查通過：{count:,} 筆，{dates} 個交易日期")
    return count


def self_check():
    fixture = "交易日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,交易時段,契約到期日\n2026/11/02,TXO,202611W1,40600,買權,10,12,9,11,4,11,7,10.5,11,一般,20261104\n".encode("cp950")
    rows = parse_csv(fixture)
    assert len(rows) == 1 and rows[0]["close"] == 11.0 and rows[0]["contract_expiry"] == "2026-11-04"
    with tempfile.TemporaryDirectory() as temp:
        db = connect(Path(temp) / "test.db")
        assert store(db, rows) == 1 and store(db, rows) == 1
        assert db.execute("SELECT COUNT(*) FROM txo_daily").fetchone()[0] == 1
        db.close()
    print("離線自我檢查通過：CSV 解析、SQLite 儲存與重複匯入")


def main():
    parser = argparse.ArgumentParser(description="下載 TAIFEX 臺指選擇權每日行情至 SQLite")
    parser.add_argument("--db", type=Path, default=Path("data/txo.sqlite3"), help="SQLite 路徑")
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("download", help="下載指定日期範圍")
    get.add_argument("--start", required=True, type=dt.date.fromisoformat)
    get.add_argument("--end", required=True, type=dt.date.fromisoformat)
    get.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    sub.add_parser("check", help="檢查資料庫完整性")
    sub.add_parser("self-check", help="離線自我檢查")
    args = parser.parse_args()
    try:
        if args.command == "download":
            download(args.start, args.end, args.db, args.raw_dir)
        elif args.command == "check":
            check(args.db)
        else:
            self_check()
    except Exception as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
