#!/usr/bin/env python3
"""楽天証券のお気に入りCSVを watchlist_tracker.csv へ移行する。

入力は CP932（Shift_JIS）の6列CSVを想定する。
1行目の ``MS2,2`` のような形式ヘッダは読み飛ばす。

日本株（STK）は ``{code}.T``、米国株（USS）は ``{code}`` に変換する。
同じ銘柄が複数グループにある場合は、グループごとに1行残す。
再実行しても、既存行の登録日・基準株価・騰落の記録は消さない。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
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
]

KEEP_ON_MERGE = {
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
}

ENCODINGS = ("cp932", "shift_jis", "utf-8-sig", "utf-8")


def normalize_code(code: str) -> str:
    trans = str.maketrans("０１２３４５６７８９", "0123456789")
    return (code or "").strip().translate(trans).upper()


def to_ticker(asset_type: str, code: str) -> str:
    if asset_type == "STK":
        return f"{code}.T"
    return code


def chronicle_url(code: str) -> str:
    return f"https://stockchronicle.app/?code={quote(code)}"


def is_youtube(*parts: str) -> bool:
    text = " ".join(parts).lower()
    return "youtube" in text or "ユーチューブ" in text


def read_rakuten_rows(path: Path) -> list[list[str]]:
    last_error: Exception | None = None
    for encoding in ENCODINGS:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                rows = list(csv.reader(handle))
            logging.info("楽天CSVを読み込みました: %s (%s, %s行)", path, encoding, len(rows))
            return rows
        except UnicodeError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"文字コードを判定できません: {path} ({last_error})")


def parse_rakuten(rows: list[list[str]], today: str) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        if not raw or not any(cell.strip() for cell in raw):
            continue
        cells = [cell.strip() for cell in raw] + [""] * 6
        asset_type = cells[0].upper()
        if asset_type not in {"STK", "USS"}:
            continue
        code = normalize_code(cells[1])
        if not code:
            logging.warning("コードが空の行をスキップしました: %s", raw)
            continue
        group_name = cells[2]
        sub_id = cells[3]
        market = cells[4]
        name = cells[5]
        key = (code, group_name)
        if key in seen:
            continue
        seen.add(key)
        parsed.append(
            {
                "code": code,
                "ticker": to_ticker(asset_type, code),
                "asset_type": asset_type,
                "name": name,
                "group_name": group_name,
                "market": market,
                "sub_id": sub_id,
                "strategy": "",
                "source": "楽天インポート",
                "added_date": today,
                "base_price": "",
                "current_price": "",
                "change_pct": "",
                "week_change_pct": "",
                "day_change_pct": "",
                "gap_pct": "",
                "max_high_price": "",
                "min_low_price": "",
                "days_elapsed": "0",
                "ma25": "",
                "ma25_dev": "",
                "rsi14": "",
                "volume_ratio": "",
                "price_date": "",
                "status": "監視中",
                "signals": "",
                "last_updated": "",
                "fetch_status": "",
            }
        )
    return parsed


def read_existing(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        return [], []
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                fieldnames = list(reader.fieldnames or [])
                rows = []
                for row in reader:
                    cleaned = {
                        (key or "").strip(): (value or "").strip()
                        for key, value in row.items()
                        if key
                    }
                    rows.append(cleaned)
            logging.info("既存トラッカーを読み込みました: %s (%s行)", path, len(rows))
            return rows, fieldnames
        except UnicodeError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"既存CSVを読み込めません: {path} ({last_error})")


def merge_rows(
    fresh: list[dict[str, str]], existing: list[dict[str, str]]
) -> list[dict[str, str]]:
    old_map: dict[tuple[str, str], dict[str, str]] = {}
    for row in existing:
        code = normalize_code(row.get("code", ""))
        group_name = (row.get("group_name") or "").strip()
        if not code:
            continue
        old_map[(code, group_name)] = row

    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in fresh:
        key = (row["code"], row["group_name"])
        seen.add(key)
        old = old_map.get(key)
        if not old:
            merged.append(row)
            continue
        combined = {column: "" for column in COLUMNS}
        combined.update(old)
        for field in ("ticker", "asset_type", "name", "group_name", "market", "sub_id"):
            if row.get(field):
                combined[field] = row[field]
        if not (combined.get("source") or "").strip():
            combined["source"] = "楽天インポート"
        if not (combined.get("status") or "").strip():
            combined["status"] = "監視中"
        if not (combined.get("added_date") or "").strip():
            combined["added_date"] = row["added_date"]
        for field in KEEP_ON_MERGE:
            combined.setdefault(field, old.get(field, ""))
        merged.append(combined)

    for key, old in old_map.items():
        if key not in seen:
            logging.info("CSVから消えた銘柄を追跡継続します: %s / %s", key[0], key[1])
            merged.append(old)
    return merged



def publish_tracker_csv(path: Path) -> None:
    """GitHub Pages の docs から追跡CSVをダウンロードできるように複製する。"""
    dest = DOCS_DIR / "watchlist_tracker.csv"
    if path.resolve() == dest.resolve():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(path.read_bytes())

def write_tracker(path: Path, rows: list[dict[str, str]], extra_columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(COLUMNS)
    for column in extra_columns:
        if column and column not in fieldnames:
            fieldnames.append(column)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: (row.get(name) or "") for name in fieldnames})
    tmp.replace(path)
    publish_tracker_csv(path)


def stock_stub(row: dict[str, str]) -> dict:
    group_name = row.get("group_name") or ""
    code = row.get("code") or ""
    return {
        "code": code,
        "ticker": row.get("ticker") or "",
        "asset_type": row.get("asset_type") or "",
        "name": row.get("name") or "",
        "group_name": group_name,
        "groups": [group_name] if group_name else [],
        "market": row.get("market") or "",
        "strategy": row.get("strategy") or "",
        "source": row.get("source") or "",
        "added_date": row.get("added_date") or "",
        "base_price": None,
        "current_price": None,
        "change_pct": None,
        "week_change_pct": None,
        "day_change_pct": None,
        "gap_pct": None,
        "max_high_price": None,
        "min_low_price": None,
        "days_elapsed": 0,
        "ma25": None,
        "ma25_dev": None,
        "rsi14": None,
        "volume_ratio": None,
        "price_date": None,
        "status": row.get("status") or "監視中",
        "signals": [],
        "youtube": is_youtube(row.get("source", ""), row.get("strategy", ""), group_name),
        "fetch_status": "",
        "chronicle_url": chronicle_url(code),
    }


def write_skeleton(rows: list[dict[str, str]], generated_at: str, force: bool) -> None:
    dest = DOCS_DIR / "latest_summary.json"
    if dest.exists() and not force:
        logging.info("既存の latest_summary.json は維持します（株価データの上書き防止）")
        return
    stocks = [stock_stub(row) for row in rows]
    groups: list[str] = []
    for stock in stocks:
        group_name = stock["group_name"]
        if group_name and group_name not in groups:
            groups.append(group_name)
    unique_codes = len({stock["code"] for stock in stocks})
    payload = {
        "generated_at": generated_at,
        "run_mode": "migrate",
        "note": "株価は未取得です。daily_tracker.py を実行すると基準株価とシグナルが入ります。",
        "summary": {
            "tracked_rows": len(stocks),
            "unique_codes": unique_codes,
            "priced": 0,
            "fetch_ok": 0,
            "fetch_failed": 0,
            "win": 0,
            "lose": 0,
            "flat": 0,
            "win_rate": None,
            "avg_change_pct": None,
            "bottom_count": 0,
            "overheat_count": 0,
            "breakout_count": 0,
            "gap_down_count": 0,
            "take_profit_count": 0,
            "stop_loss_count": 0,
            "review_count": 0,
            "youtube_review_count": 0,
            "price_as_of": None,
        },
        "bottom_signals": [],
        "overheat_signals": [],
        "breakout_signals": [],
        "volume_surge": [],
        "gap_down": [],
        "take_profit": [],
        "stop_loss": [],
        "us_gainers": [],
        "us_losers": [],
        "top_gainers": [],
        "top_losers": [],
        "weekly_gainers": [],
        "weekly_losers": [],
        "youtube_review": [],
        "review_30d": [],
        "fetch_failed_codes": [],
        "groups": groups,
        "stocks": stocks,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    for path in (dest, DATA_DIR / "latest_summary.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    logging.info("初期サマリーを書きました: %s", dest)


def default_input_path() -> Path:
    preferred = DATA_DIR / "00ファイル.csv"
    if preferred.exists():
        return preferred
    return ROOT / "00ファイル.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="楽天証券CSVを追跡マスターへ移行します")
    parser.add_argument("--input", type=Path, default=None, help="楽天CSVのパス")
    parser.add_argument("--output", type=Path, default=DATA_DIR / "watchlist_tracker.csv")
    parser.add_argument(
        "--force-summary",
        action="store_true",
        help="既存の latest_summary.json を初期状態で上書きする",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    source = args.input or default_input_path()
    if not source.exists():
        logging.error("楽天CSVが見つかりません: %s", source)
        return 1
    today = datetime.now(JST).date().isoformat()
    generated_at = datetime.now(JST).isoformat(timespec="seconds")
    try:
        fresh = parse_rakuten(read_rakuten_rows(source), today)
        existing, fieldnames = read_existing(args.output)
        rows = merge_rows(fresh, existing)
        extra = [name for name in fieldnames if name not in COLUMNS]
        write_tracker(args.output, rows, extra)
        write_skeleton(rows, generated_at, args.force_summary)
    except Exception:
        logging.exception("移行に失敗しました")
        return 1

    groups: list[str] = []
    for row in rows:
        if row.get("group_name") and row["group_name"] not in groups:
            groups.append(row["group_name"])
    unique_codes = len({row.get("code") for row in rows})
    logging.info(
        "移行完了: %s行 / ユニーク%s銘柄 / グループ%s (%s)",
        len(rows),
        unique_codes,
        len(groups),
        "、".join(groups),
    )
    logging.info("出力: %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
