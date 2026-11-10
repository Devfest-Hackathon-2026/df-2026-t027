"""引き継ぎシート（F3。要件定義書F3・7-3-1の raw_handovers、開発手順書 Step 9-3）。

- **項目は固定。** F3 の項目（健康・介護の情報）と、直近の記録・相談の要約、気をつけること
  を載せる（要件定義書F3）。
- 要約と気をつけることは Gemini が作り、**作った人が確かめて直してから保存する**
  （`handover_ai` が案を返すだけ。保存はここ）。
- **いつ・誰が・どこへ向けて作ったかを残す**（要件定義書F3）。移る先のないシートは保存しない。
- `raw_handovers` は健康・介護の項目そのものを持たず、**載せた版の番号だけ**を残す
  （要件定義書7-3-1）。版をすべて読むビューは無い（7-3-2 は `stg_health_latest` だけ）ので、
  あとで開いたシートには**いまの**健康・介護の情報が出る。保存した版と違うときは、
  そのことを画面に出す（`HEALTH_VERSION_NOTICE`）。
- 要配慮個人情報を扱う。**ログに中身を出さない**（要件定義書11-3）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from backend.config import get_settings
from backend.services import bq

# 入力の上限（表に入らない長さの案を保存しない）。
DESTINATION_MAX_LENGTH = 200
SUMMARY_MAX_LENGTH = 1200
CAUTIONS_MAX_LENGTH = 1200

# 画面に出す件数（読みを増やさないため上限を置く）。
LIST_LIMIT = 10

# Gemini に渡す直近の件数（要件定義書10-6は記録と相談を合わせて最大5件）。
RECENT_VISITS_FOR_AI = 3
RECENT_CONSULTATIONS_FOR_AI = 2

# 保存した版と、いまの健康・介護の情報の版が違うときに出す知らせ（文言の正本はここ）。
HEALTH_VERSION_NOTICE = (
    "このシートを作ったあとに、健康・介護の情報が更新されています。"
    "下に出ているのは**いまの**情報です。作ったときの内容とは違うことがあります"
)

# 画面と印刷に必ず出す断り書き（要件定義書F3「作った人が確かめて直せるようにする」）。
AI_DISCLAIMER = (
    "要約と気をつけることはAIが作った案です。記録に書かれていないことが混ざることがあるため、"
    "作った方が必ず確かめてから渡してください"
)


class HandoverValidationError(ValueError):
    """引き継ぎシートの入力が正しくない。"""


# --- 値をそろえる ---


def _clean_text(value: Any, *, label: str, max_length: int, required: bool) -> str | None:
    if value is None or not isinstance(value, str):
        if required:
            raise HandoverValidationError(f"{label}を入れてください")
        return None
    text = value.strip()
    if not text:
        if required:
            raise HandoverValidationError(f"{label}を入れてください")
        return None
    if len(text) > max_length:
        raise HandoverValidationError(f"{label}は{max_length}文字以内にしてください")
    return text


def clean_destination(value: Any) -> str:
    """移る先（必須。どこへ向けて作ったかを残すため。要件定義書F3）。"""
    text = _clean_text(
        value, label="移る先", max_length=DESTINATION_MAX_LENGTH, required=True
    )
    assert text is not None  # required=True が None を通さない
    return text


def clean_summary(value: Any) -> str:
    """直近の記録と相談の要約（必須）。"""
    text = _clean_text(value, label="要約", max_length=SUMMARY_MAX_LENGTH, required=True)
    assert text is not None
    return text


def clean_cautions(value: Any) -> str | None:
    """気をつけること（空でもよい）。"""
    return _clean_text(
        value, label="気をつけること", max_length=CAUTIONS_MAX_LENGTH, required=False
    )


def validate_health_version(value: Any) -> int | None:
    """載せた健康・介護の情報の版（まだ登録が無ければ空）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HandoverValidationError("健康・介護の情報の版が読み取れません")
    return value


# --- 書き込み ---


def new_handover_id() -> str:
    """引き継ぎシートのID（毎回ちがう値）。"""
    return f"ho_{uuid.uuid4().hex}"


def build_row(
    *,
    survivor_id: str,
    org_id: str,
    created_by: str,
    created_role: str,
    destination: Any,
    summary: Any,
    cautions: Any = None,
    health_version: Any = None,
    ai_used: bool = True,
) -> dict[str, Any]:
    """`raw_handovers` に入れる1行を作る（まだ書かない）。"""
    if not survivor_id or not org_id:
        raise HandoverValidationError("記録が決まっていません")
    return {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "handover_id": new_handover_id(),
        "destination": clean_destination(destination),
        "health_version": validate_health_version(health_version),
        "summary": clean_summary(summary),
        "cautions": clean_cautions(cautions),
        "ai_used": bool(ai_used),
    }


def save(row: dict[str, Any]) -> dict[str, Any]:
    """作った行を `raw_handovers` に追記する。"""
    bq.insert("raw_handovers", row)
    return row


# --- 読み ---


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


_COLUMNS = (
    "handover_id",
    "survivor_id",
    "destination",
    "health_version",
    "summary",
    "cautions",
    "ai_used",
    "created_at",
    "created_by",
    "created_role",
)


def list_for_survivor(
    *, survivor_id: str, org_id: str | None = None, limit: int = LIST_LIMIT
) -> list[dict[str, Any]]:
    """その人の引き継ぎシートを新しい順に読む（`stg_handovers`）。

    **被災者のIDだけで読み、組織のIDでは絞らない**（追加修正⑪）。組織で絞ると、
    組織がない本人には自分のシートが1枚も出ず、本人が組織を変えたあとは前の組織で
    作ったシートが本人にも新しい組織にも出なくなる。引き継ぎのためのシートなので、
    その人の記録に関わる人には、どの組織のときに作ったシートも見せる。

    **誰が見てよいかは、呼ぶ側が先に `shared.load_visible` で確かめる**（記録を見られる
    人と同じ。前の組織の専門職・担当を外れた一般の支援者は、そこで 404 になる）。
    `org_id` は前の呼び方との互換のために受け取るだけで、読みには使わない。
    """
    del org_id
    if not survivor_id:
        return []
    columns = ",\n       ".join(_COLUMNS)
    sql = (
        f"SELECT {columns}\n"
        f"FROM {_staging_table('stg_handovers')}\n"
        "WHERE survivor_id = @survivor_id\n"
        "ORDER BY created_at DESC\n"
        f"LIMIT {int(limit)}"
    )
    return bq.query(sql, {"survivor_id": survivor_id})


# --- 画面に返す形 ---


def payload(
    row: dict[str, Any],
    *,
    creator_names: dict[str, str] | None = None,
    health_version_now: Any = None,
) -> dict[str, Any]:
    """引き継ぎシート1件分（印刷用の画面にそのまま出せる形）。

    `health_version_now` を渡すと、保存した版と違うときに知らせを付ける。
    """
    names = creator_names or {}
    saved_version = row.get("health_version")
    changed = (
        isinstance(saved_version, int)
        and isinstance(health_version_now, int)
        and saved_version != health_version_now
    )
    return {
        "handover_id": row.get("handover_id"),
        "survivor_id": row.get("survivor_id"),
        "destination": row.get("destination"),
        "summary": row.get("summary"),
        "cautions": row.get("cautions"),
        "health_version": saved_version,
        "health_version_changed": changed,
        "health_version_notice": HEALTH_VERSION_NOTICE if changed else None,
        "ai_used": bool(row.get("ai_used")),
        "ai_disclaimer": AI_DISCLAIMER,
        "created_at": row.get("created_at"),
        "created_role": row.get("created_role"),
        "created_by_name": names.get(str(row.get("created_by") or ""), ""),
    }
