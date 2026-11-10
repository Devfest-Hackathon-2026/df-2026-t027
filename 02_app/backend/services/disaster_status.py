"""周りの地域の状況（使いやすさの改修の計画.md 8章。要件定義書には無い追加の機能）。

本人と支援者が、住んでいる・活動する市区町村と、同じ県のほかの市区町村について、
災害救助法などの適用・受付中の支援・避難所・仮設住宅の状況を見る。

- **集め方は計画の案A。** 組織のオーナーと専門職が画面から入れる。Gemini もページの読み取りも使わない。
- **出典のURLを必ず付ける。** http か https のURLだけを通す。
- **市区町村は一覧にあるものだけ。** 一覧の正本は `services/municipalities.py`
  （画面と同じ `data/municipalities.json`）。
- **上書きも削除もしない。** 直すときも、終わった・取り下げたときも、版を1つ足す
  （`stg_disaster_status` が最新の版を決める）。終わったものは読むときに外す。
- **直せるのは、入れた組織の専門職だけ。** ほかの組織の1件は、無いときと同じ 404 にする。
- 更新から30日を超えたものには「古い情報かもしれません」と出す
  （施設の空きの「24時間」の知らせと同じ考え方。日数と文言の正本はここ）。
- 入れるのは公開の情報だけで、個人の情報は入れない。それでもログには本文を出さない。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from urllib.parse import urlsplit

from backend.config import get_settings
from backend.services import bq, municipalities

# 項目（計画 8-1）。値の正本はここ。画面は API が返す一覧から選択肢を作る。
ITEM_KINDS: dict[str, str] = {
    "relief_act": "災害救助法の適用",
    "support_act": "被災者生活再建支援法の適用",
    "support_open": "受付中の支援",
    "shelter": "避難所",
    "temporary_housing": "仮設住宅",
    "other": "その他",
}

# 更新から何日で「古い」と見なすか（計画 8-3）。
STALE_DAYS = 30

# 古いものに付ける知らせ（文言の正本はここ。画面に書き写さない）。
STALE_NOTICE = "古い情報かもしれません"

# 住んでいる・活動する市区町村が分からないときの案内。
NO_AREA_NOTICE = (
    "お住まい（活動する）市区町村が分からないため、周りの地域の状況を出せません。"
    "プロフィールで市区町村を入れてください"
)

# 読む件数の上限（読みを増やさないため）。
LIST_LIMIT = 200

# 入力の上限。
DISASTER_NAME_MAX_LENGTH = 100
STATE_MAX_LENGTH = 200
NOTE_MAX_LENGTH = 500
URL_MAX_LENGTH = 500

# 期限として受け付ける範囲（打ち間違いの年を断る）。
DEADLINE_PAST_LIMIT_DAYS = 365
DEADLINE_FUTURE_LIMIT_DAYS = 365 * 3


class DisasterStatusValidationError(ValueError):
    """周りの地域の状況の入力が正しくない。"""


# --- 値をそろえる ---


def _clean_text(value: Any, *, label: str, max_length: int, required: bool) -> str | None:
    if value is None or not isinstance(value, str) or not value.strip():
        if required:
            raise DisasterStatusValidationError(f"{label}を入れてください")
        return None
    text = value.strip()
    if len(text) > max_length:
        raise DisasterStatusValidationError(f"{label}は{max_length}文字以内にしてください")
    return text


def validate_municipality(value: Any) -> dict[str, str]:
    """市区町村を確かめる（**一覧にある団体コードだけ**を通す）。"""
    code = value.strip() if isinstance(value, str) else ""
    place = municipalities.lookup(code) if code else None
    if not place:
        raise DisasterStatusValidationError("市区町村を一覧から選んでください")
    return place


def prefecture_code(municipality_code: str) -> str:
    """都道府県の番号（団体コードの上2桁）。"""
    return municipality_code[:2]


def validate_item_kind(value: Any) -> str:
    """項目を確かめる（選択肢にない値は断る）。"""
    if not isinstance(value, str) or value not in ITEM_KINDS:
        raise DisasterStatusValidationError(
            "項目は次のどれかにしてください：" + "・".join(ITEM_KINDS.values())
        )
    return value


def validate_source_url(value: Any) -> str:
    """出典のURLを確かめる（必須。http か https で、行き先のあるものだけ）。"""
    text = _clean_text(value, label="出典のURL", max_length=URL_MAX_LENGTH, required=True)
    assert text is not None
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise DisasterStatusValidationError("出典のURLは http:// か https:// で始めてください") from exc
    if parts.scheme not in ("http", "https") or not parts.netloc or any(c.isspace() for c in text):
        raise DisasterStatusValidationError("出典のURLは http:// か https:// で始めてください")
    return text


def validate_deadline(value: Any, *, today: dt.date | None = None) -> dt.date | None:
    """期限を日付にする（任意。読み取れない値と、遠すぎる値は断る）。"""
    if value is None or value == "":
        return None
    # 前の版から引き継いだ日付（BigQuery から読んだ値）は、範囲を確かめずに残す
    # （期限から1年たった1件を直すと、期限のせいで直せなくなるため）
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            parsed = dt.date.fromisoformat(value.strip())
        except ValueError as exc:
            raise DisasterStatusValidationError("期限は日付で入れてください") from exc
    else:
        raise DisasterStatusValidationError("期限は日付で入れてください")
    base = today or dt.datetime.now(dt.timezone.utc).date()
    if not (
        base - dt.timedelta(days=DEADLINE_PAST_LIMIT_DAYS)
        <= parsed
        <= base + dt.timedelta(days=DEADLINE_FUTURE_LIMIT_DAYS)
    ):
        raise DisasterStatusValidationError("期限の日付を確かめてください（年の打ち間違いなど）")
    return parsed


def _clean_flag(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise DisasterStatusValidationError("終わったかどうかは「はい」「いいえ」で入れてください")
    return value


def clean_fields(fields: dict[str, Any] | None) -> dict[str, Any]:
    """入れる項目をそろえる（版を足すたびに全部を確かめ直す）。"""
    source = fields or {}
    place = validate_municipality(source.get("municipality_code"))
    return {
        "municipality_code": place["code"],
        "prefecture_code": prefecture_code(place["code"]),
        "disaster_name": _clean_text(
            source.get("disaster_name"),
            label="災害の名前",
            max_length=DISASTER_NAME_MAX_LENGTH,
            required=True,
        ),
        "item_kind": validate_item_kind(source.get("item_kind")),
        "state": _clean_text(
            source.get("state"), label="状態", max_length=STATE_MAX_LENGTH, required=True
        ),
        "ended": _clean_flag(source.get("ended")),
        "deadline": validate_deadline(source.get("deadline")),
        "source_url": validate_source_url(source.get("source_url")),
        "note": _clean_text(
            source.get("note"), label="補足", max_length=NOTE_MAX_LENGTH, required=False
        ),
    }


# --- 書き込み ---


def new_status_id() -> str:
    """状況の1件のID（毎回ちがう値）。"""
    return f"ds_{uuid.uuid4().hex}"


def build_row(
    *,
    org_id: str,
    created_by: str,
    created_role: str,
    fields: dict[str, Any],
    version: int = 1,
    status_id: str | None = None,
) -> dict[str, Any]:
    """`raw_disaster_status` に入れる1行を作る（まだ書かない）。

    組織・入れた人・立場は、要求ではなく `stg_users` から読んだ値を呼ぶ側が渡す。
    """
    if not org_id:
        raise DisasterStatusValidationError("組織が決まっていません")
    row: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "status_id": status_id or new_status_id(),
        "version": version,
    }
    row.update(clean_fields(fields))
    return row


# 直すときに変えられる項目（組織と入れた人は変えられない）。
_EDITABLE = (
    "municipality_code",
    "disaster_name",
    "item_kind",
    "state",
    "ended",
    "deadline",
    "source_url",
    "note",
)


def build_update_row(
    *,
    current: dict[str, Any],
    created_by: str,
    created_role: str,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """前の版に変更を重ねた**新しい版**の行を作る。

    渡されなかった項目（None）は前の版のまま。期限を空の文字で送ると、期限を消す。
    組織は前の版から引き継ぐ（呼ぶ側が、同じ組織の1件だけを `current` に渡す）。
    """
    if not current:
        raise DisasterStatusValidationError("状況が見つかりません")
    version = current.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise DisasterStatusValidationError("状況の版が読み取れません")
    fields = {key: current.get(key) for key in _EDITABLE}
    for key in _EDITABLE:
        if changes.get(key) is not None:
            fields[key] = changes[key]
    return build_row(
        org_id=str(current.get("org_id") or ""),
        created_by=created_by,
        created_role=created_role,
        fields=fields,
        version=version + 1,
        status_id=str(current.get("status_id") or ""),
    )


def save(row: dict[str, Any]) -> dict[str, Any]:
    """作った行を `raw_disaster_status` に追記する（INSERT だけ）。"""
    bq.insert("raw_disaster_status", row)
    return row


# --- 読み ---


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


_COLUMNS = (
    "status_id",
    "org_id",
    "version",
    "municipality_code",
    "prefecture_code",
    "disaster_name",
    "item_kind",
    "state",
    "ended",
    "deadline",
    "source_url",
    "note",
    "updated_at",
)


def list_for_prefecture(
    *, municipality_code: str, limit: int = LIST_LIMIT
) -> list[dict[str, Any]]:
    """その市区町村と同じ県の状況を読む（終わったものは外す）。

    自分の市区町村を先に、それぞれ新しく更新した順に並べる。
    """
    if not municipality_code:
        return []
    columns = ",\n       ".join(_COLUMNS)
    sql = (
        f"SELECT {columns}\n"
        f"FROM {_staging_table('stg_disaster_status')}\n"
        "WHERE prefecture_code = @prefecture_code AND NOT ended\n"
        "ORDER BY IF(municipality_code = @municipality_code, 0, 1), updated_at DESC\n"
        f"LIMIT {int(limit)}"
    )
    return bq.query(
        sql,
        {
            "prefecture_code": prefecture_code(municipality_code),
            "municipality_code": municipality_code,
        },
    )


def fetch_status(*, status_id: str, org_id: str) -> dict[str, Any] | None:
    """直すときに1件読む（**同じ組織のものだけ**。ほかの組織のものは見つからない扱い）。"""
    if not status_id or not org_id:
        return None
    columns = ",\n       ".join(_COLUMNS)
    sql = (
        f"SELECT {columns}\n"
        f"FROM {_staging_table('stg_disaster_status')}\n"
        "WHERE status_id = @status_id AND org_id = @org_id"
    )
    return bq.query_one(sql, {"status_id": status_id, "org_id": org_id})


def fetch_survivor_municipality(*, survivor_id: str, user_id: str) -> str | None:
    """本人の記録から、住んでいる市区町村を読む（**本人の記録だけ**。self_user_id で絞る）。

    記録の全体（`int_survivor_list`）は読まない。市区町村の列だけを読む。
    """
    if not survivor_id or not user_id:
        return None
    sql = (
        "SELECT municipality_code\n"
        f"FROM {_staging_table('stg_survivors')}\n"
        "WHERE survivor_id = @survivor_id AND self_user_id = @user_id"
    )
    row = bq.query_one(sql, {"survivor_id": survivor_id, "user_id": user_id})
    code = (row or {}).get("municipality_code")
    return code if isinstance(code, str) and code else None


def fetch_org_municipality(org_id: str | None) -> str | None:
    """組織が受け持つ市区町村を読む（本人・支援者の市区町村が空のときの代わり）。"""
    if not org_id:
        return None
    sql = (
        "SELECT municipality_code\n"
        f"FROM {_staging_table('stg_organizations')}\n"
        "WHERE org_id = @org_id"
    )
    row = bq.query_one(sql, {"org_id": org_id})
    code = (row or {}).get("municipality_code")
    return code if isinstance(code, str) and code else None


# --- 画面に返す形 ---


def _as_datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None


def is_stale(updated_at: Any, *, now: dt.datetime | None = None) -> bool:
    """更新から30日を超えたか（更新の日時が読めないものも古い扱いにする）。"""
    updated = _as_datetime(updated_at)
    if updated is None:
        return True
    current = now or dt.datetime.now(dt.timezone.utc)
    return current - updated > dt.timedelta(days=STALE_DAYS)


def _iso(value: Any) -> str | None:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value) if value else None


def payload(
    row: dict[str, Any], *, viewer_org_id: str | None, now: dt.datetime | None = None
) -> dict[str, Any]:
    """1件を画面に返す形にする。

    入れた人のIDと組織のIDは返さない。直せるか（`editable`）だけを返す。
    """
    code = str(row.get("municipality_code") or "")
    place = municipalities.lookup(code) or {}
    stale = is_stale(row.get("updated_at"), now=now)
    kind = str(row.get("item_kind") or "")
    return {
        "status_id": row.get("status_id"),
        "version": row.get("version"),
        "municipality_code": code,
        "municipality_name": place.get("name", ""),
        "prefecture": place.get("prefecture", ""),
        "disaster_name": row.get("disaster_name"),
        "item_kind": kind,
        "item_kind_label": ITEM_KINDS.get(kind, kind),
        "state": row.get("state"),
        "ended": bool(row.get("ended")),
        "deadline": _iso(row.get("deadline")),
        "source_url": row.get("source_url"),
        "note": row.get("note"),
        "updated_at": _iso(row.get("updated_at")),
        "is_stale": stale,
        "stale_notice": STALE_NOTICE if stale else None,
        "editable": bool(viewer_org_id) and row.get("org_id") == viewer_org_id,
    }


def split_by_area(
    rows: list[dict[str, Any]],
    *,
    municipality_code: str,
    viewer_org_id: str | None,
    now: dt.datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """自分の市区町村（own）と、同じ県のほかの市区町村（nearby）に分ける。

    読みは県で絞っているが、念のためここでも同じ県のものだけを残す。
    """
    pref = prefecture_code(municipality_code)
    own: list[dict[str, Any]] = []
    nearby: list[dict[str, Any]] = []
    for row in rows:
        if row.get("ended"):
            continue
        code = str(row.get("municipality_code") or "")
        if prefecture_code(code) != pref:
            continue
        item = payload(row, viewer_org_id=viewer_org_id, now=now)
        (own if code == municipality_code else nearby).append(item)
    return {"own": own, "nearby": nearby}


def item_kind_choices() -> list[dict[str, str]]:
    """項目の選択肢（画面の選択欄に使う）。"""
    return [{"value": key, "label": label} for key, label in ITEM_KINDS.items()]
