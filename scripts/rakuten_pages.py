"""楽天お気に入りの正規10ページ定義（migrate / tracker / JSON 共通）。

ダッシュボード (docs/index.html) の PAGE_ORDER もこの並びと一致させること。
"""

from __future__ import annotations

# 厳格順: Page1 → Page10。括弧内は各フォルダの役割。
RAKUTEN_PAGES: list[str] = [
    "日米コア",
    "長期・配当",
    "金利・内需",
    "YouTube・新規",
    "AI・半導体",
    "先端テック",
    "新エネルギー",
    "環境・インフラ",
    "米国株・ETF",
    "スイング監視",
]

PAGE_ROLES: dict[str, str] = {
    "日米コア": "東証プライム大型・主戦場",
    "長期・配当": "高配当バリュー・新NISA候補",
    "金利・内需": "銀行・金融・不動産・リース",
    "YouTube・新規": "注目株・インキュベーション枠、30日追跡専用",
    "AI・半導体": "中小型グロース・DX",
    "先端テック": "量子コンピュータ・宇宙・新技術",
    "新エネルギー": "全固体電池・EV・先端素材",
    "環境・インフラ": "海水淡水化・水処理・重工",
    "米国株・ETF": "米国株全般およびセクターETF",
    "スイング監視": "直近決算・テクニカル押し目待ち",
}

RAKUTEN_PAGE_SET = frozenset(RAKUTEN_PAGES)
RAKUTEN_PAGE_INDEX = {name: index for index, name in enumerate(RAKUTEN_PAGES)}

# 旧グループ名 → 新10ページ名
GROUP_NAME_ALIASES: dict[str, str] = {
    "日米": "日米コア",
    "人工知能": "AI・半導体",
    "全固体電池": "新エネルギー",
    "量子コンピュータ": "先端テック",
    "高配当銘柄": "長期・配当",
    "1031新NISA先": "長期・配当",
    "配当": "長期・配当",
    "海水淡水化": "環境・インフラ",
    "銀行不動産証券": "金利・内需",
    "2024": "スイング監視",
}

# 未マップ名の退避先（運用上、未知グループを孤立させない）
UNKNOWN_GROUP_FALLBACK = "スイング監視"


def canonicalize_group_name(name: str, *, warn: bool = False, log=None) -> str:
    """旧名を正規10ページへ変換する。既に新名ならそのまま。未知は警告してスイング監視へ。"""
    text = (name or "").strip()
    if not text:
        if warn and log is not None:
            log.warning("空のグループ名を %s に退避します", UNKNOWN_GROUP_FALLBACK)
        return UNKNOWN_GROUP_FALLBACK
    if text in RAKUTEN_PAGE_SET:
        return text
    mapped = GROUP_NAME_ALIASES.get(text)
    if mapped:
        return mapped
    if warn and log is not None:
        log.warning(
            "未マップのグループ名を %s に退避します: %s",
            UNKNOWN_GROUP_FALLBACK,
            text,
        )
    return UNKNOWN_GROUP_FALLBACK


def ordered_groups(present: list[str] | set[str] | None = None, *, include_empty: bool = True) -> list[str]:
    """Page1→Page10 順。include_empty=True なら未使用ページも返す。余剰名は末尾。"""
    present_set = {str(name).strip() for name in (present or []) if str(name).strip()}
    ordered: list[str] = []
    for name in RAKUTEN_PAGES:
        if include_empty or name in present_set:
            ordered.append(name)
    extras = sorted(present_set - RAKUTEN_PAGE_SET, key=lambda value: value)
    ordered.extend(extras)
    return ordered


def group_sort_key(group_name: str) -> tuple[int, str]:
    name = (group_name or "").strip()
    if name in RAKUTEN_PAGE_INDEX:
        return (RAKUTEN_PAGE_INDEX[name], name)
    return (len(RAKUTEN_PAGES), name)
