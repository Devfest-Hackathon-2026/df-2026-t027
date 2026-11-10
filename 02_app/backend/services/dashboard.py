"""F7 組織全体の状況（要件定義書F7・7-3-4・7-3-5、開発手順書 Step 11-1）。

守ること：

- 読むのは mart の **`mart_org_status` 1表だけ**（要件定義書1055行「組織全体の状況（F7）は
  `mart_org_status` から読む」）。`stg_`・`int_` を数え直さない。同じ人数が2か所で作られると、
  画面とデータ基盤で食い違ったときにどちらが正しいか決められない。
- mart は**1時間ごとに作り直す**（スケジュールクエリ。登録は `04_scripts/schedule_mart.py`）。
  そのため、**いつ時点の数か（`as_of`）を必ず返す**。返さないと、入れたばかりの記録が
  出ていないことを「不具合」と読み違える。
- **組織をまたいで読まない**（`org_id` で必ず絞る。要件定義書10-3）。
- 1行の単位は「組織・段階・居場所の種類・悪化の兆しの度合い」。被災者1人はどれか1行にだけ
  入るため、**行をまたいで足し合わせても二重に数えない**。
- 地図（Google Maps Platform）は入れない（開発手順書9章の要確認6）。
- 氏名も記録の本文も扱わない（人数だけ）。ログに出すのは行数だけ（10-2の6）。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.config import get_settings
from backend.services import assessments, bq, survivors

logger = logging.getLogger(__name__)

MART_TABLE = "mart_org_status"

# 人数の列（`mart_org_status` の作り方は 03_sql/05_mart.sql）。
METRICS: tuple[str, ...] = (
    "survivor_count",
    "with_open_consultation",
    "with_unapplied_support",
    "with_pending_proposal",
)

# 読む列。`SELECT *` にせず並べる（mart に列が増えても画面の形が勝手に変わらないようにする）。
COLUMNS: tuple[str, ...] = ("as_of", "stage", "location_type", "risk_level") + METRICS

# 度合いが決まっていない人の値（05_mart.sql が `IFNULL(risk_level, "unknown")` で入れる）。
UNKNOWN_RISK = "unknown"

# 度合いの呼び方。`assessments.LEVELS` を正本にして、判断前の分だけ足す。
RISK_LEVELS: dict[str, str] = {**assessments.LEVELS, UNKNOWN_RISK: "まだ判断していない"}

# 値が空だったときの呼び方（居場所を記録していない人など）。
EMPTY_LABEL = "記載なし"

# 画面に必ず出す断り書き（mart は1時間ごとにしか変わらない）。
NOTE = "1時間ごとに作り直した集計です。入れたばかりの記録は、次に作り直すまで反映されません。"


def _mart_table() -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_mart, MART_TABLE)


def fetch_org_status(org_id: str) -> list[dict[str, Any]]:
    """組織の集計を読む（**読みは1回**）。"""
    sql = (
        f"SELECT {', '.join(COLUMNS)}\n"
        f"FROM {_mart_table()}\n"
        "WHERE org_id = @org_id\n"
        "ORDER BY stage, location_type, risk_level"
    )
    return bq.query(sql, {"org_id": org_id})


def _count(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    return int(value) if isinstance(value, (int, float)) else 0


def _label(value: str, labels: dict[str, str]) -> str:
    if not value:
        return EMPTY_LABEL
    # 呼び方の表に無い値は、値そのものを出す（黙って落とすと人数が合わなくなる）
    return labels.get(value, value)


def _totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {metric: sum(_count(row, metric) for row in rows) for metric in METRICS}


def _group(
    rows: list[dict[str, Any]],
    key: str,
    labels: dict[str, str],
    *,
    include_all: bool,
    default_value: str = "",
) -> list[dict[str, Any]]:
    """`key` ごとに足し合わせる。並びは `labels` の順にそろえる。

    `include_all` が真のときは、0人の区分も出す（段階と度合いは数が少なく、
    「0人」と出したほうが見落としに気づける）。居場所の種類は11あるため、
    記録のある区分だけを出す。

    `default_value` は、値が空だったときに入れる区分。度合いは mart が
    `IFNULL(risk_level, "unknown")` で埋めているが、空が来ても
    「まだ判断していない」に合わせる（同じ意味の区分が2つ並ばないようにする）。
    """
    grouped: dict[str, dict[str, int]] = {}
    if include_all:
        grouped = {value: {metric: 0 for metric in METRICS} for value in labels}

    for row in rows:
        value = str(row.get(key) or default_value)
        bucket = grouped.setdefault(value, {metric: 0 for metric in METRICS})
        for metric in METRICS:
            bucket[metric] += _count(row, metric)

    order = {value: index for index, value in enumerate(labels)}
    items = sorted(grouped.items(), key=lambda pair: (order.get(pair[0], len(order)), pair[0]))
    return [
        {"value": value, "label": _label(value, labels), **counts} for value, counts in items
    ]


def build_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """読んだ行から画面に返す形を作る（接続はしない）。"""
    as_of_values = [row.get("as_of") for row in rows if row.get("as_of")]
    return {
        "as_of": max(as_of_values) if as_of_values else None,
        "total": _totals(rows),
        "by_stage": _group(rows, "stage", survivors.STAGES, include_all=True),
        "by_location_type": _group(rows, "location_type", survivors.LOCATION_TYPES, include_all=False),
        "by_risk_level": _group(
            rows, "risk_level", RISK_LEVELS, include_all=True, default_value=UNKNOWN_RISK
        ),
        "note": NOTE,
    }


def load(org_id: str) -> dict[str, Any]:
    """組織全体の状況を読んで返す（F7）。"""
    rows = fetch_org_status(org_id)
    logger.info("dashboard_loaded: rows=%d", len(rows))
    return build_payload(rows)
