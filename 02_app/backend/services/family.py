"""家族と記録のつながり（使いやすさの改修の計画.md 10章。raw_family_links・stg_family_links）。

家族などが本人の代わりに記録を登録したり、本人の招待コードで記録につながったりする。

- 家族は**一般の支援者**として入る（画面の立場の名前も「支援者（ボランティア・ご家族）」）。
  組織に入っていなくてもつながれる。
- つながりは担当（`raw_assignment_events`）とは分ける。担当は組織の中の割り当てで、
  家族は組織がなくてもつながるため（10-3）。
- つなぐ・外すのたびに `raw_family_links` に1行足す（上書きしない。要件定義書10-4）。
  いまつながっているかは `stg_family_links`（記録と利用者の組ごとに最新の行が link のもの）で決め、
  見てよいかの判定は `int_survivor_list.family_user_ids` で行う（`survivors.can_view`）。
- 家族に返すのは**基本の情報と居場所だけ**にする。健康・介護の情報・訪問の記録・相談・支援・
  悪化の兆しは返さない（本人の同意なしに集めない情報で、家族の画面では扱わないため）。
- 家族の名前や連絡先をログに出さない。出すのは処理の名前と件数だけ。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from backend.config import get_settings
from backend.services import bq, survivors

# raw_family_links.action の値
ACTION_LINK = "link"
ACTION_UNLINK = "unlink"

# raw_family_links.via の値（どうやってつながったか）
VIA_PROXY = "proxy"  # 代わりに登録した
VIA_INVITE = "invite"  # 本人の招待コードを入れた

VIA_LABELS = {
    VIA_PROXY: "代わりに登録した方",
    VIA_INVITE: "招待コードでつながった方",
}


class FamilyError(ValueError):
    """家族のつながりの操作が正しくない。"""


def _raw_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_raw, table)


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


def _event(
    *,
    survivor_id: str,
    org_id: str,
    user_id: str,
    action: str,
    via: str | None,
    created_by: str,
    created_role: str,
) -> dict[str, Any]:
    if not survivor_id or not user_id:
        raise FamilyError("記録か利用者が決まっていません")
    row: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        # 組織がない記録もつなげる（列は NOT NULL なので空の文字にする）
        "org_id": org_id or "",
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "user_id": user_id,
        "action": action,
    }
    if via:
        row["via"] = via
    return row


# --- 書く ---


def link(
    *,
    survivor_id: str,
    org_id: str,
    user_id: str,
    via: str,
    created_by: str,
    created_role: str,
) -> dict[str, Any]:
    """家族として記録につなぐ（1行足す）。"""
    if via not in VIA_LABELS:
        raise FamilyError("つながり方が正しくありません")
    row = _event(
        survivor_id=survivor_id,
        org_id=org_id,
        user_id=user_id,
        action=ACTION_LINK,
        via=via,
        created_by=created_by,
        created_role=created_role,
    )
    bq.insert("raw_family_links", row)
    return row


def unlink(
    *,
    survivor_id: str,
    org_id: str,
    user_id: str,
    created_by: str,
    created_role: str,
) -> dict[str, Any]:
    """家族を記録から外す（「外した」の行を足す。消さない）。"""
    row = _event(
        survivor_id=survivor_id,
        org_id=org_id,
        user_id=user_id,
        action=ACTION_UNLINK,
        via=None,
        created_by=created_by,
        created_role=created_role,
    )
    bq.insert("raw_family_links", row)
    return row


# --- 読む ---


def list_links(survivor_id: str) -> list[dict[str, Any]]:
    """その記録にいまつながっている家族（つないだ日時の古い順）。"""
    if not survivor_id:
        return []
    sql = (
        "SELECT survivor_id, user_id, via, linked_at\n"
        f"FROM {_staging_table('stg_family_links')}\n"
        "WHERE survivor_id = @survivor_id\n"
        "ORDER BY linked_at, user_id"
    )
    return bq.query(sql, {"survivor_id": survivor_id})


def records_for_user(user_id: str) -> list[dict[str, Any]]:
    """その利用者が家族としてつながっている記録（登録の新しい順）。"""
    if not user_id:
        return []
    sql = (
        "SELECT *\n"
        f"FROM {_staging_table('int_survivor_list')}\n"
        "WHERE @user_id IN UNNEST(family_user_ids)\n"
        "ORDER BY registered_at DESC"
    )
    return bq.query(sql, {"user_id": user_id})


def display_names(user_ids: Sequence[str]) -> dict[str, str]:
    """家族の表示名（本人が「つながっている家族」を見るときだけ使う）。

    家族は組織に入っていないことがあるので、組織では絞らずに利用者のIDで引く。
    返すのは表示名だけ（連絡先・住所は返さない）。
    """
    ids = sorted({str(item) for item in user_ids if item})
    if not ids:
        return {}
    sql = (
        "SELECT user_id, display_name\n"
        f"FROM {_staging_table('stg_users')}\n"
        "WHERE user_id IN UNNEST(@user_ids)"
    )
    rows = bq.query(sql, {"user_ids": ids})
    return {str(row.get("user_id")): str(row.get("display_name") or "") for row in rows}


# --- 画面に返す形 ---


def record_payload(row: dict[str, Any]) -> dict[str, Any]:
    """家族の画面に返す1件分（基本の情報と居場所だけ）。

    健康・介護の情報、訪問・見守りの記録、相談、支援、悪化の兆しは入れない。
    """
    return {
        "survivor_id": row.get("survivor_id"),
        "full_name": row.get("full_name"),
        "age": row.get("age"),
        "household_size": row.get("household_size"),
        "contact": row.get("contact"),
        "address": row.get("address"),
        "municipality_code": row.get("municipality_code"),
        "stage": row.get("stage"),
        "stage_label": survivors.STAGES.get(row.get("stage") or "", ""),
        "location_type": row.get("location_type"),
        "location_type_label": survivors.LOCATION_TYPES.get(row.get("location_type") or "", ""),
        "location_detail": row.get("location_detail"),
        "registered_at": row.get("registered_at"),
        "updated_at": row.get("updated_at"),
        "registered_by_family": row.get("registered_via") == survivors.VIA_PROXY,
        "proxy_consent_label": survivors.PROXY_CONSENTS.get(row.get("proxy_consent") or "", ""),
        # 支援の組織につながっているか（組織の名前やIDは返さない。家族の画面では使わないため）
        "has_org": bool(row.get("org_id")),
        # 本人のアカウントがつながっているか（つながる前だけ、確認コードを発行できる）
        "linked_to_self": bool(row.get("self_user_id")),
        "role_view": "family",
    }


def list_item(row: dict[str, Any]) -> dict[str, Any]:
    """家族の一覧の1行（連絡先は入れない）。"""
    return {
        "survivor_id": row.get("survivor_id"),
        "full_name": row.get("full_name"),
        "age": row.get("age"),
        "stage_label": survivors.STAGES.get(row.get("stage") or "", ""),
        "location_type_label": survivors.LOCATION_TYPES.get(row.get("location_type") or "", ""),
        "location_detail": row.get("location_detail"),
        "registered_by_family": row.get("registered_via") == survivors.VIA_PROXY,
        "linked_to_self": bool(row.get("self_user_id")),
        "updated_at": row.get("updated_at"),
    }


def member_payload(link_row: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """本人に返す「つながっている家族」の1人分（表示名とつながり方だけ）。"""
    user_id = str(link_row.get("user_id") or "")
    via = str(link_row.get("via") or "")
    return {
        "user_id": user_id,
        "display_name": names.get(user_id, ""),
        "via": via,
        "via_label": VIA_LABELS.get(via, ""),
        "linked_at": link_row.get("linked_at"),
    }
