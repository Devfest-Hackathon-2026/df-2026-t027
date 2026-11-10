"""周りの地域の状況の API（使いやすさの改修の計画.md 8章。要件定義書には無い追加の機能）。

- `GET   /api/disaster-status`               自分の市区町村と、同じ県の状況（立場を選んだ人全員）
- `POST  /api/disaster-status`               状況を1件入れる（専門職だけ。組織のオーナーも専門職）
- `PATCH /api/disaster-status/{status_id}`   状況を直す・終わった印を付ける（版を足す）

守ること：

- **立場と組織は `stg_users` から読む**（`auth`）。要求に書かれた組織や立場は受け取らない。
- 読むのは、本人・一般の支援者・専門職の全員。組織に入っていなくても読める
  （公開の情報で、記録には触れないため）。
- 書けるのは専門職だけ（本人と一般の支援者は 403）。直せるのは**入れた組織の1件だけ**で、
  ほかの組織の1件は、無いときと同じ 404 にする。
- 上書きも削除もしない。直すとき・終わったときも、版を1つ足す（INSERT だけ）。
- Gemini は使わない。外のページも読まない（計画の案A）。
- ログには項目の種類と版だけを出す（災害の名前・状態の本文・URLは出さない）。

BigQuery は同期なので、読み書きはすべて `asyncio.to_thread` で別のスレッドに出す。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from backend import auth
from backend.routers import shared
from backend.services import bq, disaster_status, municipalities, users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["周りの地域の状況"])

# 読むのは、立場を選んだ人全員（組織がなくてもよい）
anyone_registered = auth.require_roles_without_org(*users.ROLES)
# 書くのは専門職だけ（組織のオーナーも専門職。組織に入っていることも求める）
only_professional = auth.require_roles(users.ROLE_PROFESSIONAL)


# --- 要求の形（組織・立場・入れた人は持たない） ---


class DisasterStatusCreateRequest(BaseModel):
    """状況を1件入れる。"""

    municipality_code: str = Field(default="", description="市区町村の団体コード（6桁）")
    disaster_name: str = Field(default="", description="災害の名前")
    item_kind: str = Field(default="", description="項目（災害救助法の適用・避難所など）")
    state: str = Field(default="", description="状態（適用中・開設中など）")
    deadline: str | None = Field(default=None, description="申請・受付の期限（YYYY-MM-DD）")
    source_url: str = Field(default="", description="出典のURL（必須）")
    note: str | None = Field(default=None, description="補足")


class DisasterStatusUpdateRequest(BaseModel):
    """状況を直す（渡さなかった項目は前の版のまま）。"""

    municipality_code: str | None = None
    disaster_name: str | None = None
    item_kind: str | None = None
    state: str | None = None
    ended: bool | None = Field(default=None, description="終わった・取り下げた")
    deadline: str | None = Field(default=None, description="空の文字で送ると期限を消す")
    source_url: str | None = None
    note: str | None = None


# --- 自分の市区町村を決める ---


def _home_municipality(user: auth.CurrentUser) -> str | None:
    """住んでいる・活動する市区町村を決める（どれも stg_ から読んだ値だけを使う）。

    - 本人：自分の記録の市区町村（無ければ、つながっている組織の市区町村）
    - 支援者：プロフィールの活動する市区町村（無ければ、組織の市区町村）
    """
    code: str | None = None
    if user.role == users.ROLE_SURVIVOR and user.linked_survivor_id:
        code = disaster_status.fetch_survivor_municipality(
            survivor_id=user.linked_survivor_id, user_id=user.uid
        )
    elif user.role in users.SUPPORTER_ROLES:
        code = user.municipality_code or None
    if not code:
        code = disaster_status.fetch_org_municipality(user.org_id)
    # 一覧に無い値（古い団体コードなど）は使わない
    if code and not municipalities.lookup(code):
        return None
    return code


# --- 読む ---


@router.get("/disaster-status")
async def list_disaster_status(
    municipality_code: str | None = Query(default=None, description="見る市区町村（省くと自分の市区町村）"),
    user: auth.CurrentUser = Depends(anyone_registered),
) -> dict[str, Any]:
    """その市区町村の状況（own）と、同じ県のほかの市区町村の状況（nearby）を返す。

    更新から30日を超えたものには `stale_notice`（古い情報かもしれません）を付ける。
    """
    if municipality_code:
        try:
            code = disaster_status.validate_municipality(municipality_code)["code"]
        except disaster_status.DisasterStatusValidationError as exc:
            raise shared.bad_request(str(exc)) from exc
    else:
        try:
            code = await asyncio.to_thread(_home_municipality, user)
        except bq.BigQueryUnavailable as exc:
            raise shared.unavailable() from exc

    base: dict[str, Any] = {
        "item_kinds": disaster_status.item_kind_choices(),
        "stale_days": disaster_status.STALE_DAYS,
        "can_edit": user.role == users.ROLE_PROFESSIONAL and user.has_org,
    }
    if not code:
        return {
            **base,
            "municipality_code": None,
            "municipality_name": "",
            "prefecture": "",
            "own": [],
            "nearby": [],
            "count": 0,
            "notice": disaster_status.NO_AREA_NOTICE,
        }

    try:
        rows = await asyncio.to_thread(disaster_status.list_for_prefecture, municipality_code=code)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    groups = disaster_status.split_by_area(rows, municipality_code=code, viewer_org_id=user.org_id)
    place = municipalities.lookup(code) or {}
    count = len(groups["own"]) + len(groups["nearby"])
    logger.info("disaster_status_listed: count=%d", count)
    return {
        **base,
        "municipality_code": code,
        "municipality_name": place.get("name", ""),
        "prefecture": place.get("prefecture", ""),
        "own": groups["own"],
        "nearby": groups["nearby"],
        "count": count,
        "notice": None,
    }


# --- 入れる ---


@router.post("/disaster-status", status_code=status.HTTP_201_CREATED)
async def create_disaster_status(
    body: DisasterStatusCreateRequest,
    user: auth.CurrentUser = Depends(only_professional),
) -> dict[str, Any]:
    """状況を1件入れる（版1として追記する）。組織は利用者の組織を使う。"""
    try:
        row = disaster_status.build_row(
            org_id=str(user.org_id),
            created_by=user.uid,
            created_role=str(user.role),
            fields=body.model_dump(),
        )
    except disaster_status.DisasterStatusValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(disaster_status.save, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("disaster_status_created: kind=%s", saved.get("item_kind"))
    return {"item": disaster_status.payload(_as_stg_row(saved), viewer_org_id=user.org_id)}


# --- 直す ---


@router.patch("/disaster-status/{status_id}")
async def update_disaster_status(
    status_id: str,
    body: DisasterStatusUpdateRequest,
    user: auth.CurrentUser = Depends(only_professional),
) -> dict[str, Any]:
    """状況を直す（**版を1つ足す**）。終わった・取り下げたときは `ended` を true にする。

    ほかの組織の1件は、存在しないときと同じ 404（組織で絞って読む）。
    """
    try:
        current = await asyncio.to_thread(
            disaster_status.fetch_status, status_id=status_id, org_id=str(user.org_id)
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if not current:
        raise shared.not_found()

    try:
        row = disaster_status.build_update_row(
            current=current,
            created_by=user.uid,
            created_role=str(user.role),
            changes=body.model_dump(),
        )
    except disaster_status.DisasterStatusValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(disaster_status.save, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info(
        "disaster_status_updated: kind=%s version=%s ended=%s",
        saved.get("item_kind"),
        saved.get("version"),
        saved.get("ended"),
    )
    return {"item": disaster_status.payload(_as_stg_row(saved), viewer_org_id=user.org_id)}


def _as_stg_row(saved: dict[str, Any]) -> dict[str, Any]:
    """保存した行を `stg_disaster_status` の1行と同じ形にする（読み直さずに返すため）。"""
    return {**saved, "updated_at": saved.get("created_at")}
