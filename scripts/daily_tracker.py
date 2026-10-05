#!/usr/bin/env python3
"""楽天ウォッチリストの株価追跡、上岡式シグナル判定、メール、JSON更新。

環境変数または --mode で動作を分ける。
  morning  朝: 前夜の米国株、過熱警戒、ギャップダウン狙い
  evening  夕: 当日シグナル、登録来パフォーマンス上下
  weekend  週末: 30日棚卸し、週間騰落

株価取得に失敗した銘柄はスキップし、前回の価格を残す。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import smtplib
import ssl
import sys
import time
from collections import Counter
from datetime import date, datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from rakuten_pages import group_sort_key, ordered_groups  # noqa: E402

ROOT = SCRIPT_DIR.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
TRACKER_CSV = DATA_DIR / "watchlist_tracker.csv"
JST = ZoneInfo("Asia/Tokyo")

COLUMNS = [
    "code",
    "ticker",
    "asset_type",
    "name",
    "group_name",
    "market",
    "sub_id",
    "strategy",
    "source",
    "added_date",
    "base_price",
    "current_price",
    "change_pct",
    "week_change_pct",
    "day_change_pct",
    "gap_pct",
    "max_high_price",
    "min_low_price",
    "days_elapsed",
    "ma25",
    "ma25_dev",
    "rsi14",
    "volume_ratio",
    "price_date",
    "status",
    "signals",
    "last_updated",
    "fetch_status",
    "archived",
]

TAKE_PROFIT = 15.0
STOP_LOSS = -8.0
BOTTOM_RSI = 30.0
BOTTOM_DEV = -5.0
OVERHEAT_DEV = 10.0
VOLUME_SURGE = 2.0
BREAKOUT_VOLUME = 1.5
GAP_DOWN = -2.0
REVIEW_DAYS = 30
BATCH_SIZE = 25
BATCH_SLEEP_SEC = 1.5
REQUEST_GAP_SEC = 0.3
BATCH_FAIL_COOLDOWN_SEC = 8.0
RATE_LIMIT_SLEEP_SEC = 30.0
RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit", "rate limited")

STATUS_PRIORITY = [
    "利確目標到達",
    "損切り警戒",
    "うねり底値シグナル",
    "過熱警戒（寄り天・押し目待ち）",
    "棚卸し候補（30日経過）",
]

MODE_TITLE = {
    "morning": "朝レポート",
    "evening": "夕方レポート",
    "weekend": "週末棚卸し",
}

DEFAULT_PAGES_URL = "https://mnghfxiidm4ad-rgb.github.io/uneri-tracker/"


def clean_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("%", "").replace("＋", "+").replace("－", "-")
        if text == "" or text.lower() in {"nan", "none", "null", "-"}:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and not (
        isinstance(value, float) and (math.isnan(value) or math.isinf(value))
    )


def num_to_csv(value, digits: int = 4) -> str:
    number = clean_float(value)
    if number is None:
        return ""
    text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def parse_date(value) -> date | None:
    text = ("" if value is None else str(value)).strip()
    if not text:
        return None
    head = text[:10].replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def normalize_code(code: str) -> str:
    trans = str.maketrans("０１２３４５６７８９", "0123456789")
    return (code or "").strip().translate(trans).upper()


def to_ticker(asset_type: str, code: str) -> str:
    code = normalize_code(code)
    if not code:
        return ""
    if (asset_type or "").upper() == "STK":
        return f"{code}.T"
    return code


def chronicle_url(code: str) -> str:
    return f"https://stockchronicle.app/?code={quote(str(code).strip())}"


def _bare_symbol(value: str) -> str:
    text = (value or "").strip()
    if text.upper().endswith(".T"):
        return text[:-2]
    return text


def yahoo_quote_url(stock: dict) -> str:
    """日本株は Yahoo!ファイナンス、米国株・ETFは Yahoo Finance の銘柄ページ。"""
    asset = str(stock.get("asset_type") or "").strip().upper()
    code = _bare_symbol(str(stock.get("code") or ""))
    ticker = str(stock.get("ticker") or "").strip()
    symbol = code or _bare_symbol(ticker)
    if not symbol:
        return ""
    japanese = asset == "STK" or ticker.upper().endswith(".T")
    if asset == "USS":
        japanese = False
    if japanese:
        return f"https://finance.yahoo.co.jp/quote/{quote(symbol, safe='')}.T"
    us = _bare_symbol(ticker or code)
    if not us:
        return ""
    return f"https://finance.yahoo.com/quote/{quote(us, safe='')}"


def is_youtube(*parts) -> bool:
    text = " ".join("" if part is None else str(part) for part in parts).lower()
    return "youtube" in text or "ユーチューブ" in text


def is_youtube_tracking(group_name="", source="", strategy="") -> bool:
    """YouTube・新規グループ、または source/strategy に YouTube を含む場合。"""
    if str(group_name or "").strip() == "YouTube・新規":
        return True
    return is_youtube(source, strategy)


def fmt_pct(value) -> str:
    number = value if is_num(value) else clean_float(value)
    if number is None:
        return "—"
    return f"{number:+.2f}%"


def fmt_price(value) -> str:
    number = value if is_num(value) else clean_float(value)
    if number is None:
        return "—"
    if abs(number - round(number)) < 0.001:
        return f"{number:,.0f}"
    return f"{number:,.2f}"


def fmt_plain(value, digits: int = 1) -> str:
    number = value if is_num(value) else clean_float(value)
    if number is None:
        return "—"
    return f"{number:.{digits}f}"


def fmt_x(value) -> str:
    number = value if is_num(value) else clean_float(value)
    if number is None:
        return "—"
    return f"{number:.1f}x"


def h(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def classify(
    change,
    rsi,
    dev,
    days,
    youtube: bool,
    volume_ratio,
    gap,
    current,
    ma25,
) -> tuple[str, list[str]]:
    signals: list[str] = []
    if is_num(change) and change >= TAKE_PROFIT:
        signals.append("利確目標到達")
    if is_num(change) and change <= STOP_LOSS:
        signals.append("損切り警戒")
    if is_num(rsi) and is_num(dev) and rsi <= BOTTOM_RSI and dev <= BOTTOM_DEV:
        signals.append("うねり底値シグナル")
    if is_num(dev) and dev >= OVERHEAT_DEV:
        signals.append("過熱警戒（寄り天・押し目待ち）")
    if is_num(volume_ratio) and volume_ratio >= VOLUME_SURGE:
        signals.append("出来高急増")
    if (
        is_num(volume_ratio)
        and volume_ratio >= BREAKOUT_VOLUME
        and is_num(current)
        and is_num(ma25)
        and current > ma25
        and (not is_num(dev) or dev < OVERHEAT_DEV)
    ):
        signals.append("ブレイク")
    if is_num(gap) and gap <= GAP_DOWN:
        signals.append("ギャップダウン")
    if youtube and is_num(days) and days >= REVIEW_DAYS:
        signals.append("棚卸し候補（30日経過）")

    status = "監視中"
    for name in STATUS_PRIORITY:
        if name in signals:
            status = name
            break
    return status, signals


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.where(avg_loss != 0)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return rsi


def normalize_ohlcv(frame: pd.DataFrame) -> pd.DataFrame | None:
    if frame is None or frame.empty:
        return None
    out = frame.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [
            " ".join(str(part) for part in col if str(part) not in {"", "None"}).strip()
            for col in out.columns
        ]
    else:
        out.columns = [str(col) for col in out.columns]
    lookup = {str(col).lower().replace(" ", ""): col for col in out.columns}

    def pick(*names):
        for name in names:
            key = name.lower().replace(" ", "")
            if key in lookup:
                return out[lookup[key]]
        return None

    close = pick("close", "adjclose")
    if close is None:
        return None
    opened = pick("open")
    high = pick("high")
    low = pick("low")
    volume = pick("volume")
    data = pd.DataFrame({"Close": pd.to_numeric(close, errors="coerce")}, index=out.index)
    data["Open"] = pd.to_numeric(opened, errors="coerce") if opened is not None else data["Close"]
    data["High"] = pd.to_numeric(high, errors="coerce") if high is not None else data["Close"]
    data["Low"] = pd.to_numeric(low, errors="coerce") if low is not None else data["Close"]
    data["Volume"] = pd.to_numeric(volume, errors="coerce") if volume is not None else pd.NA
    data.index = pd.to_datetime(data.index, errors="coerce")
    data = data[data.index.notna()]
    if getattr(data.index, "tz", None) is not None:
        data.index = data.index.tz_localize(None)
    data = data[~data.index.duplicated(keep="last")].sort_index()
    data = data.dropna(subset=["Close"])
    if data.empty:
        return None
    return data


def extract_ticker_frame(frame: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    if not isinstance(frame.columns, pd.MultiIndex):
        return normalize_ohlcv(frame)
    for level in range(frame.columns.nlevels):
        values = set(frame.columns.get_level_values(level))
        if ticker not in values:
            continue
        try:
            sub = frame.xs(ticker, axis=1, level=level)
        except Exception as exc:
            logging.warning("%s の列分解に失敗しました: %s", ticker, exc)
            return None
        if isinstance(sub, pd.Series):
            return None
        return normalize_ohlcv(sub)
    return None


def looks_like_rate_limit(exc: BaseException | str | None) -> bool:
    text = str(exc or "").lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


def download_one(ticker: str) -> tuple[pd.DataFrame | None, bool]:
    """1銘柄を取得する。戻り値は (足, レート制限か)。失敗しても例外は外へ出さない。"""
    try:
        single = download_raw(ticker)
    except Exception as exc:
        logging.warning("個別取得失敗 %s: %s", ticker, exc)
        return None, looks_like_rate_limit(exc)
    if not isinstance(single, pd.DataFrame) or single.empty:
        return None, False
    hist = extract_ticker_frame(single, ticker)
    if hist is None and not isinstance(single.columns, pd.MultiIndex):
        hist = normalize_ohlcv(single)
    if hist is None or hist.empty:
        return None, False
    return hist, False


def download_raw(tickers: list[str] | str):
    kwargs = {
        "tickers": tickers,
        "period": "3mo",
        "interval": "1d",
        "group_by": "ticker",
        "auto_adjust": True,
        "threads": False,
        "progress": False,
    }
    try:
        return yf.download(**kwargs, timeout=60)
    except TypeError:
        return yf.download(**kwargs)
    except Exception as exc:
        logging.warning("yfinance の取得に失敗しました (%s): %s", tickers if isinstance(tickers, str) else len(tickers), exc)
        if looks_like_rate_limit(exc):
            raise
        return None


def fetch_all(tickers: list[str]) -> dict[str, pd.DataFrame]:
    histories: dict[str, pd.DataFrame] = {}
    total = len(tickers)
    abort_remaining = False
    empty_batches = 0
    for start in range(0, total, BATCH_SIZE):
        if abort_remaining:
            logging.warning("レート制限のため残り %s ティッカーは取得を打ち切り、前回値を維持します", total - start)
            break
        batch = tickers[start : start + BATCH_SIZE]
        logging.info("株価取得 %s-%s / %s", start + 1, start + len(batch), total)
        frame = None
        last_batch_error: Exception | None = None
        for attempt in range(2):
            try:
                frame = download_raw(batch if len(batch) > 1 else batch[0])
            except Exception as exc:
                logging.warning("バッチ失敗 %s/2: %s", attempt + 1, exc)
                last_batch_error = exc
                frame = None
                if looks_like_rate_limit(exc):
                    time.sleep(RATE_LIMIT_SLEEP_SEC)
                    continue
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                break
            time.sleep(2.5 * (attempt + 1))

        missing: list[str] = []
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            empty_batches += 1
            if looks_like_rate_limit(last_batch_error) or empty_batches >= 2:
                logging.warning("バッチ取得が連続で失敗したため個別連打をせず、残りは前回値を維持します")
                abort_remaining = True
                time.sleep(RATE_LIMIT_SLEEP_SEC)
            else:
                logging.warning(
                    "バッチ未取得のため %.1fs 待って、このバッチだけ個別に再取得します",
                    BATCH_FAIL_COOLDOWN_SEC,
                )
                time.sleep(BATCH_FAIL_COOLDOWN_SEC)
                missing = list(batch)
        else:
            empty_batches = 0
            if len(batch) == 1 and not isinstance(frame.columns, pd.MultiIndex):
                hist = normalize_ohlcv(frame)
                if hist is not None and not hist.empty:
                    histories[batch[0]] = hist
                else:
                    missing = list(batch)
            else:
                for ticker in batch:
                    hist = extract_ticker_frame(frame, ticker)
                    if hist is not None and not hist.empty:
                        histories[ticker] = hist
                    else:
                        missing.append(ticker)

        for ticker in missing:
            if abort_remaining:
                break
            time.sleep(REQUEST_GAP_SEC)
            hist, limited = download_one(ticker)
            if limited:
                logging.warning("レート制限を検出。残り取得を打ち切ります")
                abort_remaining = True
                time.sleep(RATE_LIMIT_SLEEP_SEC)
            if hist is not None and not hist.empty:
                histories[ticker] = hist
            else:
                logging.warning("取得スキップ: %s", ticker)
        if start + BATCH_SIZE < total:
            time.sleep(BATCH_SLEEP_SEC)
    logging.info("取得成功 %s / %s", len(histories), total)
    return histories


def compute_snapshot(hist: pd.DataFrame) -> dict:
    close = hist["Close"]
    ma25 = close.rolling(25, min_periods=25).mean()
    rsi = wilder_rsi(close, 14)
    volume = hist["Volume"]
    vol_avg = volume.shift(1).rolling(5, min_periods=5).mean()
    volume_ratio = volume / vol_avg.where(vol_avg > 0)
    prev = close.shift(1)
    gap = (hist["Open"] - prev) / prev.where(prev != 0) * 100
    day = (close - prev) / prev.where(prev != 0) * 100
    week_base = close.shift(5)
    week = (close - week_base) / week_base.where(week_base != 0) * 100
    current = clean_float(close.iloc[-1])
    ma = clean_float(ma25.iloc[-1])
    dev = None
    if current is not None and ma not in (None, 0):
        dev = (current - ma) / ma * 100
    return {
        "current": current,
        "high": clean_float(hist["High"].iloc[-1]),
        "low": clean_float(hist["Low"].iloc[-1]),
        "ma25": ma,
        "ma25_dev": dev,
        "rsi14": clean_float(rsi.iloc[-1]),
        "volume_ratio": clean_float(volume_ratio.iloc[-1]),
        "gap_pct": clean_float(gap.iloc[-1]),
        "day_change_pct": clean_float(day.iloc[-1]),
        "week_change_pct": clean_float(week.iloc[-1]),
        "price_date": pd.Timestamp(hist.index[-1]).date().isoformat(),
    }


def range_since(hist: pd.DataFrame, added: date | None) -> tuple[float | None, float | None]:
    window = hist
    if added is not None:
        sliced = hist[hist.index.date >= added]
        if sliced.empty:
            current = clean_float(hist["Close"].iloc[-1])
            return current, current
        window = sliced
    high = clean_float(window["High"].max())
    low = clean_float(window["Low"].min())
    if high is None:
        high = clean_float(window["Close"].max())
    if low is None:
        low = clean_float(window["Close"].min())
    return high, low


def apply_status(rec: dict, today: date, stamp: str, quote: dict | None) -> None:
    added = parse_date(rec.get("added_date"))
    if added is None:
        added = today
        rec["added_date"] = today.isoformat()
    days = max(0, (today - added).days)
    base = clean_float(rec.get("base_price"))
    current = clean_float(rec.get("current_price"))
    change = clean_float(rec.get("change_pct"))
    rsi = clean_float(rec.get("rsi14"))
    dev = clean_float(rec.get("ma25_dev"))
    volume_ratio = clean_float(rec.get("volume_ratio"))
    gap = clean_float(rec.get("gap_pct"))
    ma25 = clean_float(rec.get("ma25"))
    week = clean_float(rec.get("week_change_pct"))
    day = clean_float(rec.get("day_change_pct"))
    max_high = clean_float(rec.get("max_high_price"))
    min_low = clean_float(rec.get("min_low_price"))
    price_date = (rec.get("price_date") or "").strip()

    if quote and quote.get("current") is not None:
        current = quote["current"]
        rsi = quote["rsi14"]
        dev = quote["ma25_dev"]
        volume_ratio = quote["volume_ratio"]
        gap = quote["gap_pct"]
        ma25 = quote["ma25"]
        week = quote["week_change_pct"]
        day = quote["day_change_pct"]
        price_date = quote["price_date"]
        if base in (None, 0):
            base = current
        if base not in (None, 0) and current is not None:
            change = (current - base) / base * 100
        obs_high, obs_low = quote["obs_high"], quote["obs_low"]
        if obs_high is not None:
            max_high = obs_high if max_high is None else max(max_high, obs_high)
        if obs_low is not None:
            min_low = obs_low if min_low is None else min(min_low, obs_low)
    elif base not in (None, 0) and current is not None:
        change = (current - base) / base * 100

    youtube = is_youtube_tracking(rec.get("group_name"), rec.get("source"), rec.get("strategy"))
    status, signals = classify(change, rsi, dev, days, youtube, volume_ratio, gap, current, ma25)
    rec["days_elapsed"] = str(days)
    rec["base_price"] = num_to_csv(base)
    rec["current_price"] = num_to_csv(current)
    rec["change_pct"] = num_to_csv(change, 2)
    rec["week_change_pct"] = num_to_csv(week, 2)
    rec["day_change_pct"] = num_to_csv(day, 2)
    rec["gap_pct"] = num_to_csv(gap, 2)
    rec["max_high_price"] = num_to_csv(max_high)
    rec["min_low_price"] = num_to_csv(min_low)
    rec["ma25"] = num_to_csv(ma25)
    rec["ma25_dev"] = num_to_csv(dev, 2)
    rec["rsi14"] = num_to_csv(rsi, 2)
    rec["volume_ratio"] = num_to_csv(volume_ratio, 2)
    rec["price_date"] = price_date
    rec["status"] = status
    rec["signals"] = "|".join(signals)
    rec["last_updated"] = stamp


def update_record(rec: dict, hist: pd.DataFrame | None, today: date, stamp: str, attempted: bool) -> None:
    try:
        quote = None
        if attempted and hist is not None:
            snap = compute_snapshot(hist)
            if snap.get("current") is None:
                rec["fetch_status"] = "no_data"
            else:
                obs_high, obs_low = range_since(hist, parse_date(rec.get("added_date")) or today)
                snap["obs_high"] = obs_high
                snap["obs_low"] = obs_low
                quote = snap
                rec["fetch_status"] = "ok"
        elif attempted:
            rec["fetch_status"] = "no_data"
        apply_status(rec, today, stamp, quote)
    except Exception as exc:
        logging.exception("更新失敗 %s", rec.get("code"))
        rec["fetch_status"] = "error:" + str(exc).replace("\n", " ")[:100]
        rec["last_updated"] = stamp


def read_tracker(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            frame = pd.read_csv(path, dtype=str, encoding=encoding, keep_default_na=False)
            frame.columns = [str(col).strip() for col in frame.columns]
            logging.info("ウォッチリストを読み込みました: %s (%s, %s行)", path.name, encoding, len(frame))
            return frame
        except UnicodeError as exc:
            last_error = exc
            continue
    logging.warning("厳格な読込に失敗したため cp932/replace で継続します: %s (%s)", path, last_error)
    frame = pd.read_csv(
        path, dtype=str, encoding="cp932", encoding_errors="replace", keep_default_na=False
    )
    frame.columns = [str(col).strip() for col in frame.columns]
    return frame



def publish_tracker_csv(path: Path) -> None:
    """GitHub Pages の docs から追跡CSVをダウンロードできるように複製する。"""
    dest = DOCS_DIR / "watchlist_tracker.csv"
    if path.resolve() == dest.resolve():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(path.read_bytes())

def write_tracker(path: Path, records: list[dict]) -> None:
    frame = pd.DataFrame(records)
    for column in COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    extra = [col for col in frame.columns if col not in COLUMNS]
    frame = frame[COLUMNS + extra].fillna("")
    frame = frame.astype(str).replace({"nan": "", "None": "", "<NA>": ""})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(tmp, index=False, encoding="utf-8-sig", lineterminator="\n")
    os.replace(tmp, path)
    publish_tracker_csv(path)


def to_stock(rec: dict) -> dict:
    group_name = (rec.get("group_name") or "").strip()
    code = (rec.get("code") or "").strip()
    signals = [part for part in (rec.get("signals") or "").split("|") if part]
    days = clean_float(rec.get("days_elapsed"))

    def rounded(key, digits=4):
        number = clean_float(rec.get(key))
        if number is None:
            return None
        return round(number, digits)

    return {
        "code": code,
        "ticker": (rec.get("ticker") or "").strip(),
        "asset_type": (rec.get("asset_type") or "").strip().upper(),
        "name": (rec.get("name") or "").strip(),
        "group_name": group_name,
        "groups": [group_name] if group_name else [],
        "market": (rec.get("market") or "").strip(),
        "strategy": (rec.get("strategy") or "").strip(),
        "source": (rec.get("source") or "").strip(),
        "added_date": (rec.get("added_date") or "").strip(),
        "base_price": rounded("base_price"),
        "current_price": rounded("current_price"),
        "change_pct": rounded("change_pct", 2),
        "week_change_pct": rounded("week_change_pct", 2),
        "day_change_pct": rounded("day_change_pct", 2),
        "gap_pct": rounded("gap_pct", 2),
        "max_high_price": rounded("max_high_price"),
        "min_low_price": rounded("min_low_price"),
        "days_elapsed": None if days is None else int(days),
        "ma25": rounded("ma25"),
        "ma25_dev": rounded("ma25_dev", 2),
        "rsi14": rounded("rsi14", 2),
        "volume_ratio": rounded("volume_ratio", 2),
        "price_date": (rec.get("price_date") or "").strip() or None,
        "status": (rec.get("status") or "監視中").strip(),
        "signals": signals,
        "youtube": is_youtube_tracking(group_name, rec.get("source"), rec.get("strategy")),
        "fetch_status": (rec.get("fetch_status") or "").strip(),
        "chronicle_url": chronicle_url(code),
    }


def sort_num(items: list[dict], key: str, reverse: bool = False) -> list[dict]:
    def sort_key(stock):
        value = stock.get(key)
        if not is_num(value):
            return (1, 0.0)
        return (0, -float(value) if reverse else float(value))

    return sorted(items, key=sort_key)


def best_worst(items: list[dict], key: str, limit: int = 10) -> tuple[list[dict], list[dict]]:
    valid = [stock for stock in items if is_num(stock.get(key))]
    gainers = sorted((stock for stock in valid if float(stock[key]) > 0), key=lambda stock: float(stock[key]), reverse=True)
    losers = sorted((stock for stock in valid if float(stock[key]) < 0), key=lambda stock: float(stock[key]))
    return gainers[:limit], losers[:limit]


def dedupe(stocks: list[dict]) -> list[dict]:
    order: list[str] = []
    bucket: dict[str, dict] = {}
    for stock in stocks:
        code = stock["code"]
        if code not in bucket:
            copied = dict(stock)
            copied["groups"] = [stock["group_name"]] if stock["group_name"] else []
            bucket[code] = copied
            order.append(code)
            continue
        dest = bucket[code]
        if stock["group_name"] and stock["group_name"] not in dest["groups"]:
            dest["groups"].append(stock["group_name"])
        if (stock.get("added_date") or "9999") < (dest.get("added_date") or "9999"):
            groups = dest["groups"]
            dest = dict(stock)
            dest["groups"] = groups
            bucket[code] = dest
    result = []
    for code in order:
        item = bucket[code]
        item["group_name"] = " / ".join(item["groups"])
        result.append(item)
    return result


def has_signal(stock: dict, name: str) -> bool:
    return name in (stock.get("signals") or [])


def build_payload(records: list[dict], mode: str, generated_at: str, attempted_count: int) -> dict:
    stocks = [to_stock(rec) for rec in records if rec.get("code") and not is_archived(rec)]
    stocks.sort(key=lambda stock: (group_sort_key(stock.get("group_name") or ""), stock.get("code") or ""))
    unique = dedupe(stocks)
    priced = [stock for stock in unique if is_num(stock.get("change_pct"))]
    wins = sum(1 for stock in priced if stock["change_pct"] > 0)
    losses = sum(1 for stock in priced if stock["change_pct"] < 0)
    flats = sum(1 for stock in priced if stock["change_pct"] == 0)
    win_rate = round(wins / len(priced) * 100, 1) if priced else None
    avg_change = round(sum(stock["change_pct"] for stock in priced) / len(priced), 2) if priced else None

    status_by_ticker: dict[str, str] = {}
    for stock in stocks:
        ticker = stock.get("ticker") or stock["code"]
        current = status_by_ticker.get(ticker, "")
        fetched = stock.get("fetch_status") or ""
        if current == "ok":
            continue
        if fetched:
            status_by_ticker[ticker] = fetched
    fetch_ok = sum(1 for value in status_by_ticker.values() if value == "ok")
    failed_codes = sorted(ticker for ticker, value in status_by_ticker.items() if value and value != "ok")
    dates = [stock["price_date"] for stock in unique if stock.get("price_date")]
    price_as_of = Counter(dates).most_common(1)[0][0] if dates else None

    bottom = sort_num([s for s in unique if has_signal(s, "うねり底値シグナル")], "rsi14")
    overheat = sort_num([s for s in unique if has_signal(s, "過熱警戒（寄り天・押し目待ち）")], "ma25_dev", True)
    breakout = sort_num([s for s in unique if has_signal(s, "ブレイク")], "volume_ratio", True)
    surge = sort_num([s for s in unique if has_signal(s, "出来高急増")], "volume_ratio", True)
    gap_down = sort_num([s for s in unique if has_signal(s, "ギャップダウン")], "gap_pct")
    take_profit = sort_num([s for s in unique if has_signal(s, "利確目標到達")], "change_pct", True)
    stop_loss = sort_num([s for s in unique if has_signal(s, "損切り警戒")], "change_pct")
    review = sort_num(
        [s for s in unique if is_num(s.get("days_elapsed")) and s["days_elapsed"] >= REVIEW_DAYS],
        "change_pct",
    )
    youtube = [s for s in review if s.get("youtube")]
    us = [s for s in unique if s.get("asset_type") == "USS"]
    us_gainers, us_losers = best_worst(us, "day_change_pct", 10)
    top_gainers, top_losers = best_worst(unique, "change_pct", 10)
    weekly_gainers, weekly_losers = best_worst(unique, "week_change_pct", 10)
    present = [stock["group_name"] for stock in stocks if stock.get("group_name")]
    groups = ordered_groups(present, include_empty=True)

    note = ""
    if attempted_count and fetch_ok == 0:
        note = "今回の実行では株価を取得できませんでした。前回の数値を維持しています。"

    return {
        "generated_at": generated_at,
        "run_mode": mode,
        "note": note,
        "summary": {
            "tracked_rows": len(stocks),
            "unique_codes": len(unique),
            "priced": len(priced),
            "fetch_ok": fetch_ok,
            "fetch_failed": len(failed_codes),
            "win": wins,
            "lose": losses,
            "flat": flats,
            "win_rate": win_rate,
            "avg_change_pct": avg_change,
            "bottom_count": len(bottom),
            "overheat_count": len(overheat),
            "breakout_count": len(breakout),
            "gap_down_count": len(gap_down),
            "take_profit_count": len(take_profit),
            "stop_loss_count": len(stop_loss),
            "review_count": len(review),
            "youtube_review_count": len(youtube),
            "price_as_of": price_as_of,
        },
        "bottom_signals": bottom,
        "overheat_signals": overheat,
        "breakout_signals": breakout,
        "volume_surge": surge,
        "gap_down": gap_down,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "us_gainers": us_gainers,
        "us_losers": us_losers,
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "weekly_gainers": weekly_gainers,
        "weekly_losers": weekly_losers,
        "youtube_review": youtube,
        "review_30d": review,
        "fetch_failed_codes": failed_codes[:50],
        "groups": groups,
        "stocks": stocks,
    }


def write_json(payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    for path in (DOCS_DIR / "latest_summary.json", DATA_DIR / "latest_summary.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def pages_url() -> str:
    value = os.getenv("PAGES_URL", "").strip()
    return value or DEFAULT_PAGES_URL


def pct_color(value) -> str:
    number = value if is_num(value) else clean_float(value)
    if number is None or number == 0:
        return "#6b7280"
    return "#dc2626" if number > 0 else "#059669"


def code_link(stock: dict) -> str:
    code = stock.get("code") or ""
    url = yahoo_quote_url(stock) or chronicle_url(str(code))
    return (
        f'<a href="{h(url)}" style="color:#9f1239;font-weight:700;text-decoration:none">{h(code)}</a>'
    )


def render_stock_row(stock: dict, primary: str) -> str:
    name = stock.get("name") or stock.get("code") or ""
    group = stock.get("group_name") or ""
    metrics = (
        f"RSI {fmt_plain(stock.get('rsi14'), 1)} / 乖離 {fmt_pct(stock.get('ma25_dev'))}"
        f" / 出来高 {fmt_x(stock.get('volume_ratio'))}"
    )
    secondary = ""
    if primary != "change_pct":
        secondary = (
            f'<div style="color:#6b7280;font-size:12px;margin-top:2px">登録来 {h(fmt_pct(stock.get("change_pct")))}</div>'
        )
    quote_url = yahoo_quote_url(stock)
    if quote_url:
        name_html = (
            f'<a href="{h(quote_url)}" style="color:#111827;font-weight:700;text-decoration:underline">{h(name)}</a>'
        )
    else:
        name_html = h(name)
    return f"""
      <tr>
        <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;vertical-align:top">
          <div style="font-weight:700;color:#111827;font-size:14px">{name_html}</div>
          <div style="font-size:12px;color:#6b7280;margin-top:2px">{code_link(stock)} · {h(group)}</div>
        </td>
        <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;text-align:right;vertical-align:top;white-space:nowrap">
          <div style="font-weight:700;color:#111827">{h(fmt_price(stock.get("current_price")))}</div>
          <div style="font-weight:700;color:{pct_color(stock.get(primary))}">{h(fmt_pct(stock.get(primary)))}</div>
          {secondary}
        </td>
        <td style="padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#374151;vertical-align:top">{h(metrics)}</td>
      </tr>
    """


def render_section(title: str, stocks: list[dict], primary: str, limit: int = 30) -> tuple[str, list[str]]:
    shown = list(stocks)[:limit]
    hidden = max(0, len(stocks) - len(shown))
    if not shown:
        body = '<p style="margin:8px 0 0;color:#6b7280;font-size:13px">該当なし</p>'
        text = [f"## {title}", "該当なし", ""]
    else:
        rows = "".join(render_stock_row(stock, primary) for stock in shown)
        more = ""
        if hidden:
            more = f'<p style="margin:8px 0 0;color:#6b7280;font-size:12px">ほか {hidden} 件はダッシュボードで確認できます。</p>'
        body = f"""
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:8px">
            <tr>
              <th align="left" style="padding:6px 8px;border-bottom:2px solid #e5e7eb;font-size:12px;color:#6b7280">銘柄</th>
              <th align="right" style="padding:6px 8px;border-bottom:2px solid #e5e7eb;font-size:12px;color:#6b7280">価格</th>
              <th align="left" style="padding:6px 8px;border-bottom:2px solid #e5e7eb;font-size:12px;color:#6b7280">指標</th>
            </tr>
            {rows}
          </table>
          {more}
        """
        text = [f"## {title}"]
        for stock in shown:
            text.append(
                f"- {stock.get('code')} {stock.get('name') or ''} {fmt_pct(stock.get(primary))} {yahoo_quote_url(stock)}"
            )
        if hidden:
            text.append(f"ほか {hidden} 件")
        text.append("")
    html = f"""
      <h2 style="margin:22px 0 0;font-size:16px;color:#111827">{h(title)} <span style="color:#6b7280;font-size:12px;font-weight:600">{len(stocks)}</span></h2>
      {body}
    """
    return html, text


def email_sections(mode: str, payload: dict) -> list[tuple[str, list[dict], str]]:
    if mode == "morning":
        return [
            ("前夜の米国株（上昇）", payload["us_gainers"], "day_change_pct"),
            ("前夜の米国株（下落）", payload["us_losers"], "day_change_pct"),
            ("過熱警戒（寄り天・押し目待ち）", payload["overheat_signals"], "ma25_dev"),
            ("ギャップダウン狙い", payload["gap_down"], "gap_pct"),
        ]
    if mode == "weekend":
        return [
            ("登録後30日超の棚卸し", payload["review_30d"], "change_pct"),
            ("YouTube棚卸し（30日経過）", payload["youtube_review"], "change_pct"),
            ("週間騰落（上昇）", payload["weekly_gainers"], "week_change_pct"),
            ("週間騰落（下落）", payload["weekly_losers"], "week_change_pct"),
        ]
    return [
        ("うねり底値シグナル", payload["bottom_signals"], "change_pct"),
        ("ブレイク", payload["breakout_signals"], "change_pct"),
        ("過熱警戒（寄り天・押し目待ち）", payload["overheat_signals"], "ma25_dev"),
        ("登録来パフォーマンス上位", payload["top_gainers"], "change_pct"),
        ("登録来パフォーマンス下位", payload["top_losers"], "change_pct"),
    ]


def build_email(mode: str, payload: dict) -> tuple[str, str, str]:
    summary = payload["summary"]
    title = MODE_TITLE.get(mode, "レポート")
    today = payload["generated_at"][:10]
    subject = f"【uneri-tracker】{title} {today}"
    dashboard = pages_url()
    win = "—" if summary["win_rate"] is None else f"{summary['win_rate']:.1f}%"
    avg = "—" if summary["avg_change_pct"] is None else fmt_pct(summary["avg_change_pct"])
    note_html = ""
    note_text = ""
    if payload.get("note"):
        note_html = (
            '<p style="margin:12px 0 0;padding:10px 12px;background:#fff7ed;color:#9a3412;'
            f'border-radius:8px;font-size:13px">{h(payload["note"])}</p>'
        )
        note_text = payload["note"]
    failed = payload.get("fetch_failed_codes") or []
    failed_html = ""
    if failed:
        failed_html = (
            '<p style="margin:16px 0 0;color:#6b7280;font-size:12px">取得できなかったティッカー: '
            + h(", ".join(failed[:20]))
            + (" …" if len(failed) > 20 else "")
            + "</p>"
        )

    html_parts = []
    text_lines = [
        subject,
        f"更新: {payload['generated_at']}",
        f"銘柄: {summary['unique_codes']} / 勝率: {win} / 平均騰落: {avg}",
        f"利確 {summary['take_profit_count']} / 損切り {summary['stop_loss_count']} / 底値 {summary['bottom_count']} / 過熱 {summary['overheat_count']}",
        f"ダッシュボード: {dashboard}",
        note_text,
        "",
    ]
    for title_text, stocks, primary in email_sections(mode, payload):
        section_html, section_text = render_section(title_text, stocks, primary, 40 if mode == "weekend" else 30)
        html_parts.append(section_html)
        text_lines.extend(section_text)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<body style="margin:0;padding:0;background:#f3f4f6;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;">
    <tr><td align="center" style="padding:12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border-radius:12px;">
        <tr><td style="padding:20px 16px;font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',Meiryo,sans-serif;">
          <div style="font-size:12px;letter-spacing:0.08em;color:#9f1239;font-weight:700">UNERI TRACKER</div>
          <h1 style="margin:4px 0 0;font-size:22px;color:#111827">{h(title)}</h1>
          <p style="margin:6px 0 0;color:#6b7280;font-size:13px">{h(payload['generated_at'])} / 株価日 {h(summary.get('price_as_of') or '—')}</p>
          {note_html}
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px">
            <tr>
              <td style="padding:8px;background:#f9fafb;border-radius:8px;width:25%"><div style="font-size:11px;color:#6b7280">銘柄</div><div style="font-weight:700">{summary['unique_codes']}</div></td>
              <td width="8"></td>
              <td style="padding:8px;background:#f9fafb;border-radius:8px;width:25%"><div style="font-size:11px;color:#6b7280">勝率</div><div style="font-weight:700">{h(win)}</div></td>
              <td width="8"></td>
              <td style="padding:8px;background:#f9fafb;border-radius:8px;width:25%"><div style="font-size:11px;color:#6b7280">平均騰落</div><div style="font-weight:700;color:{pct_color(summary.get('avg_change_pct'))}">{h(avg)}</div></td>
              <td width="8"></td>
              <td style="padding:8px;background:#f9fafb;border-radius:8px;width:25%"><div style="font-size:11px;color:#6b7280">底値</div><div style="font-weight:700">{summary['bottom_count']}</div></td>
            </tr>
          </table>
          <p style="margin:12px 0 0;font-size:13px;color:#374151">利確 {summary['take_profit_count']} ・ 損切り {summary['stop_loss_count']} ・ 過熱 {summary['overheat_count']} ・ ブレイク {summary['breakout_count']} ・ ギャップダウン {summary['gap_down_count']}</p>
          <p style="margin:14px 0 0"><a href="{h(dashboard)}" style="display:inline-block;background:#9f1239;color:#ffffff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:700;font-size:14px">ダッシュボードを開く</a></p>
          {''.join(html_parts)}
          {failed_html}
          <p style="margin:22px 0 0;color:#9ca3af;font-size:11px">上昇は赤、下落は緑。銘柄名とコードは Yahoo!ファイナンスへリンクしています。数値は登録来騰落で、朝の米国株と週末の週間欄だけ直前セッションの騰落です。</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    text = "\n".join(line for line in text_lines if line is not None)
    return subject, html, text


def send_mail(subject: str, html: str, text: str) -> bool:
    user = os.getenv("MAIL_USER", "").strip()
    password = os.getenv("MAIL_PASS", "").replace(" ", "").strip()
    raw_to = os.getenv("MAIL_TO", "").strip()
    if not (user and password and raw_to):
        logging.warning("MAIL_USER / MAIL_PASS / MAIL_TO が未設定のため、メール送信をスキップしました")
        return False
    recipients = [item.strip() for item in raw_to.replace(";", ",").split(",") if item.strip()]
    sender = os.getenv("MAIL_FROM", "").strip() or user
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    context = ssl.create_default_context()
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=40) as smtp:
                smtp.login(user, password)
                smtp.send_message(message)
            logging.info("メールを送信しました: %s", ", ".join(recipients))
            return True
        except Exception as exc:
            last_error = exc
            logging.warning("メール送信に失敗しました (%s/2): %s", attempt + 1, exc)
            time.sleep(3)
    logging.error("メール送信を諦めました: %s", last_error)
    return False


def parse_args() -> argparse.Namespace:
    default_mode = os.getenv("RUN_MODE", "evening").strip().lower() or "evening"
    parser = argparse.ArgumentParser(description="ウォッチリストの株価・シグナル・メールを更新します")
    parser.add_argument("--mode", default=default_mode, choices=["morning", "evening", "weekend"])
    parser.add_argument("--limit", type=int, default=0, help="デバッグ用。先頭から N ティッカーだけ取得する")
    parser.add_argument("--skip-mail", action="store_true", help="メールを送らない")
    return parser.parse_args()


def is_archived(rec: dict) -> bool:
    return (rec.get("archived") or "").strip().lower() in {"1", "true", "yes", "archived"}


def collect_tickers(records: list[dict]) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    for rec in records:
        if is_archived(rec):
            continue
        asset = (rec.get("asset_type") or "").strip().upper()
        code = normalize_code(rec.get("code") or "")
        rec["code"] = code
        rec["asset_type"] = asset
        ticker = (rec.get("ticker") or "").strip()
        if not ticker:
            ticker = to_ticker(asset, code)
        rec["ticker"] = ticker
        if ticker and ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("yfinance", "peewee", "urllib3"):
        logger = logging.getLogger(noisy)
        logger.handlers.clear()
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False
    args = parse_args()
    if not TRACKER_CSV.exists():
        logging.error("ウォッチリストがありません。先に scripts/migrate_rakuten_csv.py を実行してください")
        return 1
    try:
        frame = read_tracker(TRACKER_CSV)
    except Exception:
        logging.exception("ウォッチリストの読み込みに失敗しました")
        return 1
    if "code" not in frame.columns or frame.empty:
        logging.error("ウォッチリストに code 列がないか、行がありません")
        return 1
    records = frame.to_dict(orient="records")
    for rec in records:
        for key, value in list(rec.items()):
            rec[key] = "" if value is None else str(value).strip()
    tickers = collect_tickers(records)
    if args.limit and args.limit > 0:
        tickers = tickers[: args.limit]
        logging.info("デバッグ制限: %s ティッカー", len(tickers))
    try:
        histories = fetch_all(tickers)
    except Exception:
        logging.exception("株価取得全体が失敗しました。前回値のまま集計します")
        histories = {}
    today = datetime.now(JST).date()
    stamp = datetime.now(JST).isoformat(timespec="seconds")
    attempted = set(tickers)
    for rec in records:
        if is_archived(rec):
            continue
        ticker = rec.get("ticker") or ""
        hist = histories.get(ticker) if ticker in attempted else None
        update_record(rec, hist, today, stamp, ticker in attempted)
    try:
        write_tracker(TRACKER_CSV, records)
        payload = build_payload(records, args.mode, stamp, len(tickers))
        write_json(payload)
    except Exception:
        logging.exception("結果の保存に失敗しました")
        return 1
    summary = payload["summary"]
    logging.info(
        "mode=%s rows=%s unique=%s ok=%s failed=%s bottom=%s overheat=%s win_rate=%s",
        args.mode,
        summary["tracked_rows"],
        summary["unique_codes"],
        summary["fetch_ok"],
        summary["fetch_failed"],
        summary["bottom_count"],
        summary["overheat_count"],
        summary["win_rate"],
    )
    if not args.skip_mail:
        try:
            subject, html, text = build_email(args.mode, payload)
            send_mail(subject, html, text)
        except Exception:
            logging.exception("メール作成または送信でエラーになりました。株価データは保存済みです")
    return 0


if __name__ == "__main__":
    sys.exit(main())
