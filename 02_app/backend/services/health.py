"""健康・介護の情報（F3。要件定義書F3・7-3-1の raw_health_versions、開発手順書 Step 6-2）。

- **上書きはしない。** 更新のたびに版を1つ足す（要件定義書F3「版として残す」）。
  最新の版は `stg_health_latest` が版の番号から決める。
- 誰が書いたかを `source` に残す（`professional`・`self`・`visit_proposal`）。
  **本人が書いたものは「本人の申告」と分かるようにする**（要件定義書F3）。
- 一般の支援者は**見るだけ**。更新は F2 の更新の案（`visit_proposal`）を通す（要件定義書F3）。
- 要配慮個人情報を扱う。**ログに中身を出さない**（要件定義書11-3）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from backend.config import get_settings
from backend.services import bq

# 誰の情報か（raw_health_versions.source）
SOURCE_PROFESSIONAL = "professional"
SOURCE_SELF = "self"
SOURCE_VISIT_PROPOSAL = "visit_proposal"

SOURCE_LABELS: dict[str, str] = {
    SOURCE_PROFESSIONAL: "専門職が記入",
    SOURCE_SELF: "ご本人の申告",
    SOURCE_VISIT_PROPOSAL: "訪問の記録から反映",
}

# 立場から `source` を決める（要求に書かれた値は使わない）。
_SOURCE_BY_ROLE = {"professional": SOURCE_PROFESSIONAL, "survivor": SOURCE_SELF}

# 文章で入れる項目（要件定義書F3の項目のうち、1つの文章で書くもの）。
TEXT_FIELDS: dict[str, str] = {
    "allergies": "アレルギー",
    "primary_clinic": "かかりつけの医療機関",
    "care_level": "要介護度",
    "assistance_mobility": "移動で必要な介助",
    "assistance_meal": "食事で必要な介助",
    "assistance_toilet": "排せつで必要な介助",
    "dementia_notes": "認知症の有無と配慮すること",
    "family_contact": "連絡してほしい家族など",
}

# 複数書く項目（配列で持つ。raw_health_versions は ARRAY<STRING>）。
ARRAY_FIELDS: dict[str, str] = {
    "chronic_diseases": "持病",
    "medications": "飲んでいる薬",
    "medical_needs": "必要な医療（透析・在宅酸素・たんの吸引など）",
}

FIELD_LABELS: dict[str, str] = {**ARRAY_FIELDS, **TEXT_FIELDS}

# 画面と Gemini の案に出す並び（要件定義書F3の順）。
FIELD_ORDER = (
    "chronic_diseases",
    "medications",
    "allergies",
    "primary_clinic",
    "medical_needs",
    "care_level",
    "assistance_mobility",
    "assistance_meal",
    "assistance_toilet",
    "dementia_notes",
    "family_contact",
)

# 要介護度の選択肢。**要件定義書F3は値を決めていない**ため、介護保険の区分に合わせてここで決めた
# （開発ログの要確認に記録する）。空（未記入）も許す。
CARE_LEVELS = (
    "なし",
    "要支援1",
    "要支援2",
    "要介護1",
    "要介護2",
    "要介護3",
    "要介護4",
    "要介護5",
    "不明",
)

# 入力の上限（間違えて長文を貼ったときに表に入れない）。
TEXT_MAX_LENGTH = 300
ARRAY_ITEM_MAX_LENGTH = 100
ARRAY_MAX_ITEMS = 20


class HealthValidationError(ValueError):
    """健康・介護の情報の入力が正しくない。"""


def source_for_role(role: str | None) -> str:
    """立場から `source` を決める。一般の支援者は直接更新できない（要件定義書F3）。"""
    source = _SOURCE_BY_ROLE.get(role or "")
    if source is None:
        raise HealthValidationError("健康・介護の情報を更新できるのは、専門職とご本人だけです")
    return source


def _clean_text(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HealthValidationError(f"{label}は文章で入れてください")
    text = value.strip()
    if not text:
        return None
    if len(text) > TEXT_MAX_LENGTH:
        raise HealthValidationError(f"{label}は{TEXT_MAX_LENGTH}文字以内にしてください")
    return text


def _clean_array(value: Any, *, label: str) -> list[str]:
    """配列の項目をそろえる（空の要素を落とし、重複を消す。並びは入れた順）。

    画面からは改行・読点で区切った文章でも、配列でも受け取れるようにする
    （スマートフォンで入れやすい形にするため。要件定義書F2の3）。
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace("、", "\n").splitlines()
    elif isinstance(value, (list, tuple)):
        items = []
        for part in value:
            if part is None:
                continue
            if not isinstance(part, str):
                raise HealthValidationError(f"{label}は文章で入れてください")
            items.append(part)
    else:
        raise HealthValidationError(f"{label}は文章で入れてください")

    cleaned: list[str] = []
    for item in items:
        text = item.strip()
        if not text:
            continue
        if len(text) > ARRAY_ITEM_MAX_LENGTH:
            raise HealthValidationError(
                f"{label}の1つは{ARRAY_ITEM_MAX_LENGTH}文字以内にしてください"
            )
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > ARRAY_MAX_ITEMS:
        raise HealthValidationError(f"{label}は{ARRAY_MAX_ITEMS}件までにしてください")
    return cleaned


def validate_care_level(value: Any) -> str | None:
    """要介護度を確かめて返す（空は許す）。"""
    text = _clean_text(value, label="要介護度")
    if text is None:
        return None
    if text not in CARE_LEVELS:
        raise HealthValidationError("要介護度は次のどれかにしてください：" + "・".join(CARE_LEVELS))
    return text


def clean_fields(fields: dict[str, Any] | None) -> dict[str, Any]:
    """画面や Gemini の案から受け取った項目をそろえる（知らない項目は無視する）。"""
    source = fields or {}
    cleaned: dict[str, Any] = {}
    for name, label in ARRAY_FIELDS.items():
        cleaned[name] = _clean_array(source.get(name), label=label)
    for name, label in TEXT_FIELDS.items():
        if name == "care_level":
            cleaned[name] = validate_care_level(source.get(name))
            continue
        cleaned[name] = _clean_text(source.get(name), label=label)
    return cleaned


def is_empty(fields: dict[str, Any]) -> bool:
    """どの項目も入っていないか（空の案を保存しないため）。"""
    return not any(fields.get(name) for name in FIELD_ORDER)


def merge(current: dict[str, Any] | None, changes: dict[str, Any]) -> dict[str, Any]:
    """最新の版に、**空でない項目だけ**を重ねる（F2 の更新の案を反映するときに使う）。

    案に入っていない項目を空で上書きすると、専門職が入れた情報が消える。
    消すのは「人が画面で空にして保存したとき」だけにする。
    """
    merged = clean_fields(current or {})
    for name in FIELD_ORDER:
        value = changes.get(name)
        if not value:
            continue
        if name in ARRAY_FIELDS:
            merged[name] = _clean_array(value, label=ARRAY_FIELDS[name])
        elif name == "care_level":
            merged[name] = validate_care_level(value)
        else:
            merged[name] = _clean_text(value, label=TEXT_FIELDS[name])
    return merged


def next_version(current: dict[str, Any] | None) -> int:
    """次の版の番号（まだ無ければ1）。"""
    if not current:
        return 1
    version = current.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise HealthValidationError("健康・介護の情報の版が読み取れません")
    return version + 1


def build_row(
    *,
    survivor_id: str,
    org_id: str,
    created_by: str,
    created_role: str,
    fields: dict[str, Any],
    source: str,
    version: int,
) -> dict[str, Any]:
    """`raw_health_versions` に入れる1行を作る（まだ書かない）。

    組織がない本人（追加修正②）も自分で書けるように、組織のIDは空でもよい。
    """
    if not survivor_id:
        raise HealthValidationError("記録が決まっていません")
    if source not in SOURCE_LABELS:
        raise HealthValidationError("健康・介護の情報の出どころが決まっていません")
    row: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "version": version,
        "source": source,
    }
    row.update(clean_fields(fields))
    return row


def save(
    *,
    survivor_id: str,
    org_id: str,
    created_by: str,
    created_role: str,
    fields: dict[str, Any],
    source: str,
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    """版を1つ足して追記する。"""
    row = build_row(
        survivor_id=survivor_id,
        org_id=org_id,
        created_by=created_by,
        created_role=created_role,
        fields=fields,
        source=source,
        version=next_version(current),
    )
    bq.insert("raw_health_versions", row)
    return row


# --- 読み ---


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


def fetch_latest(*, survivor_id: str, org_id: str | None = None) -> dict[str, Any] | None:
    """最新の版を1件読む（まだ無ければ None）。

    **被災者のIDだけで読む**（追加修正②⑦）。健康・介護の情報は記録についていくもので、
    組織がない本人が書いた版も、本人が組織を変える前の版も、いまの記録の続きとして読む
    （`stg_health_latest` は被災者のIDごとに最新の版を取る）。組織で絞ると、組織を変えた
    あとに最新の版が見えず、次の版の番号も1に戻ってしまう。
    見てよいかは、呼ぶ側が `shared.load_visible` で先に確かめる。
    `org_id` は前の呼び方との互換のために受け取るだけで、読みには使わない。
    """
    del org_id
    if not survivor_id:
        return None
    sql = (
        "SELECT *\n"
        f"FROM {_staging_table('stg_health_latest')}\n"
        "WHERE survivor_id = @survivor_id"
    )
    return bq.query_one(sql, {"survivor_id": survivor_id})


def payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """画面に返す形（項目の並びと、誰が書いたかの名前を付ける）。"""
    if not row:
        return None
    fields: dict[str, Any] = {}
    for name in FIELD_ORDER:
        value = row.get(name)
        fields[name] = list(value or []) if name in ARRAY_FIELDS else value
    return {
        "version": row.get("version"),
        "source": row.get("source"),
        "source_label": SOURCE_LABELS.get(row.get("source") or "", ""),
        "updated_at": row.get("updated_at"),
        "updated_role": row.get("updated_role"),
        "fields": fields,
    }


def summary_for_ai(row: dict[str, Any] | None) -> str:
    """Gemini に渡す「いま登録されている健康・介護の情報」の文章にする。"""
    if not row:
        return "（まだ登録がありません）"
    lines: list[str] = []
    for name in FIELD_ORDER:
        value = row.get(name)
        if name in ARRAY_FIELDS:
            text = "・".join(str(item) for item in (value or []))
        else:
            text = str(value) if value else ""
        lines.append(f"- {FIELD_LABELS[name]}：{text or '記載なし'}")
    return "\n".join(lines)
