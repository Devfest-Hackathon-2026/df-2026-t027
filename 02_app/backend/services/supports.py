"""F6 受けられる支援と申請の状況（要件定義書F6・7-3-1、開発手順書 Step 10）。

守ること：

- **上書きはしない。** 示したとき（`suggested`）と状況を変えたとき（`status_changed`）を
  `raw_support_events` に追記し、いまの状況は `stg_supports_latest` から読む（要件定義書10-4）。
- **支援の記録のIDは「被災者と制度の組み合わせ」で決める**（`sp_` ＋ uuid5）。同じ制度を
  2回示しても記録が2本にならない（Step 4 の記録IDと同じ考え方）。
- **示す制度は `stg_programs` にあるものだけ。** AIが返したIDは渡した一覧と照らす
  （`services/support_ai.py`。要件定義書10-6）。
- 画面に出す文言・選択肢の正本はこのファイル（画面に書き写すとずれる。Step 8 と同じ）。
- ログに氏名・住宅の被害の程度を出さない（要件定義書10-2の6・11-3）。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from backend.config import get_settings
from backend.services import bq

logger = logging.getLogger(__name__)

# --- 選択肢（要件定義書F6は値を決めていないため、ここで決めた） ---

# 住宅の被害の程度。被害認定の区分（全壊〜一部損壊）に、支援金の対象になる
# 「解体」「長期避難」を加えた（出典［4］［17］が対象として挙げているため）。
HOUSING_DAMAGES: dict[str, str] = {
    "total": "全壊",
    "demolished": "解体（住宅をやむを得ず解体した）",
    "long_term_evacuation": "長期避難（住宅に長く住めない）",
    "major_half": "大規模半壊",
    "medium_half": "中規模半壊",
    "half": "半壊",
    "partial": "一部損壊",
    "none": "被害なし",
    "unknown": "分からない",
}

# 再建の方法の意向（要件定義書F6の「建設・購入／補修／賃借など」）。
REBUILD_INTENTS: dict[str, str] = {
    "rebuild": "建設・購入",
    "repair": "補修",
    "rent": "賃借（公営住宅を除く）",
    "undecided": "決めていない",
}

# 申請の状況（要件定義書F6・7-3-1）。値は要件定義書が決めている。
STATUS_NOT_APPLIED = "not_applied"
STATUS_APPLYING = "applying"
STATUS_RECEIVED = "received"
STATUS_NOT_ELIGIBLE = "not_eligible"
STATUSES: dict[str, str] = {
    STATUS_NOT_APPLIED: "未申請",
    STATUS_APPLYING: "申請中",
    STATUS_RECEIVED: "受給済み",
    STATUS_NOT_ELIGIBLE: "対象外",
}

# 記録の種類（要件定義書7-3-1 の `action`）。
ACTION_SUGGESTED = "suggested"
ACTION_STATUS_CHANGED = "status_changed"
ACTIONS = (ACTION_SUGGESTED, ACTION_STATUS_CHANGED)

# 本人の画面に出す「次にすること」（要件定義書F6の「次にすること」。文言の正本はここ）。
NEXT_ACTIONS: dict[str, str] = {
    STATUS_NOT_APPLIED: "必要な書類をそろえて、申請先の窓口に申し込んでください",
    STATUS_APPLYING: "結果の連絡を待ってください。分からないことは相談できます",
    STATUS_RECEIVED: "この制度の手続きは終わっています",
    STATUS_NOT_ELIGIBLE: "この制度は対象外です。ほかに使える支援がないか相談できます",
}

# 期限が近いと見なす日数（要件定義書F6の「期限が近い未申請の制度」。
# 要件定義書は日数を決めていないため、ここで決めた）。
DEADLINE_SOON_DAYS = 30

# 本人のホームの上に知らせを出す日数（使いやすさの改修の計画.md 11-2。期限切れも含む）。
SURVIVOR_ALERT_DAYS = 3

# 必ず画面に出す断り書き（要件定義書F6の最後。文言の正本はここ）。
DISCLAIMER = "ここに出るのは目安です。受けられるかどうかは自治体が決めます"

# 期限の知らせの文言。
DEADLINE_PASSED_NOTICE = "申請の期限を過ぎています。窓口に相談してください"

# 長さの上限と読む件数。
REASON_MAX_LENGTH = 500
LIST_LIMIT = 50
PROGRAM_LIMIT = 50

# AIが返した期限として受け取る範囲（過去1年〜10年先）。これを外れた日付は落とす。
DEADLINE_PAST_LIMIT_DAYS = 365
DEADLINE_FUTURE_LIMIT_DAYS = 365 * 10

# 支援の記録のIDを「被災者と制度の組み合わせ」から決めるための名前空間。
_SUPPORT_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "gdgoc:support")


class SupportValidationError(ValueError):
    """F6 の入力が受け取れない（画面には 400 で返す）。"""


# --- 値の検査 ---


def validate_housing_damage(value: Any) -> str:
    """住宅の被害の程度（選択肢の外は受け取らない）。"""
    if isinstance(value, str) and value in HOUSING_DAMAGES:
        return value
    raise SupportValidationError("住宅の被害の程度を選んでください")


def validate_rebuild_intent(value: Any) -> str:
    """再建の方法の意向（選択肢の外は受け取らない）。"""
    if isinstance(value, str) and value in REBUILD_INTENTS:
        return value
    raise SupportValidationError("再建の方法の意向を選んでください")


def optional_housing_damage(value: Any) -> str | None:
    """住宅の被害の程度（空なら None。制度の一覧から記録するときだけ使う）。

    一覧から記録するときは被害の程度を聞かない（使いやすさの改修の計画.md 6-2）。
    空でない値は、今までどおり選択肢と照らす。
    """
    if value is None or value == "":
        return None
    return validate_housing_damage(value)


def optional_rebuild_intent(value: Any) -> str | None:
    """再建の方法の意向（空なら None。制度の一覧から記録するときだけ使う）。"""
    if value is None or value == "":
        return None
    return validate_rebuild_intent(value)


def validate_status(value: Any) -> str:
    """申請の状況（4つの外は受け取らない。要件定義書F6）。"""
    if isinstance(value, str) and value in STATUSES:
        return value
    raise SupportValidationError(
        "申請の状況は、未申請・申請中・受給済み・対象外のどれかにしてください"
    )


def clean_reason(value: Any) -> str | None:
    """受けられそうな理由（空なら None）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SupportValidationError("理由は文字で入れてください")
    text = value.strip()
    if not text:
        return None
    return text[:REASON_MAX_LENGTH]


def clean_deadline(value: Any, *, today: dt.date | None = None) -> dt.date | None:
    """申請の期限を日付にする（読み取れない値・遠すぎる値は落として None にする）。

    期限の決まり（「災害発生日から13月以内」など）から日付を出させるため、
    **作り出した日付をそのまま保存しない**ように範囲を確かめる。
    """
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        parsed = value.date()
    elif isinstance(value, dt.date):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = dt.date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    else:
        return None
    base = today or dt.datetime.now(dt.timezone.utc).date()
    if parsed < base - dt.timedelta(days=DEADLINE_PAST_LIMIT_DAYS):
        return None
    if parsed > base + dt.timedelta(days=DEADLINE_FUTURE_LIMIT_DAYS):
        return None
    return parsed


# --- 記録のID と 行づくり ---


def support_id_for(*, org_id: str, survivor_id: str, program_id: str) -> str:
    """支援の記録のID（被災者と制度の組み合わせで必ず同じ値になる）。

    同じ制度を2回示しても記録が2本にならないようにする（`stg_supports_latest` は
    `support_id` ごとに最新を取るため、IDが分かれると未申請が二重に数えられる）。

    全国共通の制度（追加修正②）は `org_id` を空で渡す。組織がない本人が示した記録と、
    のちに組織につないでから示した記録が、同じ1本になる。
    """
    if not survivor_id or not program_id:
        raise SupportValidationError("支援の記録を決められません")
    key = f"{org_id or ''}/{survivor_id}/{program_id}"
    return f"sp_{uuid.uuid5(_SUPPORT_ID_NAMESPACE, key).hex}"


def _base_row(
    *, org_id: str, survivor_id: str, created_by: str, created_role: str
) -> dict[str, Any]:
    # 組織がない本人（追加修正②）の記録は、組織のIDを空にして残す
    if not survivor_id:
        raise SupportValidationError("記録が決まっていません")
    return {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id or "",
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
    }


def build_suggested_row(
    *,
    org_id: str,
    survivor_id: str,
    created_by: str,
    created_role: str,
    program_id: str,
    reason: Any,
    deadline: Any,
    housing_damage: Any,
    rebuild_intent: Any,
    program_org_id: str | None = None,
    conditions_optional: bool = False,
) -> dict[str, Any]:
    """示した支援を `raw_support_events` に入れる1行を作る（まだ書かない）。

    示した時点の状況は**未申請**から始める（要件定義書F6）。
    `program_org_id` は制度を持つ組織（全国共通の制度は空。追加修正②）。記録のIDは
    この値で決める（渡さなければ `org_id` と同じ。組織の制度は今までどおりのID）。
    `conditions_optional` は、制度の一覧から本人が記録するときだけ真にする。
    住宅の被害の程度と再建の意向を空のまま残せる（使いやすさの改修の計画.md 6-2）。
    """
    program = (program_id or "").strip()
    if not program:
        raise SupportValidationError("制度が決まっていません")
    row = _base_row(
        org_id=org_id,
        survivor_id=survivor_id,
        created_by=created_by,
        created_role=created_role,
    )
    id_org_id = org_id if program_org_id is None else program_org_id
    if conditions_optional:
        damage = optional_housing_damage(housing_damage)
        intent = optional_rebuild_intent(rebuild_intent)
    else:
        damage = validate_housing_damage(housing_damage)
        intent = validate_rebuild_intent(rebuild_intent)
    row.update(
        {
            "support_id": support_id_for(
                org_id=id_org_id, survivor_id=survivor_id, program_id=program
            ),
            "program_id": program,
            "action": ACTION_SUGGESTED,
            "reason": clean_reason(reason),
            "status": STATUS_NOT_APPLIED,
            "deadline": clean_deadline(deadline),
            "housing_damage": damage,
            "rebuild_intent": intent,
        }
    )
    return row


def build_status_row(
    *,
    current: dict[str, Any],
    created_by: str,
    created_role: str,
    status: Any,
    deadline: Any = None,
) -> dict[str, Any]:
    """申請の状況を変える1行を作る（**前の行は消さない**。要件定義書F6・10-4）。

    期限を渡さなかったときは、いまの期限をそのまま引き継ぐ（None で消さない）。
    """
    if not current:
        raise SupportValidationError("支援の記録が見つかりません")
    row = _base_row(
        org_id=str(current.get("org_id") or ""),
        survivor_id=str(current.get("survivor_id") or ""),
        created_by=created_by,
        created_role=created_role,
    )
    kept_deadline = current.get("deadline") if deadline is None else deadline
    support_id = str(current.get("support_id") or "")
    if not support_id:
        raise SupportValidationError("支援の記録が見つかりません")
    row.update(
        {
            "support_id": support_id,
            "program_id": str(current.get("program_id") or "") or None,
            "action": ACTION_STATUS_CHANGED,
            "reason": None,
            "status": validate_status(status),
            "deadline": clean_deadline(kept_deadline),
            "housing_damage": current.get("housing_damage"),
            "rebuild_intent": current.get("rebuild_intent"),
        }
    )
    return row


def save(row: dict[str, Any]) -> dict[str, Any]:
    """作った行を `raw_support_events` に追記する。"""
    bq.insert("raw_support_events", row)
    return row


# --- 読み ---


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


_PROGRAM_COLUMNS = (
    "program_id",
    "org_id",
    "version",
    "program_name",
    "conditions",
    "benefit",
    "apply_to",
    "deadline_rule",
    "documents",
    "source_url",
)


def is_nationwide(row: dict[str, Any]) -> bool:
    """全国共通の制度か（`raw_programs.org_id` が空の行。追加修正②）。"""
    return not row.get("org_id")


def list_programs(*, org_id: str | None, limit: int = PROGRAM_LIMIT) -> list[dict[str, Any]]:
    """本人に出せる支援制度を読む（`stg_programs`。AIに渡す一覧の正本）。

    - 組織に入っている本人：その組織の制度と、全国共通の制度（`org_id` が空）の両方。
    - 組織がない本人（追加修正②）：全国共通の制度だけ。

    同じ名前の制度が組織にも全国共通にもあるときは、**組織のものを残す**（組織が自分の
    地域に合わせて入れ直した内容を優先し、同じ制度が2つ並ばないようにする）。
    """
    columns = ",\n       ".join(_PROGRAM_COLUMNS)
    sql = (
        f"SELECT {columns}\n"
        f"FROM {_staging_table('stg_programs')}\n"
        "WHERE IFNULL(org_id, '') = '' OR org_id = @org_id\n"
        "ORDER BY program_name ASC\n"
        f"LIMIT {int(limit)}"
    )
    rows = bq.query(sql, {"org_id": org_id or ""})
    org_names = {
        str(row.get("program_name") or "") for row in rows if not is_nationwide(row)
    }
    return [
        row
        for row in rows
        if not (is_nationwide(row) and str(row.get("program_name") or "") in org_names)
    ]


_SUPPORT_SELECT = (
    "SELECT s.support_id,\n"
    "       s.survivor_id,\n"
    "       s.org_id,\n"
    "       s.program_id,\n"
    "       s.suggested_reason,\n"
    "       s.suggested_at,\n"
    "       s.status,\n"
    "       s.deadline,\n"
    "       s.days_to_deadline,\n"
    "       s.housing_damage,\n"
    "       s.rebuild_intent,\n"
    "       s.updated_at,\n"
    "       s.updated_by,\n"
    "       p.program_name,\n"
    "       p.conditions,\n"
    "       p.benefit,\n"
    "       p.apply_to,\n"
    "       p.deadline_rule,\n"
    "       p.documents,\n"
    "       p.source_url\n"
)


def _support_from(table: str) -> str:
    """支援の状況と制度の情報を**1回の読み**で取る（詳細の読みを増やさないため）。

    全国共通の制度（`org_id` が空。追加修正②）は、どの組織の記録ともつなぐ。
    """
    return (
        f"FROM {_staging_table(table)} AS s\n"
        f"LEFT JOIN {_staging_table('stg_programs')} AS p\n"
        "  ON p.program_id = s.program_id\n"
        " AND (p.org_id = s.org_id OR IFNULL(p.org_id, '') = '')\n"
    )


def list_for_survivor(
    *, survivor_id: str, org_id: str | None = None, limit: int = LIST_LIMIT
) -> list[dict[str, Any]]:
    """その人に示した支援を、期限の近い未申請から読む。

    **被災者のIDだけで読む**（追加修正②⑦）。組織がないときに示した支援も、本人が組織を
    変える前に示した支援も、その人の記録の続きとして出す。見てよいかは、呼ぶ側が
    `shared.load_visible` で先に確かめる。`org_id` は互換のために受け取るだけで使わない。
    """
    del org_id
    if not survivor_id:
        return []
    sql = (
        _SUPPORT_SELECT
        + _support_from("stg_supports_latest")
        + "WHERE s.survivor_id = @survivor_id\n"
        "ORDER BY IF(s.status = @not_applied, 0, 1) ASC,\n"
        "         s.deadline ASC NULLS LAST,\n"
        "         s.suggested_at DESC\n"
        f"LIMIT {int(limit)}"
    )
    return bq.query(
        sql,
        {"survivor_id": survivor_id, "not_applied": STATUS_NOT_APPLIED},
    )


def fetch_support(
    *, support_id: str, survivor_id: str, org_id: str | None = None
) -> dict[str, Any] | None:
    """支援の記録を1件読む（**その人のものだけ**。要件定義書10-3）。

    被災者のIDで絞る（追加修正②⑦）。記録を見てよいかは、呼ぶ側が `shared.load_visible`
    で先に確かめる。`org_id` は互換のために受け取るだけで使わない。
    """
    del org_id
    if not support_id or not survivor_id:
        return None
    sql = (
        _SUPPORT_SELECT
        + _support_from("stg_supports_latest")
        + "WHERE s.support_id = @support_id\n"
        "  AND s.survivor_id = @survivor_id"
    )
    return bq.query_one(sql, {"support_id": support_id, "survivor_id": survivor_id})


# --- 画面に返す形 ---


def days_to_deadline(row: dict[str, Any], *, today: dt.date | None = None) -> int | None:
    """期限まであと何日か（ビューが計算した値を使い、無ければ自分で数える）。"""
    value = row.get("days_to_deadline")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    deadline = row.get("deadline")
    if isinstance(deadline, dt.datetime):
        deadline = deadline.date()
    if not isinstance(deadline, dt.date):
        return None
    base = today or dt.datetime.now(dt.timezone.utc).date()
    return (deadline - base).days


def is_due_soon(status: Any, days: int | None) -> bool:
    """期限が近い未申請か（要件定義書F6の知らせの条件）。"""
    if status != STATUS_NOT_APPLIED or days is None:
        return False
    return days <= DEADLINE_SOON_DAYS


def deadline_notice(status: Any, days: int | None) -> str | None:
    """期限の知らせ（近い・過ぎた）。未申請でないときは出さない。"""
    if status != STATUS_NOT_APPLIED or days is None:
        return None
    if days < 0:
        return DEADLINE_PASSED_NOTICE
    if days > DEADLINE_SOON_DAYS:
        return None
    if days == 0:
        return "申請の期限は今日です"
    return f"申請の期限まであと{days}日です"


def payload(
    row: dict[str, Any], *, role: str = "", today: dt.date | None = None
) -> dict[str, Any]:
    """支援1件分（制度の情報・状況・期限の知らせ・次にすること）。

    制度の情報（申請先・期限の決まり・必要な書類・出典）は `stg_programs` の値を
    そのまま返す。**AIに作らせない**（要件定義書F6）。
    """
    status = str(row.get("status") or STATUS_NOT_APPLIED)
    days = days_to_deadline(row, today=today)
    item = {
        "support_id": row.get("support_id"),
        "program_id": row.get("program_id"),
        "program_name": row.get("program_name") or "",
        "conditions": row.get("conditions"),
        "benefit": row.get("benefit"),
        "apply_to": row.get("apply_to"),
        "deadline_rule": row.get("deadline_rule"),
        "documents": row.get("documents"),
        "source_url": row.get("source_url"),
        "reason": row.get("suggested_reason"),
        "suggested_at": row.get("suggested_at"),
        "status": status,
        "status_label": STATUSES.get(status, ""),
        "next_action": NEXT_ACTIONS.get(status, ""),
        "deadline": row.get("deadline"),
        "days_to_deadline": days,
        "due_soon": is_due_soon(status, days),
        "deadline_notice": deadline_notice(status, days),
        "housing_damage": row.get("housing_damage"),
        "housing_damage_label": HOUSING_DAMAGES.get(str(row.get("housing_damage") or ""), ""),
        "rebuild_intent": row.get("rebuild_intent"),
        "rebuild_intent_label": REBUILD_INTENTS.get(str(row.get("rebuild_intent") or ""), ""),
        "updated_at": row.get("updated_at"),
        # 目安であることは1件ごとに添える（画面のどこに出しても落ちないように）
        "disclaimer": DISCLAIMER,
    }
    if role == "professional":
        # 誰が変えたかは専門職にだけ返す（本人の画面には出さない）
        item["updated_by"] = row.get("updated_by")
    return item


def program_payload(row: dict[str, Any]) -> dict[str, Any]:
    """制度1件分（画面の一覧と、示す前の確認に使う）。"""
    return {
        "program_id": row.get("program_id"),
        "program_name": row.get("program_name") or "",
        "conditions": row.get("conditions"),
        "benefit": row.get("benefit"),
        "apply_to": row.get("apply_to"),
        "deadline_rule": row.get("deadline_rule"),
        "documents": row.get("documents"),
        "source_url": row.get("source_url"),
        # 全国共通の制度か（追加修正②。画面で「全国共通」と印を付ける）
        "nationwide": is_nationwide(row),
    }


def alerts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """ホームと一覧に出す「期限の近い未申請」（要件定義書F6）。

    **一覧の読み（`int_survivor_list`）をそのまま使う**ので BigQuery の読みは増えない
    （Step 7 の「対応が必要な人」と同じ作り）。

    - `survivors`・`count`：期限まで30日以内（期限切れを含む）。ホームの知らせの数
    - `all_survivors`・`all_count`：期限のある未申請すべて。画面の「すべて」の絞り込みで使う
      （使いやすさの改修の計画.md 11章。期日で絞るのは画面の中で行い、読みを増やさない）
    """
    items: list[dict[str, Any]] = []
    for row in rows:
        unapplied = row.get("unapplied_count") or 0
        days = row.get("days_to_nearest_deadline")
        if not unapplied or not isinstance(days, int) or isinstance(days, bool):
            continue
        items.append(
            {
                "survivor_id": row.get("survivor_id"),
                "full_name": row.get("full_name"),
                "unapplied_count": unapplied,
                "nearest_unapplied_deadline": row.get("nearest_unapplied_deadline"),
                "days_to_nearest_deadline": days,
                "notice": deadline_notice(STATUS_NOT_APPLIED, days),
            }
        )
    items.sort(key=lambda item: item["days_to_nearest_deadline"])
    soon = [item for item in items if item["days_to_nearest_deadline"] <= DEADLINE_SOON_DAYS]
    return {
        "count": len(soon),
        "survivors": soon,
        "all_count": len(items),
        "all_survivors": items,
        "soon_days": DEADLINE_SOON_DAYS,
        "disclaimer": DISCLAIMER,
    }


def summary_for_ai(row: dict[str, Any]) -> str:
    """Gemini に渡す制度1件分の文章（渡した制度だけから選ばせるため、IDを必ず入れる）。"""
    item = program_payload(row)
    parts = [
        f"- ID：{item['program_id']}",
        f"名前：{item['program_name']}",
        f"対象になる条件：{item['conditions'] or '記載なし'}",
        f"受けられる内容：{item['benefit'] or '記載なし'}",
        f"申請先：{item['apply_to'] or '記載なし'}",
        f"申請の期限の決まり：{item['deadline_rule'] or '記載なし'}",
        f"必要な書類：{item['documents'] or '記載なし'}",
    ]
    return "／".join(parts)
