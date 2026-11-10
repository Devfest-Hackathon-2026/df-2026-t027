"""本人からの相談（F8。要件定義書F8・7-3-1の raw_consultations・7-3-2、開発手順書 Step 8）。

- **本人が確かめてから送る。** Gemini で整えた案を返すだけの処理（`consultation_ai`）と、
  本人が確かめたものを書く処理（ここ）を分ける（要件定義書F8・10-5の「AI」の列）。
- 整える項目は **F2 と同じ9つ**（要件定義書F8）。書かれていない項目は「記載なし」にする。
- raw は追記だけ。**相談の状況（未対応／対応中／対応済み）も書き換えず**、
  `raw_consultation_events` に1行足す（`stg_consultations` が最新の状況を計算する。7-3-2）。
- 整えた項目は JSON の列に**辞書で入れる**（`{"items": {...}}`）。配列をそのまま渡すと、
  0件のときに要素の型が決められず JSON の列に書けない（`services/bq.py` の `_array_parameter`）。
- **急ぎの度合いと根拠は本人に返さない。** 支援者が訪ねる順番を決めるためのもので、
  本人の画面に出すと診断のように読まれる（要件定義書10-3、F4 と同じ考え方）。
- 相談の種類が読み取れなくても**保存は止めない**（「その他」にする。本人が送れなくなるため）。
- ログに相談の本文・氏名・健康の情報を出さない（要件定義書10-2の6・11-3）。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from backend.config import get_settings
from backend.services import bq, visits

# 相談の種類（要件定義書F8・7-3-1）。値の正本はここ。画面に出す名前も同じ。
CATEGORIES: dict[str, str] = {
    "health": "体調",
    "care": "介護",
    "housing": "住まい",
    "money": "お金と制度",
    "life": "生活",
    "other": "その他",
}

# 読み取れない種類に入れる値（保存を止めないため）。
DEFAULT_CATEGORY = "other"

# 日本語の名前で返ってきたときに、コードへ戻すための対応。
_LABEL_TO_CATEGORY = {label: value for value, label in CATEGORIES.items()}

# 急ぎの度合い（要件定義書F8・7-3-1）。
URGENCY_LEVELS: dict[str, str] = {
    "high": "急ぎ",
    "medium": "中くらい",
    "low": "急がない",
}

# 並べ替えの順位（`int_survivor_consultations.max_open_urgency` と同じ数え方）。
URGENCY_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}

# Gemini に急ぎの度合いを判断させられなかったときの既定（2026-11-08 の決定）。
# 全部を「高い」にすると専門職のホームが埋まり、急ぎの順そのものが意味を失う。
# 整える処理が「急を要する」と見たときだけ「高い」にする。
FALLBACK_URGENCY = "medium"
EMERGENCY_URGENCY = "high"
FALLBACK_URGENCY_EVIDENCE = "AIで急ぎの度合いを判断できませんでした"

# 相談の状況（要件定義書F8・7-3-1）。
STATUS_OPEN = "open"
STATUS_IN_PROGRESS = "in_progress"
STATUS_CLOSED = "closed"
STATUSES: dict[str, str] = {
    STATUS_OPEN: "未対応",
    STATUS_IN_PROGRESS: "対応中",
    STATUS_CLOSED: "対応済み",
}

# やり取りの種類（raw_consultation_events.action）。
ACTION_REPLIED = "replied"
ACTION_STATUS_CHANGED = "status_changed"
# 本人が同じ相談に書き足す（要望7。要件定義書には無い追加の機能）。
ACTION_MESSAGE = "message"
ACTIONS = (ACTION_REPLIED, ACTION_STATUS_CHANGED, ACTION_MESSAGE)

# スレッドに出すやり取り（返事・本人の書き足し。状況の変更だけの行は出さない）。
THREAD_ACTIONS = (ACTION_REPLIED, ACTION_MESSAGE)

# 送る前に必ず画面に出す案内（要件定義書F8）。文言の正本はここ（画面に書き写さない）。
EMERGENCY_GUIDE = (
    "命に関わるとき（息が苦しい・強い痛み・意識がはっきりしないなど）は、"
    "この相談を待たずに119番へ電話してください。"
    "警察が必要なときは110番、健康の相談は地域の相談窓口もご利用ください。"
)

# 組織につながっていない本人が送る前に、必ず画面に出す知らせ（追加修正②）。
# 文言の正本はここ（画面に書き写さない。`/api/public-config` の choices で返す）。
PUBLIC_NOTICE = (
    "この相談は、組織を問わず支援者が見られます。"
    "病名や住所など、知られたくないことは書かないでください"
)

# AIが「急を要する」と見たときに、本人の画面にすぐ出す文言（要件定義書F8）。
EMERGENCY_NOTICE = (
    "書かれた内容から、急いで連絡したほうがよい様子が読み取れました。"
    "息が苦しい・強い痛み・意識がはっきりしないなどのときは、いますぐ119番へ電話してください。"
)

# 入力の上限。
RAW_TEXT_MAX_LENGTH = 2000
REPLY_MAX_LENGTH = 1000
NEXT_ACTION_MAX_LENGTH = 300
EVIDENCE_MAX_LENGTH = 300

# 画面に出す件数（読みを増やさないため上限を置く）。
LIST_LIMIT = 20
ORG_LIST_LIMIT = 50

# F4 の判断に渡す相談の件数（要件定義書10-6は記録と相談を合わせて最大5件）。
RECENT_FOR_AI_LIMIT = 3


class ConsultationValidationError(ValueError):
    """相談の入力が正しくない。"""


# --- 値をそろえる ---


def clean_items(items: dict[str, Any] | None) -> dict[str, str]:
    """F2 と同じ9つの項目をそろえる（書かれていない項目は「記載なし」）。"""
    try:
        return visits.clean_items(items)
    except visits.VisitValidationError as exc:
        raise ConsultationValidationError(str(exc)) from exc


def is_all_empty(items: dict[str, str]) -> bool:
    return visits.is_all_empty(items)


def clean_raw_text(value: Any) -> str:
    """本人が書いた文章をそろえる（空では送れない）。"""
    if value is None or not isinstance(value, str):
        raise ConsultationValidationError("相談の内容を入れてください")
    text = value.strip()
    if not text:
        raise ConsultationValidationError("相談の内容を入れてください")
    if len(text) > RAW_TEXT_MAX_LENGTH:
        raise ConsultationValidationError(f"相談の内容は{RAW_TEXT_MAX_LENGTH}文字以内にしてください")
    return text


def normalize_category(value: Any) -> str:
    """相談の種類をそろえる（読み取れない値は「その他」にする）。"""
    if not isinstance(value, str):
        return DEFAULT_CATEGORY
    text = value.strip()
    if text in CATEGORIES:
        return text
    return _LABEL_TO_CATEGORY.get(text, DEFAULT_CATEGORY)


def validate_urgency(value: Any) -> str:
    """急ぎの度合いを確かめる（高・中・低のどれでもなければ断る）。"""
    if not isinstance(value, str) or value not in URGENCY_LEVELS:
        raise ConsultationValidationError("急ぎの度合いを読み取れませんでした")
    return value


def fallback_urgency(*, emergency_flag: bool) -> str:
    """AIで判断できなかったときの急ぎの度合い（2026-11-08 の決定）。"""
    return EMERGENCY_URGENCY if emergency_flag else FALLBACK_URGENCY


def clean_evidence(value: Any) -> str | None:
    """急ぎの度合いの根拠（相談の記述から引いた一文）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:EVIDENCE_MAX_LENGTH]


def clean_reply_text(value: Any) -> str:
    """専門職の返事をそろえる（空では送れない）。"""
    if not isinstance(value, str) or not value.strip():
        raise ConsultationValidationError("返事を入れてください")
    text = value.strip()
    if len(text) > REPLY_MAX_LENGTH:
        raise ConsultationValidationError(f"返事は{REPLY_MAX_LENGTH}文字以内にしてください")
    return text


def clean_message_text(value: Any) -> str:
    """本人が同じ相談に書き足す内容をそろえる（空では送れない。要望7）。"""
    if not isinstance(value, str) or not value.strip():
        raise ConsultationValidationError("内容を入れてください")
    text = value.strip()
    if len(text) > REPLY_MAX_LENGTH:
        raise ConsultationValidationError(f"内容は{REPLY_MAX_LENGTH}文字以内にしてください")
    return text


def clean_next_action(value: Any) -> str | None:
    """次の対応（任意）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:NEXT_ACTION_MAX_LENGTH]


def validate_status(value: Any) -> str:
    """相談の状況を確かめる（未対応・対応中・対応済みのどれか）。"""
    if not isinstance(value, str) or value not in STATUSES:
        raise ConsultationValidationError("相談の状況を読み取れませんでした")
    return value


# --- 書き込み ---


def new_consultation_id() -> str:
    """相談のID（毎回ちがう値）。"""
    return f"cs_{uuid.uuid4().hex}"


def structured_column(items: dict[str, str]) -> dict[str, Any]:
    """JSON の列に入れる形（**辞書**にする。空の配列を渡せないため）。"""
    return {"items": dict(items)}


def items_from_column(value: Any) -> dict[str, str]:
    """JSON の列から9つの項目を読み戻す（文字列で返ってくることがある）。"""
    source: Any = value
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except (TypeError, ValueError):
            source = None
    if isinstance(source, dict):
        inner = source.get("items")
        if isinstance(inner, dict):
            return clean_items(inner)
    return clean_items({})


def build_row(
    *,
    survivor_id: str,
    org_id: str,
    created_by: str,
    created_role: str,
    raw_text: str,
    category: Any,
    items: dict[str, str],
    emergency_flag: bool,
    urgency: str,
    urgency_evidence: Any = None,
    ai_used: bool = True,
    consultation_id: str | None = None,
) -> dict[str, Any]:
    """`raw_consultations` に入れる1行を作る（まだ書かない）。

    組織につながっていない本人の相談は、組織のIDを空にして残す（追加修正②。
    空の行が「公開の相談」になるかは `is_public` が、本人の**いまの**組織と合わせて決める）。
    """
    if not survivor_id:
        raise ConsultationValidationError("記録が決まっていません")
    # 本人の文章が正本（画面の改修の計画.md 6章）。文章があれば、AIで整えられず
    # 9つの項目がすべて「記載なし」でも保存する。文章も項目も無いときだけ断る
    if not (raw_text or "").strip() and is_all_empty(items):
        raise ConsultationValidationError("相談の内容を入れてください")
    return {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id or "",
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "consultation_id": consultation_id or new_consultation_id(),
        "raw_text": raw_text,
        "category": normalize_category(category),
        "structured": structured_column(items),
        "emergency_flag": bool(emergency_flag),
        "urgency": validate_urgency(urgency),
        "urgency_evidence": clean_evidence(urgency_evidence),
        "ai_used": bool(ai_used),
    }


def save(row: dict[str, Any]) -> dict[str, Any]:
    """作った行を `raw_consultations` に追記する。"""
    bq.insert("raw_consultations", row)
    return row


def build_event_row(
    *,
    survivor_id: str,
    org_id: str,
    consultation_id: str,
    created_by: str,
    created_role: str,
    action: str,
    reply_text: Any = None,
    next_action: Any = None,
    status: Any = None,
) -> dict[str, Any]:
    """`raw_consultation_events` に入れる1行を作る（返事・状況の変更・本人の書き足し）。

    公開の相談（追加修正②）へのやり取りは、組織のIDを空にして残す。
    """
    if not survivor_id or not consultation_id:
        raise ConsultationValidationError("相談が決まっていません")
    if action not in ACTIONS:
        raise ConsultationValidationError("相談への操作を読み取れませんでした")

    reply: str | None = None
    cleaned_next_action: str | None = None
    if action == ACTION_REPLIED:
        reply = clean_reply_text(reply_text)
        cleaned_next_action = clean_next_action(next_action)
        # 返事を書いたときに状況を変えないこともある（そのときは前の状況が続く）
        cleaned_status = validate_status(status) if status is not None else None
    elif action == ACTION_MESSAGE:
        # 本人は状況を変えられない（専門職だけ。要件定義書F8・10-5）
        reply = clean_message_text(reply_text)
        cleaned_status = None
    else:
        cleaned_status = validate_status(status)

    return {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id or "",
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "consultation_id": consultation_id,
        "action": action,
        "reply_text": reply,
        "next_action": cleaned_next_action,
        "status": cleaned_status,
    }


def save_event(row: dict[str, Any]) -> dict[str, Any]:
    """返事・状況の変更を `raw_consultation_events` に追記する。"""
    bq.insert("raw_consultation_events", row)
    return row


# --- 読み ---


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


def _raw_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_raw, table)


_COLUMNS = (
    "consultation_id",
    "survivor_id",
    "raw_text",
    "category",
    "structured",
    "emergency_flag",
    "urgency",
    "urgency_evidence",
    "ai_used",
    "consulted_at",
    "consulted_by",
    "consulted_role",
    "status",
    "first_replied_at",
    "reply_count",
    "first_reply_minutes",
    "last_reply_text",
    "last_next_action",
    "last_replied_at",
    "is_unanswered",
)


def _urgency_rank_sql(alias: str) -> str:
    """急ぎの度合いを並べ替えられる数にする（`int_survivor_consultations` と同じ数え方）。"""
    cases = " ".join(
        f'WHEN "{value}" THEN {rank}' for value, rank in URGENCY_RANK.items()
    )
    return f"CASE {alias}.urgency {cases} ELSE 0 END AS urgency_rank"


# --- 誰の相談として見せるか（追加修正②⑦） ---
#
# 相談の行（`raw_consultations.org_id`）は、送った時点の組織を残すだけにする。
# **誰に見せるかは、本人の記録のいまの組織（`stg_survivors.org_id`）で決める。**
# raw は追記だけで UPDATE も DELETE もしないため、本人が組織につないだり変えたりしても
# 相談の行は書き換えない。代わりに読むときに次のように決める。
#
# - 本人の記録に組織がある → その組織の専門職の相談（送った時点の組織は問わない）。
#   組織がないときに送った相談も、本人が組織につないだ時点でその組織の相談になり、
#   公開の一覧から消える。
# - 本人の記録に組織がなく、相談の行にも組織がない → **公開の相談**。組織を問わず、
#   ログインした支援者全員が一覧で見られる（氏名は出さない）。
# - 本人の記録に組織がなく、相談の行には組織がある（本人が組織を外した）→ 本人だけが見る。
#   その組織に宛てて送った相談を、外したあとで公開にはしない。


def is_public(*, consultation_org_id: Any, survivor_org_id: Any) -> bool:
    """公開の相談か（組織がないときに送り、本人がいまも組織につながっていない）。"""
    return not consultation_org_id and not survivor_org_id


def list_for_survivor(
    *, survivor_id: str, org_id: str | None = None, limit: int = LIST_LIMIT
) -> list[dict[str, Any]]:
    """その人の相談を新しい順に読む（本人の画面と詳細の画面で使う）。

    **被災者のIDだけで読む**（追加修正②⑦。組織で絞らない理由は上の説明）。
    見てよいかは、呼ぶ側が `shared.load_visible` で先に確かめる。
    `org_id` は前の呼び方との互換のために受け取るだけで、読みには使わない。
    公開かどうかを決めるため、送った時点の組織（`org_id`）も返す。
    """
    del org_id
    if not survivor_id:
        return []
    columns = ",\n       ".join(f"c.{name}" for name in _COLUMNS)
    sql = (
        f"SELECT {columns},\n"
        "       c.org_id\n"
        f"FROM {_staging_table('stg_consultations')} AS c\n"
        "WHERE c.survivor_id = @survivor_id\n"
        "ORDER BY consulted_at DESC\n"
        f"LIMIT {int(limit)}"
    )
    return bq.query(sql, {"survivor_id": survivor_id})


def list_for_org(*, org_id: str, limit: int = ORG_LIST_LIMIT) -> list[dict[str, Any]]:
    """組織の相談を、**未対応から・急ぎの順**に読む（要件定義書F8・10-5）。

    氏名は `stg_survivors` とつないで取る（専門職の画面に誰の相談かを出すため。
    **読みは1回のまま**にする）。同じ急ぎの度合いなら、**待っている時間が長いもの**を先に出す。

    絞るのは**本人の記録のいまの組織**（追加修正②⑦）。組織がないときに送った相談も、
    本人がこの組織につないだあとはここに出る。前の組織の相談は、本人が組織を変えたら出ない。
    """
    if not org_id:
        return []
    columns = ",\n       ".join(f"c.{name}" for name in _COLUMNS)
    sql = (
        f"SELECT {columns},\n"
        "       s.full_name,\n"
        "       s.age,\n"
        "       s.location_type,\n"
        f"       {_urgency_rank_sql('c')}\n"
        f"FROM {_staging_table('stg_consultations')} AS c\n"
        f"JOIN {_staging_table('stg_survivors')} AS s\n"
        "  ON s.survivor_id = c.survivor_id\n"
        "WHERE s.org_id = @org_id\n"
        "ORDER BY c.is_unanswered DESC, urgency_rank DESC, c.consulted_at ASC\n"
        f"LIMIT {int(limit)}"
    )
    return bq.query(sql, {"org_id": org_id})


def fetch_consultation(
    *, consultation_id: str, survivor_id: str, org_id: str | None = None
) -> dict[str, Any] | None:
    """相談を1件読む（返事・状況の変更のときに、実在するか確かめる）。

    その人の相談であることだけを確かめる（被災者のIDで読む。追加修正②⑦）。
    記録を見てよいかは、呼ぶ側が `shared.load_visible` で先に確かめる。
    `org_id` は前の呼び方との互換のために受け取るだけで、読みには使わない。
    """
    del org_id
    if not consultation_id or not survivor_id:
        return None
    columns = ",\n       ".join(f"c.{name}" for name in _COLUMNS)
    sql = (
        f"SELECT {columns},\n"
        "       c.org_id\n"
        f"FROM {_staging_table('stg_consultations')} AS c\n"
        "WHERE c.consultation_id = @consultation_id\n"
        "  AND c.survivor_id = @survivor_id"
    )
    return bq.query_one(
        sql,
        {"consultation_id": consultation_id, "survivor_id": survivor_id},
    )


def list_events(
    *, consultation_id: str, survivor_id: str, org_id: str | None = None
) -> list[dict[str, Any]]:
    """相談のやり取り（返事・本人の書き足し）を古い順に読む（要望7）。

    `raw_consultation_events` を直接読む。`stg_consultations` は最新の返事だけを
    持つ形（要件定義書7-3-2・テストで数が固定）で、全部のやり取りを並べる形には
    変えられない（要件定義書には無い追加の機能のため。開発ログの要確認に記録する）。
    状況の変更だけの行（`status_changed`）はスレッドに出さない。

    やり取りは、公開だったとき・組織を変える前のものも含めて、相談のIDで読む
    （追加修正②⑦）。`org_id` は互換のために受け取るだけで、読みには使わない。
    """
    del org_id
    if not consultation_id or not survivor_id:
        return []
    sql = (
        "SELECT event_id, created_at, created_by, created_role, action, reply_text, next_action\n"
        f"FROM {_raw_table('raw_consultation_events')}\n"
        "WHERE consultation_id = @consultation_id AND survivor_id = @survivor_id\n"
        "  AND action IN UNNEST(@actions)\n"
        "ORDER BY created_at ASC, event_id ASC"
    )
    return bq.query(
        sql,
        {
            "consultation_id": consultation_id,
            "survivor_id": survivor_id,
            "actions": list(THREAD_ACTIONS),
        },
    )


# --- 公開の相談（追加修正②） ---

# 公開の一覧の条件。相談の行にも、本人の記録のいまの版にも組織がないもの。
_PUBLIC_WHERE = "IFNULL(c.org_id, '') = '' AND IFNULL(s.org_id, '') = ''"


def list_public(*, limit: int = ORG_LIST_LIMIT) -> list[dict[str, Any]]:
    """公開の相談を、未対応から・急ぎの順に読む（ログインした支援者全員が見る一覧）。

    **氏名・年齢は読まない。** 出すのは住んでいる市区町村まで（追加修正②）。
    """
    columns = ",\n       ".join(f"c.{name}" for name in _COLUMNS)
    sql = (
        f"SELECT {columns},\n"
        "       s.municipality_code,\n"
        f"       {_urgency_rank_sql('c')}\n"
        f"FROM {_staging_table('stg_consultations')} AS c\n"
        f"JOIN {_staging_table('stg_survivors')} AS s\n"
        "  ON s.survivor_id = c.survivor_id\n"
        f"WHERE {_PUBLIC_WHERE}\n"
        "ORDER BY c.is_unanswered DESC, urgency_rank DESC, c.consulted_at ASC\n"
        f"LIMIT {int(limit)}"
    )
    return bq.query(sql)


def fetch_public(*, consultation_id: str) -> dict[str, Any] | None:
    """公開の相談を1件読む（公開でなければ None。返事を書く前に確かめる）。"""
    if not consultation_id:
        return None
    columns = ",\n       ".join(f"c.{name}" for name in _COLUMNS)
    sql = (
        f"SELECT {columns},\n"
        "       s.municipality_code\n"
        f"FROM {_staging_table('stg_consultations')} AS c\n"
        f"JOIN {_staging_table('stg_survivors')} AS s\n"
        "  ON s.survivor_id = c.survivor_id\n"
        "WHERE c.consultation_id = @consultation_id\n"
        f"  AND {_PUBLIC_WHERE}"
    )
    return bq.query_one(sql, {"consultation_id": consultation_id})


def public_payload(row: dict[str, Any]) -> dict[str, Any]:
    """公開の相談1件分（支援者全員に見せる形）。

    **氏名・年齢・被災者のIDは入れない。** 誰の相談かは分からないようにし、出すのは
    住んでいる市区町村の団体コードまで（画面が名前に直す）。返事・やり取りは
    相談のIDだけで行う（`/api/consultations/public/{id}/...`）。
    """
    item = payload(row, role="professional")
    for key in ("survivor_id", "full_name", "age"):
        item.pop(key, None)
    item["municipality_code"] = row.get("municipality_code")
    item["is_public"] = True
    return item


def event_payload(row: dict[str, Any]) -> dict[str, Any]:
    """スレッドの1件分（返事・書き足し）。立場による出し分けは無い（urgencyなどを含まない）。"""
    role = row.get("created_role")
    return {
        "event_id": row.get("event_id"),
        "action": row.get("action"),
        "created_at": row.get("created_at"),
        "created_role": role,
        "from_survivor": role == "survivor",
        "text": row.get("reply_text"),
        "next_action": row.get("next_action"),
    }


# --- 画面に返す形 ---


def payload(row: dict[str, Any], *, role: str, public: bool | None = None) -> dict[str, Any]:
    """相談1件分。**本人には急ぎの度合いと根拠を入れない**（要件定義書10-3）。

    `public` を渡すと、公開の相談か（追加修正②）を `is_public` に入れる。
    """
    category = normalize_category(row.get("category"))
    status = row.get("status") if row.get("status") in STATUSES else STATUS_OPEN
    item: dict[str, Any] = {
        "consultation_id": row.get("consultation_id"),
        "survivor_id": row.get("survivor_id"),
        "category": category,
        "category_label": CATEGORIES[category],
        "items": items_from_column(row.get("structured")),
        "raw_text": row.get("raw_text"),
        "emergency_flag": bool(row.get("emergency_flag")),
        "status": status,
        "status_label": STATUSES[str(status)],
        "consulted_at": row.get("consulted_at"),
        "reply_count": int(row.get("reply_count") or 0),
        "last_reply_text": row.get("last_reply_text"),
        "last_next_action": row.get("last_next_action"),
        "last_replied_at": row.get("last_replied_at"),
        "is_unanswered": bool(row.get("is_unanswered")),
        "ai_used": bool(row.get("ai_used")),
    }
    if public is not None:
        item["is_public"] = bool(public)
    if role == "survivor":
        return item

    urgency = row.get("urgency")
    item["urgency"] = urgency
    item["urgency_label"] = URGENCY_LEVELS.get(str(urgency), "")
    item["urgency_evidence"] = row.get("urgency_evidence")
    # 一覧（組織）のときだけ入っている列。詳細では None になる
    if row.get("full_name") is not None:
        item["full_name"] = row.get("full_name")
        item["age"] = row.get("age")
    return item


def body_for_ai(row: dict[str, Any]) -> str:
    """整えた項目を1行の文章にする（「記載なし」の項目は入れない）。"""
    items = items_from_column(row.get("structured"))
    parts = [
        f"{label}：{items[name]}"
        for name, label in visits.VISIT_FIELDS.items()
        if items.get(name) and items[name] != visits.NOT_RECORDED
    ]
    if parts:
        return "／".join(parts)
    return str(row.get("raw_text") or visits.NOT_RECORDED)


def recent_for_ai(
    rows: list[dict[str, Any]], *, limit: int = RECENT_FOR_AI_LIMIT
) -> list[dict[str, Any]]:
    """F4 の判断に渡す形にする（項目の名前は `services/risk_ai.py` が読むものにそろえる）。"""
    recent: list[dict[str, Any]] = []
    for row in rows[:limit]:
        category = normalize_category(row.get("category"))
        recent.append(
            {
                "consultation_id": row.get("consultation_id"),
                "consulted_at": row.get("consulted_at"),
                "category": CATEGORIES[category],
                "urgency": row.get("urgency"),
                "body": body_for_ai(row),
            }
        )
    return recent
