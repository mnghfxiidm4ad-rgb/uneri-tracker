#!/usr/bin/env python3
"""楽天証券のお気に入りCSVを watchlist_tracker.csv へ移行する。

入力は CP932（Shift_JIS）の6列CSVを想定する。
1行目の ``MS2,2`` のような形式ヘッダは読み飛ばす。

日本株（STK）は ``{code}.T``、米国株（USS）は ``{code}`` に変換する。
同じ銘柄が複数グループにある場合は、グループごとに1行残す。
突合キーは (group_name, code)。両方にある行は登録日・基準株価・高値・安値などの履歴を引き継ぐ。
楽天CSVにだけある行は当日追加とし、基準株価は空のままにする。
楽天CSVから消えた行は、既定では追跡リストから除外する。--archive で archived=1 として末尾に残せる。
出力順は最新の楽天CSVの出現順。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
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
    "archived",
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


def sync_key(row: dict[str, str]) -> tuple[str, str] | None:
    code = normalize_code(row.get("code", ""))
    group_name = (row.get("group_name") or "").strip()
    if not code:
        return None
    return (group_name, code)


def index_existing(existing: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    old_map: dict[tuple[str, str], dict[str, str]] = {}
    order: list[tuple[str, str]] = []
    for row in existing:
        key = sync_key(row)
        if key is None:
            continue
        current = old_map.get(key)
        if current is None:
            old_map[key] = row
            order.append(key)
            continue
        if not (current.get("base_price") or "").strip() and (row.get("base_price") or "").strip():
            old_map[key] = row
    old_map["__order__"] = order  # type: ignore[assignment]
    return old_map


def inherit_row(old: dict[str, str], fresh: dict[str, str]) -> dict[str, str]:
    combined = {column: "" for column in COLUMNS}
    combined.update(old)
    combined["code"] = fresh["code"]
    combined["group_name"] = fresh["group_name"]
    for field in ("ticker", "asset_type", "name", "market", "sub_id"):
        if fresh.get(field):
            combined[field] = fresh[field]
    if not (combined.get("source") or "").strip():
        combined["source"] = "楽天インポート"
    if not (combined.get("status") or "").strip():
        combined["status"] = "監視中"
    if not (combined.get("added_date") or "").strip():
        combined["added_date"] = fresh["added_date"]
    for field in KEEP_ON_MERGE:
        if not (combined.get(field) or "").strip():
            combined[field] = old.get(field, "")
    combined["archived"] = ""
    return combined


def merge_rows(
    fresh: list[dict[str, str]],
    existing: list[dict[str, str]],
    archive: bool,
) -> tuple[list[dict[str, str]], dict]:
    """楽天CSVの出現順で追跡リストを同期する。

    キーは (group_name, code)。履歴カラムは既存行を優先する。
    archive が偽のとき、楽天CSVに無い行は結果から除外する。
    """
    indexed = index_existing(existing)
    order: list[tuple[str, str]] = indexed.pop("__order__")  # type: ignore[assignment]
    old_map: dict[tuple[str, str], dict[str, str]] = indexed  # type: ignore[assignment]

    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    added = 0
    kept = 0
    for row in fresh:
        key = (row["group_name"], row["code"])
        if key in seen:
            continue
        seen.add(key)
        old = old_map.get(key)
        if old is None:
            row["archived"] = ""
            merged.append(row)
            added += 1
            logging.info("追加: %s / %s", row["group_name"], row["code"])
            continue
        merged.append(inherit_row(old, row))
        kept += 1

    removed: list[tuple[str, str]] = [key for key in order if key not in seen]
    archived_rows: list[dict[str, str]] = []
    for key in removed:
        group_name, code = key
        logging.info("削除: %s / %s", group_name, code)
        if not archive:
            continue
        old = dict(old_map[key])
        old["archived"] = "1"
        archived_rows.append(old)
        logging.info("アーカイブ: %s / %s", group_name, code)

    return merged + archived_rows, {
        "kept": kept,
        "added": added,
        "removed": len(removed),
        "archived": len(archived_rows),
    }



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
    parser.add_argument(
        "--archive",
        action="store_true",
        help="楽天CSVから消えた銘柄を削除せず、archived=1 で末尾に残す",
    )
    return parser.parse_args()


def archive_requested(args: argparse.Namespace) -> bool:
    if args.archive:
        return True
    return os.getenv("SYNC_ARCHIVE", "").strip().lower() in {"1", "true", "yes", "on"}


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
        if existing and not fresh:
            logging.error("楽天CSVから銘柄を1件も読めませんでした。追跡リストは変更しません")
            return 1
        archive = archive_requested(args)
        rows, stats = merge_rows(fresh, existing, archive)
        logging.info(
            "差分: 維持%s / 追加%s / 削除%s / アーカイブ%s",
            stats["kept"],
            stats["added"],
            stats["removed"],
            stats["archived"],
        )
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
