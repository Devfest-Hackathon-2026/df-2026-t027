"""訪問・見守りの記録（F2。要件定義書F2・7-3-1の raw_visits・7-3-2、開発手順書 Step 6-1）。

- **保存するまで記録として扱わない。** Gemini で整えた案を返すだけの処理（`visit_ai`）と、
  人が確かめたものを書く処理（ここ）を分ける（要件定義書F2・10-5の「AI」の列）。
- **文章に書かれていない項目は「記載なし」にする**（要件定義書F2・10-6）。空で保存しない。
- 記録はあとから消さない。版の番号を持つ（要件定義書F2）。
- 更新の案（居場所・健康）は、**案があれば必ず `pending` で保存する**。反映したことは
  `raw_update_applies` に書く（`proposal_status` を `applied` に書き換えない。表は追記だけ）。
  専門職がその場で反映するときも「保存 → 反映」の2段で書く。途中で落ちても確認待ちとして
  残り、`/apply` でやり直せる。
- 一般の支援者が書いた案は、専門職が確かめるまで反映しない（要件定義書F2）。
- ログに記録の本文・氏名・健康の情報を出さない（要件定義書10-2の6・11-3）。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from backend.config import get_settings
from backend.services import bq, health, survivors

# 整える9つの項目（要件定義書F2。値の正本はここ。画面の見出しもこれを使う）。
VISIT_FIELDS: dict[str, str] = {
    "location": "今の居場所",
    "condition": "体調",
    "meal": "食事",
    "sleep": "睡眠",
    "medication": "服薬の状況",
    "environment": "生活の環境",
    "concerns": "困りごと",
    "change": "前回からの変化",
    "next_action": "次に必要な対応",
}

# 文章に書かれていない項目に入れる文字（要件定義書F2・10-6）。推測で埋めない。
NOT_RECORDED = "記載なし"

# 更新の案の状況（raw_visits.proposal_status）。
STATUS_NONE = "none"
STATUS_PENDING = "pending"

# 反映の記録（raw_update_applies.result）。
RESULT_APPLIED = "applied"
RESULT_REJECTED = "rejected"

# 反映できる内容（raw_update_applies.applied_items）。
ITEM_LOCATION = "location"
ITEM_HEALTH = "health"
APPLY_ITEMS = (ITEM_LOCATION, ITEM_HEALTH)

# 入力の上限。
RAW_TEXT_MAX_LENGTH = 2000
ITEM_MAX_LENGTH = 500
REASON_MAX_LENGTH = 300

# 詳細の画面に出す記録の件数（読みを増やさないため上限を置く）。
LIST_LIMIT = 20

# 訪問した日時に許す未来のずれ（端末の時計のずれを見込む）。
FUTURE_TOLERANCE = dt.timedelta(days=1)


class VisitValidationError(ValueError):
    """訪問・見守りの記録の入力が正しくない。"""


# --- 値をそろえる ---


def _clean_item(value: Any, *, label: str) -> str:
    """9つの項目の1つをそろえる。空なら「記載なし」にする（要件定義書F2）。"""
    if value is None:
        return NOT_RECORDED
    if not isinstance(value, str):
        raise VisitValidationError(f"{label}は文章で入れてください")
    text = value.strip()
    if not text:
        return NOT_RECORDED
    if len(text) > ITEM_MAX_LENGTH:
        raise VisitValidationError(f"{label}は{ITEM_MAX_LENGTH}文字以内にしてください")
    return text


def clean_items(items: dict[str, Any] | None) -> dict[str, str]:
    """9つの項目をそろえる（知らない項目は無視し、足りない項目は「記載なし」にする）。"""
    source = items or {}
    return {
        name: _clean_item(source.get(name), label=label) for name, label in VISIT_FIELDS.items()
    }


def is_all_empty(items: dict[str, str]) -> bool:
    """すべての項目が「記載なし」か（中身のない記録を保存しないため）。"""
    return all(value == NOT_RECORDED for value in items.values())


def clean_raw_text(value: Any, *, required: bool = True) -> str | None:
    """支援者が入れた文章をそろえる。"""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise VisitValidationError("入力した内容は文章で入れてください")
    if not text:
        if required:
            raise VisitValidationError("訪問・見守りの内容を入れてください")
        return None
    if len(text) > RAW_TEXT_MAX_LENGTH:
        raise VisitValidationError(f"内容は{RAW_TEXT_MAX_LENGTH}文字以内にしてください")
    return text


def clean_visited_at(value: Any, *, now: dt.datetime | None = None) -> dt.datetime:
    """訪問した日時をそろえる（未指定なら今。未来はずれの範囲までしか許さない）。"""
    current = now or dt.datetime.now(dt.timezone.utc)
    if value is None or value == "":
        return current
    if not isinstance(value, dt.datetime):
        raise VisitValidationError("訪問した日時を読み取れませんでした")
    moment = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if moment - current > FUTURE_TOLERANCE:
        raise VisitValidationError("訪問した日時が未来になっています。日付を確かめてください")
    return moment


def _clean_reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:REASON_MAX_LENGTH]


def normalize_location_proposal(proposal: Any) -> dict[str, Any] | None:
    """居場所の更新の案をそろえる（案が無ければ None）。

    `location_type` が11種類（要件定義書F1）に無いものは**案そのものを落とす**。
    画面に出せない値を案として残すと、反映のときに何を書くか決められない。
    """
    if not isinstance(proposal, dict):
        return None
    if not proposal.get("change_suggested"):
        return None
    location_type = proposal.get("location_type")
    if not isinstance(location_type, str) or location_type not in survivors.LOCATION_TYPES:
        return None
    detail = proposal.get("location_detail")
    detail_text = detail.strip() if isinstance(detail, str) else ""
    if len(detail_text) > survivors.LOCATION_DETAIL_MAX_LENGTH:
        detail_text = detail_text[: survivors.LOCATION_DETAIL_MAX_LENGTH]
    return {
        "change_suggested": True,
        "location_type": location_type,
        "location_type_label": survivors.LOCATION_TYPES[location_type],
        "location_detail": detail_text or None,
        "reason": _clean_reason(proposal.get("reason")),
    }


def normalize_health_proposal(proposal: Any) -> dict[str, Any] | None:
    """健康・介護の更新の案をそろえる（案が無ければ None）。

    項目は `health.clean_fields` でそろえる。**どの項目も空なら案を落とす。**
    """
    if not isinstance(proposal, dict):
        return None
    if not proposal.get("change_suggested"):
        return None
    source = proposal.get("fields") if isinstance(proposal.get("fields"), dict) else proposal
    try:
        fields = health.clean_fields(source)
    except health.HealthValidationError as exc:
        raise VisitValidationError(str(exc)) from exc
    if health.is_empty(fields):
        return None
    return {
        "change_suggested": True,
        "fields": fields,
        "reason": _clean_reason(proposal.get("reason")),
    }


def proposal_status(
    *, location_proposal: dict[str, Any] | None, health_proposal: dict[str, Any] | None
) -> str:
    """案があるかどうかで状況を決める（反映したことは raw_update_applies に書く）。"""
    return STATUS_PENDING if (location_proposal or health_proposal) else STATUS_NONE


# --- 書き込み ---


def new_visit_id() -> str:
    """記録のID（毎回ちがう値）。"""
    return f"vs_{uuid.uuid4().hex}"


def build_visit_row(
    *,
    survivor_id: str,
    org_id: str,
    created_by: str,
    created_role: str,
    visited_at: dt.datetime,
    raw_text: str | None,
    items: dict[str, str],
    location_proposal: dict[str, Any] | None = None,
    health_proposal: dict[str, Any] | None = None,
    ai_used: bool = True,
    visit_id: str | None = None,
    version: int = 1,
) -> dict[str, Any]:
    """`raw_visits` に入れる1行を作る（まだ書かない）。"""
    if not survivor_id or not org_id:
        raise VisitValidationError("記録が決まっていません")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise VisitValidationError("記録の版が読み取れません")
    if is_all_empty(items):
        raise VisitValidationError(
            "どの項目にも内容がありません。少なくとも1つは入れてください"
        )
    row: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "visit_id": visit_id or new_visit_id(),
        "version": version,
        "visited_at": visited_at,
        "raw_text": raw_text,
        "location_proposal": location_proposal,
        "health_proposal": health_proposal,
        "proposal_status": proposal_status(
            location_proposal=location_proposal, health_proposal=health_proposal
        ),
        "ai_used": bool(ai_used),
    }
    row.update(items)
    return row


def save_visit(row: dict[str, Any]) -> dict[str, Any]:
    """作った行を `raw_visits` に追記する。"""
    bq.insert("raw_visits", row)
    return row


def record_apply(
    *,
    survivor_id: str,
    org_id: str,
    visit_id: str,
    created_by: str,
    created_role: str,
    applied_items: list[str],
    result: str,
) -> dict[str, Any]:
    """反映したこと（しなかったこと）を `raw_update_applies` に書く（要件定義書7-3-2）。"""
    if result not in (RESULT_APPLIED, RESULT_REJECTED):
        raise VisitValidationError("反映の結果が決まっていません")
    items = [item for item in applied_items if item in APPLY_ITEMS]
    if result == RESULT_APPLIED and not items:
        raise VisitValidationError("反映する内容を選んでください")
    row = {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "visit_id": visit_id,
        "applied_items": items,
        "result": result,
    }
    bq.insert("raw_update_applies", row)
    return row


# --- 読み ---


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


_VISIT_COLUMNS = (
    "visit_id",
    "version",
    "visited_at",
    "raw_text",
    *VISIT_FIELDS.keys(),
    "location_proposal",
    "health_proposal",
    "proposal_status",
    "ai_used",
    "recorded_at",
    "recorded_by",
    "recorded_role",
    "applied_at",
    "apply_result",
    "is_pending",
)


def _select_visits() -> str:
    columns = ",\n       ".join(_VISIT_COLUMNS)
    return f"SELECT {columns}\nFROM {_staging_table('stg_visits')}\n"


def list_visits(*, survivor_id: str, org_id: str, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """その人の記録を新しい順に読む（`stg_visits` は版の最新だけを返す）。"""
    if not survivor_id or not org_id:
        return []
    sql = (
        _select_visits()
        + "WHERE survivor_id = @survivor_id AND org_id = @org_id\n"
        + "ORDER BY visited_at DESC, recorded_at DESC\n"
        + f"LIMIT {int(limit)}"
    )
    return bq.query(sql, {"survivor_id": survivor_id, "org_id": org_id})


def fetch_visit(*, visit_id: str, survivor_id: str, org_id: str) -> dict[str, Any] | None:
    """記録を1件読む（反映のときに案の中身を読み直すのに使う）。"""
    if not visit_id or not survivor_id or not org_id:
        return None
    sql = (
        _select_visits()
        + "WHERE visit_id = @visit_id AND survivor_id = @survivor_id AND org_id = @org_id"
    )
    return bq.query_one(
        sql, {"visit_id": visit_id, "survivor_id": survivor_id, "org_id": org_id}
    )


def as_dict(value: Any) -> dict[str, Any] | None:
    """JSON の列を辞書にする（BigQuery の版によって文字列で返ることがある）。"""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def visit_payload(row: dict[str, Any], *, supporter_names: dict[str, str] | None = None) -> dict[str, Any]:
    """支援者の画面に返す1件分（**本人には返さない**。要件定義書10-3・F4）。"""
    names = supporter_names or {}
    items = {name: row.get(name) for name in VISIT_FIELDS}
    location_proposal = as_dict(row.get("location_proposal"))
    health_proposal = as_dict(row.get("health_proposal"))
    return {
        "visit_id": row.get("visit_id"),
        "version": row.get("version"),
        "visited_at": row.get("visited_at"),
        "raw_text": row.get("raw_text"),
        "items": items,
        "location_proposal": location_proposal,
        "health_proposal": health_proposal,
        "proposal_status": row.get("proposal_status"),
        "is_pending": bool(row.get("is_pending")),
        "apply_result": row.get("apply_result"),
        "applied_at": row.get("applied_at"),
        "ai_used": bool(row.get("ai_used")),
        "recorded_at": row.get("recorded_at"),
        "recorded_role": row.get("recorded_role"),
        "recorded_by_name": names.get(str(row.get("recorded_by") or ""), ""),
    }


def recent_items_for_ai(rows: list[dict[str, Any]], *, limit: int = 3) -> str:
    """Gemini に渡す「直近の記録」の文章にする（前回からの変化を見るため）。"""
    if not rows:
        return "（まだ記録がありません）"
    lines: list[str] = []
    for row in rows[:limit]:
        visited = row.get("visited_at")
        when = visited.strftime("%Y-%m-%d") if isinstance(visited, dt.datetime) else ""
        parts = [
            f"{label}：{row.get(name) or NOT_RECORDED}" for name, label in VISIT_FIELDS.items()
        ]
        lines.append(f"- {when} " + "／".join(parts))
    return "\n".join(lines)
