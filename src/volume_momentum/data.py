from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TickerRecord:
    ticker: str
    name: str | None = None
    market: str | None = None


@dataclass(frozen=True)
class FetchResult:
    ticker: str
    source: str
    status: str
    start_date: str | None
    end_date: str | None
    rows_fetched: int
    message: str | None = None


@dataclass(frozen=True)
class NotificationRecord:
    ticker: str
    event_date: str
    signal_key: str
    notification_group: str


class DataError(RuntimeError):
    """Raised when data acquisition or cache operations fail."""


def configure_yfinance_cache(cache_dir: str | Path) -> None:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise DataError("yfinance is not installed. Install yfinance to fetch market data.") from exc

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(cache_path))


def connect_database(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS instruments (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            market TEXT,
            instrument_type TEXT NOT NULL DEFAULT 'stock',
            current_market_cap INTEGER,
            current_market_cap_as_of TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_bars (
            ticker TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            adj_close REAL,
            volume INTEGER,
            trading_value REAL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ticker, trade_date),
            FOREIGN KEY (ticker) REFERENCES instruments(ticker)
        );

        CREATE INDEX IF NOT EXISTS idx_price_bars_date
            ON price_bars (trade_date);

        CREATE TABLE IF NOT EXISTS fetch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            requested_start TEXT,
            requested_end TEXT,
            period TEXT,
            interval TEXT,
            status TEXT NOT NULL,
            rows_fetched INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            fetched_at TEXT NOT NULL,
            FOREIGN KEY (ticker) REFERENCES instruments(ticker)
        );

        CREATE TABLE IF NOT EXISTS notification_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            event_date TEXT NOT NULL,
            signal_key TEXT NOT NULL,
            notification_group TEXT NOT NULL,
            notified_at TEXT NOT NULL,
            UNIQUE (ticker, event_date, signal_key)
        );
        """
    )
    connection.execute(
        """
        INSERT INTO schema_meta (key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )
    connection.commit()


def load_universe_from_jpx(url: str) -> list[TickerRecord]:
    try:
        frame = pd.read_excel(url)
    except Exception as exc:
        raise DataError(f"Failed to load JPX listed issues from {url}: {exc}") from exc
    return parse_jpx_listed_issues(frame)


def parse_jpx_listed_issues(frame: pd.DataFrame) -> list[TickerRecord]:
    columns = {str(column).strip(): column for column in frame.columns}
    code_column = _find_column(columns, ["コード", "Code"])
    name_column = _find_column(columns, ["銘柄名", "銘柄名（英語）", "Name"])
    market_column = _find_column(columns, ["市場・商品区分", "Market and Product Segment", "市場区分"])
    records: list[TickerRecord] = []
    seen: set[str] = set()

    for row in frame.itertuples(index=False):
        raw = {str(column).strip(): value for column, value in zip(frame.columns, row)}
        code = _normalize_jpx_code(raw.get(str(code_column).strip()))
        if code is None:
            continue
        market = _blank_to_none(str(raw.get(str(market_column).strip(), "")).strip())
        if not _is_listed_stock_market(market):
            continue
        ticker = f"{code}.T"
        if ticker in seen:
            continue
        seen.add(ticker)
        records.append(
            TickerRecord(
                ticker=ticker,
                name=_blank_to_none(str(raw.get(str(name_column).strip(), "")).strip()),
                market=market,
            )
        )

    if not records:
        raise DataError("JPX listed issues did not contain any stock tickers")
    return records


def _find_column(columns: dict[str, object], candidates: list[str]) -> object:
    for candidate in candidates:
        if candidate in columns:
            return columns[candidate]
    raise DataError(f"JPX listed issues is missing one of columns: {', '.join(candidates)}")


def _normalize_jpx_code(value: object) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) != 4 or not text.isdigit():
        return None
    return text


def _is_listed_stock_market(market: str | None) -> bool:
    if not market:
        return False
    excluded = ["ETF", "ETN", "REIT", "ベンチャーファンド", "カントリーファンド", "インフラファンド", "出資証券"]
    if any(word in market for word in excluded):
        return False
    return "株式" in market


def upsert_instruments(
    connection: sqlite3.Connection,
    tickers: Iterable[TickerRecord],
    instrument_type: str = "stock",
) -> None:
    now = _utc_now()
    connection.executemany(
        """
        INSERT INTO instruments (
            ticker, name, market, instrument_type, updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            name = COALESCE(excluded.name, instruments.name),
            market = COALESCE(excluded.market, instruments.market),
            instrument_type = excluded.instrument_type,
            updated_at = excluded.updated_at
        """,
        [(record.ticker, record.name, record.market, instrument_type, now) for record in tickers],
    )
    connection.commit()


def upsert_market_caps(
    connection: sqlite3.Connection,
    market_caps: dict[str, int | None],
    as_of: str | None = None,
) -> None:
    now = _utc_now()
    cap_as_of = as_of or now[:10]
    rows = [
        (ticker, cap, cap_as_of, now)
        for ticker, cap in market_caps.items()
        if cap is not None
    ]
    connection.executemany(
        """
        INSERT INTO instruments (
            ticker, current_market_cap, current_market_cap_as_of, updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            current_market_cap = excluded.current_market_cap,
            current_market_cap_as_of = excluded.current_market_cap_as_of,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    connection.commit()


def normalize_price_frame(ticker: str, frame: pd.DataFrame, source: str = "yfinance") -> pd.DataFrame:
    if frame.empty:
        return _empty_price_frame()

    working = frame.copy()
    if isinstance(working.columns, pd.MultiIndex):
        if ticker in working.columns.get_level_values(-1):
            working = working.xs(ticker, axis=1, level=-1)
        elif ticker in working.columns.get_level_values(0):
            working = working.xs(ticker, axis=1, level=0)

    working = working.rename(columns={column: str(column).strip() for column in working.columns})
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required.difference(working.columns)
    if missing:
        raise DataError(f"Price data for {ticker} is missing columns: {', '.join(sorted(missing))}")

    adj_close = working["Adj Close"] if "Adj Close" in working.columns else working["Close"]
    normalized = pd.DataFrame(
        {
            "ticker": ticker,
            "trade_date": pd.to_datetime(working.index).date.astype(str),
            "open": working["Open"],
            "high": working["High"],
            "low": working["Low"],
            "close": working["Close"],
            "adj_close": adj_close,
            "volume": working["Volume"].fillna(0).astype("int64"),
            "source": source,
        }
    )
    normalized["trading_value"] = normalized["adj_close"] * normalized["volume"]
    return normalized.dropna(subset=["trade_date", "adj_close"]).reset_index(drop=True)


def upsert_price_bars(connection: sqlite3.Connection, price_bars: pd.DataFrame) -> int:
    if price_bars.empty:
        return 0

    now = _utc_now()
    rows = [
        (
            row.ticker,
            row.trade_date,
            _nullable_float(row.open),
            _nullable_float(row.high),
            _nullable_float(row.low),
            _nullable_float(row.close),
            _nullable_float(row.adj_close),
            int(row.volume) if pd.notna(row.volume) else None,
            _nullable_float(row.trading_value),
            row.source,
            now,
            now,
        )
        for row in price_bars.itertuples(index=False)
    ]
    connection.executemany(
        """
        INSERT INTO price_bars (
            ticker, trade_date, open, high, low, close, adj_close, volume,
            trading_value, source, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, trade_date) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            adj_close = excluded.adj_close,
            volume = excluded.volume,
            trading_value = excluded.trading_value,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    connection.commit()
    return len(rows)


def record_fetch_history(
    connection: sqlite3.Connection,
    result: FetchResult,
    period: str | None,
    interval: str | None,
    requested_start: str | None = None,
    requested_end: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO fetch_history (
            ticker, source, requested_start, requested_end, period, interval,
            status, rows_fetched, message, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.ticker,
            result.source,
            requested_start,
            requested_end,
            period,
            interval,
            result.status,
            result.rows_fetched,
            result.message,
            _utc_now(),
        ),
    )
    connection.commit()


def has_notification_history(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT COUNT(*) AS notification_count
        FROM notification_history
        """
    ).fetchone()
    return bool(row and int(row["notification_count"]) > 0)


def list_notified_event_keys(connection: sqlite3.Connection, signal_key: str | None = None) -> set[tuple[str, str]]:
    params: list[object] = []
    where_clause = ""
    if signal_key is not None:
        where_clause = "WHERE signal_key = ?"
        params.append(signal_key)
    rows = connection.execute(
        f"""
        SELECT ticker, event_date
        FROM notification_history
        {where_clause}
        """,
        params,
    ).fetchall()
    return {(str(row["ticker"]), str(row["event_date"])) for row in rows}


def record_notification_history(
    connection: sqlite3.Connection,
    records: Iterable[NotificationRecord],
    notified_at: str | None = None,
) -> int:
    rows = [
        (
            record.ticker,
            record.event_date,
            record.signal_key,
            record.notification_group,
            notified_at or _utc_now(),
        )
        for record in records
    ]
    if not rows:
        return 0
    cursor = connection.executemany(
        """
        INSERT INTO notification_history (
            ticker, event_date, signal_key, notification_group, notified_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ticker, event_date, signal_key) DO NOTHING
        """,
        rows,
    )
    connection.commit()
    return int(cursor.rowcount)


def get_cached_date_range(connection: sqlite3.Connection, ticker: str) -> tuple[str | None, str | None, int]:
    row = connection.execute(
        """
        SELECT MIN(trade_date) AS start_date,
               MAX(trade_date) AS end_date,
               COUNT(*) AS row_count
        FROM price_bars
        WHERE ticker = ?
        """,
        (ticker,),
    ).fetchone()
    if row is None:
        return None, None, 0
    return row["start_date"], row["end_date"], int(row["row_count"])


def get_latest_trade_date(connection: sqlite3.Connection, tickers: Sequence[str] | None = None) -> str | None:
    conditions = []
    params: list[object] = []
    if tickers:
        placeholders = ", ".join("?" for _ in tickers)
        conditions.append(f"ticker IN ({placeholders})")
        params.extend(tickers)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    row = connection.execute(
        f"""
        SELECT MAX(trade_date) AS latest_trade_date
        FROM price_bars
        {where_clause}
        """,
        params,
    ).fetchone()
    return None if row is None else row["latest_trade_date"]


def list_universe_tickers(connection: sqlite3.Connection, min_current_market_cap: int | None) -> list[str]:
    if min_current_market_cap is None:
        rows = connection.execute(
            """
            SELECT ticker
            FROM instruments
            WHERE instrument_type = 'stock'
            ORDER BY ticker
            """
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT ticker
            FROM instruments
            WHERE current_market_cap >= ?
              AND instrument_type = 'stock'
            ORDER BY ticker
            """,
            (min_current_market_cap,),
        ).fetchall()
    return [row["ticker"] for row in rows]


def list_instrument_tickers(connection: sqlite3.Connection, instrument_type: str) -> list[str]:
    rows = connection.execute(
        """
        SELECT ticker
        FROM instruments
        WHERE instrument_type = ?
        ORDER BY ticker
        """,
        (instrument_type,),
    ).fetchall()
    return [row["ticker"] for row in rows]


def count_instruments_missing_market_cap(connection: sqlite3.Connection, instrument_type: str = "stock") -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS missing_count
        FROM instruments
        WHERE instrument_type = ?
          AND current_market_cap IS NULL
        """,
        (instrument_type,),
    ).fetchone()
    return int(row["missing_count"])


def count_instruments(connection: sqlite3.Connection, instrument_type: str = "stock") -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS instrument_count
        FROM instruments
        WHERE instrument_type = ?
        """,
        (instrument_type,),
    ).fetchone()
    return int(row["instrument_count"])


def load_price_bars(
    connection: sqlite3.Connection,
    tickers: Sequence[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    conditions = []
    params: list[object] = []
    if tickers:
        placeholders = ", ".join("?" for _ in tickers)
        conditions.append(f"ticker IN ({placeholders})")
        params.extend(tickers)
    if start_date is not None:
        conditions.append("trade_date >= ?")
        params.append(start_date)
    if end_date is not None:
        conditions.append("trade_date <= ?")
        params.append(end_date)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT ticker, trade_date, open, high, low, close, adj_close, volume,
               trading_value, source
        FROM price_bars
        {where_clause}
        ORDER BY ticker, trade_date
    """
    return pd.read_sql_query(query, connection, params=params)


def fetch_prices_with_yfinance(
    ticker: str,
    period: str,
    interval: str,
    source: str = "yfinance",
    start: str | None = None,
    end: str | None = None,
) -> tuple[pd.DataFrame, FetchResult]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise DataError("yfinance is not installed. Install the optional data dependency to fetch prices.") from exc

    download_kwargs: dict[str, object] = {
        "tickers": ticker,
        "interval": interval,
        "auto_adjust": False,
        "progress": False,
        "threads": False,
    }
    if start is not None or end is not None:
        if start is not None:
            download_kwargs["start"] = start
        if end is not None:
            download_kwargs["end"] = end
    else:
        download_kwargs["period"] = period
    frame = yf.download(**download_kwargs)
    normalized = normalize_price_frame(ticker, frame, source=source)
    start_date = None if normalized.empty else str(normalized["trade_date"].min())
    end_date = None if normalized.empty else str(normalized["trade_date"].max())
    result = FetchResult(
        ticker=ticker,
        source=source,
        status="success" if not normalized.empty else "empty",
        start_date=start_date,
        end_date=end_date,
        rows_fetched=len(normalized),
        message=None if not normalized.empty else "No price rows returned",
    )
    return normalized, result


def fetch_current_market_cap_with_yfinance(ticker: str) -> int | None:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise DataError("yfinance is not installed. Install the optional data dependency to fetch market caps.") from exc

    ticker_obj = yf.Ticker(ticker)
    fast_info = ticker_obj.fast_info
    market_cap = fast_info.get("marketCap") or fast_info.get("market_cap")
    if market_cap is None:
        info = ticker_obj.get_info()
        market_cap = info.get("marketCap")
    return None if market_cap is None else int(market_cap)


def _empty_price_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "ticker",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "adj_close",
            "volume",
            "source",
            "trading_value",
        ]
    )


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _nullable_float(value: object) -> float | None:
    return None if pd.isna(value) else float(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
