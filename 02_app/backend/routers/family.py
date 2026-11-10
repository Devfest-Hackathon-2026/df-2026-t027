"""家族などによる代わりの登録とつなぎの API（使いやすさの改修の計画.md 10章）。

家族（一般の支援者。組織がなくてもよい）が使う API：

- `POST  /api/family/survivors`                      本人の代わりに記録を登録する
- `GET   /api/family/survivors`                      家族としてつながっている記録の一覧
- `GET   /api/family/survivors/{id}`                 記録を見る（基本の情報と居場所だけ）
- `PATCH /api/family/survivors/{id}/basic`           氏名・年齢・世帯の人数・連絡先・住所を直す
- `POST  /api/family/survivors/{id}/location`        居場所・段階を変える
- `POST  /api/family/survivors/{id}/link-code`       本人とつなぐ確認コードを発行する
- `POST  /api/family/join`                           本人の招待コードを入れて家族としてつながる

本人が使う API：

- `POST  /api/family/invites`                        家族の招待コードを発行する
- `GET   /api/family/members`                        つながっている家族の一覧
- `POST  /api/family/members/{user_id}/remove`       家族を外す（外された人には 404 になる）

守ること（要件定義書10-3）：

- 立場と組織は `stg_users` から決める（`auth.require_roles_without_org`）。本人の記録のIDも
  `stg_users.linked_survivor_id` を使い、要求に書かれた値は使わない。
- 家族として見てよいかは `shared.load_visible(..., as_family=True)` が `int_survivor_list` の
  `family_user_ids` で決める。**つながっていない記録は、無いときと同じ 404**。
- 家族として見られても、専門職や担当の支援者の権限は付かない。ほかの API（健康・訪問・相談・
  支援など）は今までどおりの判定なので、家族というだけでは 404 のまま。
- 氏名・連絡先・コードの値・本人の同意の内容をログに出さない。
- BigQuery の読み書きは `asyncio.to_thread` で別のスレッドに出す（`routers/survivors.py` と同じ）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend import auth
from backend.routers import shared
from backend.services import bq, family, link_codes, organizations, survivors, users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/family", tags=["家族"])

# 家族として使えるのは一般の支援者だけ（組織がなくてもよい）
_family_user = auth.require_roles_without_org(users.ROLE_VOLUNTEER)
# 家族を招く・外すのは本人だけ（組織がなくてもよい）
_survivor_user = auth.require_roles_without_org(users.ROLE_SURVIVOR)


# --- 要求の形 ---


class ProxyRegistrationRequest(BaseModel):
    """家族などが本人の代わりに登録する（本人の登録と同じ欄に、本人の同意を足す）。"""

    municipality_code: str = Field(default="", description="住んでいる市区町村の団体コード（6桁）")
    org_id: str = Field(default="", description="選んだ支援の組織のID（空なら組織なし）")
    full_name: str = Field(default="", description="氏名")
    age: int | None = Field(default=None, description="年齢（後で入れてよい）")
    household_size: int | None = Field(default=None, description="世帯の人数（後で入れてよい）")
    contact: str | None = Field(default=None, description="連絡先")
    address: str | None = Field(default=None, description="住所")
    stage: str | None = Field(default=None, description="今の段階")
    location_type: str | None = Field(default=None, description="今の居場所の種類")
    location_detail: str | None = Field(default=None, description="具体的な場所")
    # obtained（同意を得た）・unable（同意できない状態）。必須（使いやすさの改修の計画.md 10-3）
    proxy_consent: str = Field(default="", description="ご本人の同意")


class FamilyBasicRequest(BaseModel):
    """家族が、氏名・年齢・世帯の人数・連絡先・住所を直す。"""

    full_name: str = Field(default="", description="氏名")
    age: int | None = Field(default=None, description="年齢")
    household_size: int | None = Field(default=None, description="世帯の人数")
    contact: str | None = Field(default=None, description="連絡先")
    address: str | None = Field(default=None, description="住所")


class FamilyLocationRequest(BaseModel):
    """家族が、居場所と段階を変える。"""

    stage: str | None = Field(default=None, description="今の段階")
    location_type: str | None = Field(default=None, description="今の居場所の種類")
    location_detail: str | None = Field(default=None, description="具体的な場所")


class FamilyJoinRequest(BaseModel):
    """本人から受け取った招待コードを入れる。"""

    code: str = Field(default="", description="家族の招待コード")


# --- 共通 ---


def _too_many(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))


def _chosen_organization(chosen_org_id: str) -> dict[str, Any] | None:
    """選んだ支援の組織を確かめる（空なら None。無ければ 400）。

    `routers/account.py` の本人の登録と同じ考え方（要求に書かれた値をそのまま信じない）。
    """
    chosen_id = (chosen_org_id or "").strip()
    if not chosen_id:
        return None
    try:
        organization = organizations.fetch_organization(chosen_id)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if organization is None:
        raise shared.bad_request("選んだ支援の組織が見つかりません。一覧から選び直してください")
    return {**organization, "org_id": chosen_id}


async def _own_record(user: auth.CurrentUser) -> dict[str, Any]:
    """本人の記録を読む（記録のIDは stg_users から。記録の本人が自分であることも確かめる）。"""
    if not user.linked_survivor_id:
        raise shared.bad_request("ご自分の記録が見つかりません。ログアウトしてお試しください")
    try:
        row = await asyncio.to_thread(survivors.fetch_survivor, user.linked_survivor_id)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if row is None or row.get("self_user_id") != user.uid:
        raise shared.bad_request("ご自分の記録が見つかりません。ログアウトしてお試しください")
    return row


# --- 家族が使う ---


@router.post("/survivors", status_code=status.HTTP_201_CREATED)
async def register_by_proxy(
    body: ProxyRegistrationRequest,
    user: auth.CurrentUser = Depends(_family_user),
) -> dict[str, Any]:
    """本人の代わりに記録を登録する。登録した人は、その記録の家族としてつながる。

    - 住んでいる市区町村とお名前は必須。年齢・世帯の人数は本人の登録と同じく後で入れてよい。
    - 本人の同意（同意を得た／同意できない状態）は必須で、記録に残す（10-3）。
    - 支援の組織は任意。選ぶと、その組織の専門職に見える（画面が先に確かめの枠を出す）。
    - 書く順番は「家族のつながり → 記録」。つながりだけが残っても指す記録が無いだけで害がない。
      逆にすると、記録はあるのに誰も家族として見られない記録が残る。
    """
    try:
        organizations.validate_municipality_code(body.municipality_code)
    except organizations.OrganizationValidationError as exc:
        raise shared.bad_request(str(exc)) from exc
    organization = await asyncio.to_thread(_chosen_organization, body.org_id)
    org_id = str(organization.get("org_id")) if organization else ""

    survivor_id = survivors.new_survivor_id()
    try:
        row = survivors.build_survivor_row(
            survivor_id=survivor_id,
            org_id=org_id,
            created_by=user.uid,
            created_role=str(user.role),
            full_name=body.full_name,
            age=body.age,
            household_size=body.household_size,
            contact=body.contact,
            stage=body.stage,
            location_type=body.location_type,
            location_detail=body.location_detail,
            self_user_id=None,
            registered_via=survivors.VIA_PROXY,
            municipality_code=body.municipality_code,
            address=body.address,
            proxy_consent=body.proxy_consent,
        )
    except survivors.SurvivorValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        await asyncio.to_thread(
            family.link,
            survivor_id=survivor_id,
            org_id=org_id,
            user_id=user.uid,
            via=family.VIA_PROXY,
            created_by=user.uid,
            created_role=str(user.role),
        )
        record = await asyncio.to_thread(survivors.insert_survivor, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("survivor_created: via=%s has_org=%s", survivors.VIA_PROXY, bool(org_id))
    return {"survivor": family.record_payload({**record, "registered_at": record["created_at"]})}


@router.get("/survivors")
async def list_family_records(user: auth.CurrentUser = Depends(_family_user)) -> dict[str, Any]:
    """家族としてつながっている記録の一覧（代わりに登録したもの・招待でつながったもの）。"""
    try:
        rows = await asyncio.to_thread(family.records_for_user, user.uid)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    return {"survivors": [family.list_item(row) for row in rows], "count": len(rows)}


@router.get("/survivors/{survivor_id}")
async def get_family_record(
    survivor_id: str, user: auth.CurrentUser = Depends(_family_user)
) -> dict[str, Any]:
    """家族として記録を見る（基本の情報・居場所・居場所の履歴だけ）。つながっていなければ 404。"""
    row = await shared.load_visible(survivor_id, user, as_family=True)
    try:
        history = await asyncio.to_thread(
            survivors.location_history, survivor_id=survivor_id
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    return {"survivor": family.record_payload(row), "location_history": history}


@router.patch("/survivors/{survivor_id}/basic")
async def change_family_basic(
    survivor_id: str,
    body: FamilyBasicRequest,
    user: auth.CurrentUser = Depends(_family_user),
) -> dict[str, Any]:
    """氏名・年齢・世帯の人数・連絡先・住所を直す（版を1つ足す）。

    年齢・世帯の人数は空にしてよい（代わりの登録で後で入れてよいことにしたため）。
    健康・介護の情報はここでは扱わない。
    """
    row = await shared.load_visible(survivor_id, user, as_family=True)
    try:
        record = await asyncio.to_thread(
            survivors.change_basic,
            current=row,
            full_name=body.full_name,
            age=body.age,
            household_size=body.household_size,
            contact=body.contact,
            created_by=user.uid,
            created_role=str(user.role),
            address=body.address,
            optional_counts=True,
        )
    except survivors.SurvivorValidationError as exc:
        raise shared.bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("survivor_basic_changed: role=%s family=True", user.role)
    return {"survivor": family.record_payload({**row, **record})}


@router.post("/survivors/{survivor_id}/location")
async def change_family_location(
    survivor_id: str,
    body: FamilyLocationRequest,
    user: auth.CurrentUser = Depends(_family_user),
) -> dict[str, Any]:
    """居場所と段階を変える（版を1つ足し、履歴に残す）。"""
    row = await shared.load_visible(survivor_id, user, as_family=True)
    try:
        record = await asyncio.to_thread(
            survivors.change_location,
            current=row,
            stage=body.stage,
            location_type=body.location_type,
            location_detail=body.location_detail,
            created_by=user.uid,
            created_role=str(user.role),
        )
    except survivors.SurvivorValidationError as exc:
        raise shared.bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("survivor_location_changed: role=%s family=True", user.role)
    return {"survivor": family.record_payload({**row, **record})}


@router.post("/survivors/{survivor_id}/link-code", status_code=status.HTTP_201_CREATED)
async def issue_link_code(
    survivor_id: str, user: auth.CurrentUser = Depends(_family_user)
) -> dict[str, Any]:
    """本人とつなぐ確認コードを発行する（本人のアカウントがまだつながっていない記録だけ）。

    本人は、登録の画面の「確認コードでつなぐ」にこのコードを入れる（`routers/account.py` の
    `_link_with_code`。専門職が発行するコードと同じ仕組み）。ハッシュだけを残し、1回使ったら
    無効になり、7日で切れる。何度も間違えると1時間入れられない（`services/link_codes.py`）。
    """
    row = await shared.load_visible(survivor_id, user, as_family=True)
    if row.get("self_user_id"):
        raise shared.bad_request("この記録には、すでにご本人のアカウントがつながっています")
    try:
        issued = await asyncio.to_thread(
            link_codes.issue_code,
            survivor_id=survivor_id,
            org_id=str(row.get("org_id") or ""),
            created_by=user.uid,
            created_role=str(user.role),
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    # 紙やメッセージで本人に渡す値。**この応答でしか返さない**（保存しているのはハッシュだけ）
    return {"link_code": {"code": issued.code, "expires_at": issued.expires_at}}


@router.post("/join")
async def join_as_family(
    body: FamilyJoinRequest, user: auth.CurrentUser = Depends(_family_user)
) -> dict[str, Any]:
    """本人から受け取った招待コードを入れて、その記録の家族としてつながる。

    書く順番は「つながり → コードを使用済みにする」。使用済みにするのを最後にすると、
    途中で落ちてもコードがまだ使えるのでやり直せる（`_link_with_code` と同じ考え方）。
    """
    try:
        resolved = await asyncio.to_thread(
            link_codes.resolve_family_code,
            body.code,
            user_id=user.uid,
            user_role=str(user.role),
        )
    except link_codes.TooManyAttempts as exc:
        raise _too_many(exc) from exc
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if resolved is None:
        # どの記録のコードだったかは返さない（当てられる手がかりを増やさない）
        raise shared.bad_request("招待コードが違います。ご本人から受け取ったコードを確かめてください")

    try:
        row = await asyncio.to_thread(survivors.fetch_survivor, resolved.survivor_id)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if row is None:
        raise shared.bad_request("このコードの記録が見つかりません。ご本人に発行し直してもらってください")
    if survivors.is_family(row, user.uid):
        raise shared.bad_request("この記録には、すでに家族としてつながっています")

    try:
        await asyncio.to_thread(
            family.link,
            survivor_id=resolved.survivor_id,
            org_id=str(row.get("org_id") or ""),
            user_id=user.uid,
            via=family.VIA_INVITE,
            created_by=user.uid,
            created_role=str(user.role),
        )
        await asyncio.to_thread(
            link_codes.mark_family_used, resolved, used_by=user.uid, used_role=str(user.role)
        )
    except family.FamilyError as exc:
        raise shared.bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("family_linked: via=%s", family.VIA_INVITE)
    return {"survivor": family.list_item(row)}


# --- 本人が使う ---


@router.post("/invites", status_code=status.HTTP_201_CREATED)
async def issue_family_invite(user: auth.CurrentUser = Depends(_survivor_user)) -> dict[str, Any]:
    """家族の招待コードを発行する（本人だけ。自分の記録にだけ発行できる）。"""
    row = await _own_record(user)
    try:
        issued = await asyncio.to_thread(
            link_codes.issue_family_code,
            survivor_id=str(row.get("survivor_id")),
            org_id=str(row.get("org_id") or ""),
            created_by=user.uid,
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    return {"invite_code": {"code": issued.code, "expires_at": issued.expires_at}}


@router.get("/members")
async def list_family_members(user: auth.CurrentUser = Depends(_survivor_user)) -> dict[str, Any]:
    """自分の記録につながっている家族の一覧（表示名とつながり方だけ）。"""
    if not user.linked_survivor_id:
        raise shared.bad_request("ご自分の記録が見つかりません。ログアウトしてお試しください")
    try:
        links = await asyncio.to_thread(family.list_links, user.linked_survivor_id)
        names = await asyncio.to_thread(
            family.display_names, [row.get("user_id") for row in links]
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    return {"members": [family.member_payload(row, names) for row in links]}


@router.post("/members/{user_id}/remove")
async def remove_family_member(
    user_id: str, user: auth.CurrentUser = Depends(_survivor_user)
) -> dict[str, Any]:
    """家族を外す（本人はいつでも外せる）。外された人は、その記録を見られなくなる（404）。"""
    row = await _own_record(user)
    survivor_id = str(row.get("survivor_id"))
    try:
        links = await asyncio.to_thread(family.list_links, survivor_id)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if not any(str(item.get("user_id")) == user_id for item in links):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="その家族は見つかりませんでした"
        )
    try:
        await asyncio.to_thread(
            family.unlink,
            survivor_id=survivor_id,
            org_id=str(row.get("org_id") or ""),
            user_id=user_id,
            created_by=user.uid,
            created_role=str(user.role),
        )
    except family.FamilyError as exc:
        raise shared.bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("family_unlinked")
    return {"removed": user_id}
