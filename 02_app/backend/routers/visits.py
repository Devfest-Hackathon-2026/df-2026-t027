"""F2 訪問・見守りの記録と F3 健康・介護の情報の API（要件定義書F2・F3・10-5、開発手順書 Step 6）。

- `POST /api/survivors/{id}/visits/structure`     文章を9つの項目に整えた案を返す（保存しない）
- `POST /api/survivors/{id}/visits`               確かめた記録を保存する
- `POST /api/survivors/{id}/visits/{vid}/apply`   確認待ちの更新の案を反映する（専門職だけ）
- `PUT  /api/survivors/{id}/health`               健康・介護の情報を保存する（版を残す）

記録を保存したあとは、**応答を返す前に F4 の悪化の兆しの判断を行う**（`services/risk_ai.py`。
要件定義書10-5の注。画面から呼ぶAPIにはしない）。

守ること（要件定義書F2・F3・10-3）：

- **整える処理は保存しない。** 人が確かめたあとの別の要求で保存する（10-5の「AI」の列）。
- **Gemini を呼べないときも記録できる。** 整える処理は 200 で `ai_used: false` を返し、
  画面が9つの項目の手入力の欄を出す。503 にすると「やり直し」に見えて、
  訪問先で記録を残せなくなる（要件定義書F2の最後）。
- **一般の支援者が書いた更新の案は反映しない。** 案があれば `pending` で保存し、
  専門職が `/apply` で反映する。専門職が自分で書いた記録も、保存 → 反映の2段で書く。
- **健康・介護の情報を更新できるのは専門職と本人だけ**（一般の支援者は見るだけ。要件定義書F3）。
- 立場と組織は `stg_users` から決める。要求に書かれた立場・組織のIDは使わない（10-3）。
- 見る権限のない記録は 404（`shared.load_visible`）。
- 記録の本文・氏名・健康の情報をログに出さない（10-2の6・11-3）。

BigQuery は同期なので、読み書きはすべて `asyncio.to_thread` で別のスレッドに出す
（`async def` の中から直接呼ぶとイベントループを塞ぐ。Step 5 で踏んだ）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from backend import auth
from backend.routers import shared
from backend.services import bq, gemini, health, risk_ai, survivors, users, visit_ai, visits

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/survivors", tags=["F2・F3"])

# Gemini を呼べなかったときに画面へ出す案内（手入力に切り替えてもらう）。
AI_UNAVAILABLE_NOTICE = (
    "AIで項目に整えられませんでした。下の欄に手で入れて保存してください"
    "（保存した内容はそのまま記録に残ります）"
)


# --- 要求の形 ---


class VisitStructureRequest(BaseModel):
    """文章を項目に整える（保存はしない）。"""

    raw_text: str = Field(default="", description="訪問・見守りの内容（文章）")


class LocationProposalBody(BaseModel):
    """居場所の更新の案（画面で直したものが返ってくる）。"""

    change_suggested: bool = Field(default=False)
    location_type: str | None = Field(default=None)
    location_detail: str | None = Field(default=None)
    reason: str | None = Field(default=None)


class HealthProposalBody(BaseModel):
    """健康・介護の更新の案（画面で直したものが返ってくる）。

    項目は `fields` の中に入れる。**`/visits/structure` が返す形と同じ**にしてある
    （画面は受け取った案をそのまま送り返すため、形がずれると案が黙って落ちる）。
    `fields` は Pydantic の BaseModel の名前と紛らわしいので、別名で受け取る。
    """

    model_config = ConfigDict(populate_by_name=True)

    change_suggested: bool = Field(default=False)
    reason: str | None = Field(default=None)
    proposal_fields: dict[str, Any] | None = Field(default=None, alias="fields")


class VisitSaveRequest(BaseModel):
    """確かめた記録を保存する。"""

    visited_at: dt.datetime | None = Field(default=None, description="訪問した日時（既定は今）")
    raw_text: str | None = Field(default=None, description="入力した文章（手入力のときは空でよい）")
    items: dict[str, str] = Field(default_factory=dict, description="整えた9つの項目")
    location_proposal: LocationProposalBody | None = Field(default=None)
    health_proposal: HealthProposalBody | None = Field(default=None)
    ai_used: bool = Field(default=True, description="AIで整えたか（手入力は false）")
    # 専門職が自分で書いた記録を、その場で反映するときだけ true にする（要件定義書F2）
    apply_location: bool = Field(default=False, description="居場所の案をその場で反映する")
    apply_health: bool = Field(default=False, description="健康・介護の案をその場で反映する")


class ApplyRequest(BaseModel):
    """確認待ちの更新の案を反映する（専門職だけ）。"""

    items: list[str] = Field(default_factory=list, description="反映する内容（location・health）")
    result: str = Field(default=visits.RESULT_APPLIED, description="applied・rejected")


class HealthSaveRequest(BaseModel):
    """健康・介護の情報を保存する（版を1つ足す）。"""

    chronic_diseases: list[str] | str | None = Field(default=None, description="持病")
    medications: list[str] | str | None = Field(default=None, description="飲んでいる薬")
    allergies: str | None = Field(default=None, description="アレルギー")
    primary_clinic: str | None = Field(default=None, description="かかりつけの医療機関")
    medical_needs: list[str] | str | None = Field(default=None, description="必要な医療")
    care_level: str | None = Field(default=None, description="要介護度")
    assistance_mobility: str | None = Field(default=None, description="移動で必要な介助")
    assistance_meal: str | None = Field(default=None, description="食事で必要な介助")
    assistance_toilet: str | None = Field(default=None, description="排せつで必要な介助")
    dementia_notes: str | None = Field(default=None, description="認知症の有無と配慮すること")
    family_contact: str | None = Field(default=None, description="連絡してほしい家族など")

    def to_fields(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in health.FIELD_ORDER}


# --- 共通 ---


def _normalize_proposals(body: VisitSaveRequest) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """画面から返ってきた案をそろえる（人が直した値を正本にする）。"""
    location_proposal = visits.normalize_location_proposal(
        body.location_proposal.model_dump() if body.location_proposal else None
    )
    health_source: dict[str, Any] | None = None
    if body.health_proposal:
        health_source = {
            "change_suggested": body.health_proposal.change_suggested,
            "reason": body.health_proposal.reason,
            "fields": body.health_proposal.proposal_fields or {},
        }
    health_proposal = visits.normalize_health_proposal(health_source)
    return location_proposal, health_proposal


def _apply_location(
    *, survivor: dict[str, Any], proposal: dict[str, Any], user: auth.CurrentUser
) -> dict[str, Any]:
    """居場所の案を反映する（段階は今のものを引き継ぎ、版を1つ足す）。"""
    return survivors.change_location(
        current=survivor,
        stage=survivor.get("stage"),
        location_type=proposal.get("location_type"),
        location_detail=proposal.get("location_detail"),
        created_by=user.uid,
        created_role=str(user.role),
    )


def _apply_health(
    *, survivor_id: str, org_id: str, proposal: dict[str, Any], user: auth.CurrentUser
) -> dict[str, Any]:
    """健康・介護の案を反映する（最新の版に、空でない項目だけを重ねる）。"""
    current = health.fetch_latest(survivor_id=survivor_id, org_id=org_id)
    merged = health.merge(current, proposal.get("fields") or {})
    return health.save(
        survivor_id=survivor_id,
        org_id=org_id,
        created_by=user.uid,
        created_role=str(user.role),
        fields=merged,
        source=health.SOURCE_VISIT_PROPOSAL,
        current=current,
    )


async def _apply_proposals(
    *,
    survivor: dict[str, Any],
    visit_id: str,
    location_proposal: dict[str, Any] | None,
    health_proposal: dict[str, Any] | None,
    wanted: list[str],
    user: auth.CurrentUser,
    result: str,
) -> list[str]:
    """選ばれた案を反映し、反映したことを `raw_update_applies` に書く。

    反映できる案が無い組み合わせは 400 で断る（押せてしまう画面の誤りを拾うため）。
    """
    survivor_id = str(survivor.get("survivor_id"))
    org_id = str(survivor.get("org_id"))
    applied: list[str] = []

    if result == visits.RESULT_APPLIED:
        if visits.ITEM_LOCATION in wanted:
            if not location_proposal:
                raise shared.bad_request("居場所の更新の案がありません")
            await asyncio.to_thread(
                _apply_location, survivor=survivor, proposal=location_proposal, user=user
            )
            applied.append(visits.ITEM_LOCATION)
        if visits.ITEM_HEALTH in wanted:
            if not health_proposal:
                raise shared.bad_request("健康・介護の更新の案がありません")
            await asyncio.to_thread(
                _apply_health,
                survivor_id=survivor_id,
                org_id=org_id,
                proposal=health_proposal,
                user=user,
            )
            applied.append(visits.ITEM_HEALTH)

    await asyncio.to_thread(
        visits.record_apply,
        survivor_id=survivor_id,
        org_id=org_id,
        visit_id=visit_id,
        created_by=user.uid,
        created_role=str(user.role),
        applied_items=applied,
        result=result,
    )
    logger.info("visit_proposal_applied: result=%s items=%s", result, ",".join(applied))
    return applied


# --- F2-1. 文章を項目に整える（保存しない） ---


@router.post("/{survivor_id}/visits/structure")
async def structure_visit(
    survivor_id: str,
    body: VisitStructureRequest,
    user: auth.CurrentUser = Depends(
        auth.require_roles(users.ROLE_PROFESSIONAL, users.ROLE_VOLUNTEER)
    ),
) -> dict[str, Any]:
    """入力した文章を9つの項目に整えた案と、居場所・健康の更新の案を返す（要件定義書F2）。

    **保存はしない。** `ai_used` が false のときは、画面が手入力の欄を出す。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id"))
    try:
        raw_text = visits.clean_raw_text(body.raw_text)
    except visits.VisitValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    # 互いに関係のない2つを同時に読む（健康の情報と直近の記録は Gemini に渡す材料）
    try:
        health_row, recent = await asyncio.gather(
            asyncio.to_thread(health.fetch_latest, survivor_id=survivor_id, org_id=org_id),
            asyncio.to_thread(visits.list_visits, survivor_id=survivor_id, org_id=org_id, limit=3),
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    payload: dict[str, Any] = {
        "survivor_id": survivor_id,
        "health": health.payload(health_row),
        "current_location": {
            "location_type": survivor.get("location_type"),
            "location_type_label": survivors.LOCATION_TYPES.get(
                survivor.get("location_type") or "", ""
            ),
            "location_detail": survivor.get("location_detail"),
        },
        # 一般の支援者が書いた案は、専門職が確かめるまで反映されない（要件定義書F2）
        "can_apply": user.role == users.ROLE_PROFESSIONAL,
    }

    try:
        structured = await asyncio.to_thread(
            visit_ai.structure,
            raw_text=str(raw_text),
            survivor=survivor,
            health_row=health_row,
            recent_visits=recent,
        )
    except gemini.GeminiUnavailable as exc:
        # 訪問先で記録を残せるようにする（要件定義書F2の最後）。エラーにはしない
        logger.warning("visit_structure_unavailable: %s", type(exc).__name__)
        return {**payload, **visit_ai.empty_payload(), "ai_used": False, "notice": AI_UNAVAILABLE_NOTICE}
    except visits.VisitValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    return {**payload, **structured, "ai_used": True, "notice": None}


# --- F2-2. 確かめた記録を保存する ---


@router.post("/{survivor_id}/visits", status_code=status.HTTP_201_CREATED)
async def save_visit(
    survivor_id: str,
    body: VisitSaveRequest,
    user: auth.CurrentUser = Depends(
        auth.require_roles(users.ROLE_PROFESSIONAL, users.ROLE_VOLUNTEER)
    ),
) -> dict[str, Any]:
    """確かめた記録を保存する（要件定義書F2）。

    更新の案があれば `pending` で保存する。専門職が `apply_location`・`apply_health` を
    付けたときは、保存のあとに反映して `raw_update_applies` に書く。

    保存できたら、**応答を返す前に F4 の悪化の兆しの判断を行う**（要件定義書10-5の注）。
    判断の分（数秒）だけ応答が遅くなるが、Cloud Run は応答のあと CPU を止める設定のため、
    裏で走らせると判断が走り切らない（無料枠に収める修正の手順4-2）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id"))
    role = str(user.role)

    try:
        location_proposal, health_proposal = _normalize_proposals(body)
        row = visits.build_visit_row(
            survivor_id=survivor_id,
            org_id=org_id,
            created_by=user.uid,
            created_role=role,
            visited_at=visits.clean_visited_at(body.visited_at),
            raw_text=visits.clean_raw_text(body.raw_text, required=False),
            items=visits.clean_items(body.items),
            location_proposal=location_proposal,
            health_proposal=health_proposal,
            ai_used=body.ai_used,
        )
    except visits.VisitValidationError as exc:
        raise shared.bad_request(str(exc)) from exc
    except health.HealthValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(visits.save_visit, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    visit_id = str(saved.get("visit_id"))
    logger.info("visit_saved: role=%s ai_used=%s status=%s", role, body.ai_used, saved.get("proposal_status"))

    # 反映できるのは専門職だけ。要求に書かれた値で立場は変えない（要件定義書10-3）
    wanted: list[str] = []
    if role == users.ROLE_PROFESSIONAL:
        if body.apply_location and location_proposal:
            wanted.append(visits.ITEM_LOCATION)
        if body.apply_health and health_proposal:
            wanted.append(visits.ITEM_HEALTH)

    applied: list[str] = []
    apply_error: str | None = None
    if wanted:
        try:
            applied = await _apply_proposals(
                survivor=survivor,
                visit_id=visit_id,
                location_proposal=location_proposal,
                health_proposal=health_proposal,
                wanted=wanted,
                user=user,
                result=visits.RESULT_APPLIED,
            )
        except (
            bq.BigQueryUnavailable,
            survivors.SurvivorValidationError,
            health.HealthValidationError,
            visits.VisitValidationError,
        ):
            # 記録そのものは保存できている。**保存を失敗にはしない**（書き直させない）。
            # 確認待ちとして残るので、記録の一覧から `/apply` でやり直せる
            logger.warning("visit_apply_failed_after_save")
            apply_error = (
                "記録は保存しましたが、更新の案を反映できませんでした。"
                "記録の一覧から、もう一度反映してください"
            )

    # F4 の悪化の兆しの判断は、**保存の後、応答を返す前に行う**（要件定義書10-5の注）。
    # 画面から呼ぶAPIにはしない。失敗しても記録の保存は成功のままにする
    # （`risk_ai.assess_and_save` が例外を外に出さない）。
    # Cloud Run は応答のあと CPU を止める設定（最小0台）のため、裏では走らせない
    await asyncio.to_thread(
        risk_ai.assess_and_save,
        survivor=survivor,
        created_by=user.uid,
        created_role=role,
        trigger_id=visit_id,
    )
    return {
        "visit": visits.visit_payload(
            {
                **saved,
                "recorded_at": saved.get("created_at"),
                "recorded_role": role,
                "is_pending": saved.get("proposal_status") == visits.STATUS_PENDING and not applied,
                "apply_result": visits.RESULT_APPLIED if applied else None,
            }
        ),
        "applied": applied,
        "apply_error": apply_error,
    }


# --- F2-3. 確認待ちの更新の案を反映する ---


@router.post("/{survivor_id}/visits/{visit_id}/apply")
async def apply_visit_proposal(
    survivor_id: str,
    visit_id: str,
    body: ApplyRequest,
    user: auth.CurrentUser = Depends(auth.require_roles(users.ROLE_PROFESSIONAL)),
) -> dict[str, Any]:
    """確認待ちの更新の案を、専門職が確かめて反映する（要件定義書F2・10-5）。

    `result="rejected"` のときは何も反映せず、確かめたことだけを残す（確認待ちから外れる）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id"))

    if body.result not in (visits.RESULT_APPLIED, visits.RESULT_REJECTED):
        raise shared.bad_request("反映するかしないかを選んでください")

    try:
        visit = await asyncio.to_thread(
            visits.fetch_visit, visit_id=visit_id, survivor_id=survivor_id, org_id=org_id
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if visit is None:
        raise shared.not_found()
    if not visit.get("is_pending"):
        raise shared.bad_request("この記録の更新の案は、すでに確かめられています")

    location_proposal = visits.normalize_location_proposal(
        visits.as_dict(visit.get("location_proposal"))
    )
    health_proposal = visits.normalize_health_proposal(
        visits.as_dict(visit.get("health_proposal"))
    )

    try:
        applied = await _apply_proposals(
            survivor=survivor,
            visit_id=visit_id,
            location_proposal=location_proposal,
            health_proposal=health_proposal,
            wanted=list(body.items),
            user=user,
            result=body.result,
        )
    except survivors.SurvivorValidationError as exc:
        raise shared.bad_request(str(exc)) from exc
    except health.HealthValidationError as exc:
        raise shared.bad_request(str(exc)) from exc
    except visits.VisitValidationError as exc:
        raise shared.bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    return {"visit_id": visit_id, "result": body.result, "applied": applied}


# --- F3. 健康・介護の情報 ---


@router.put("/{survivor_id}/health")
async def save_health(
    survivor_id: str,
    body: HealthSaveRequest,
    user: auth.CurrentUser = Depends(
        auth.require_roles_or_self(users.ROLE_PROFESSIONAL, users.ROLE_SURVIVOR)
    ),
) -> dict[str, Any]:
    """健康・介護の情報を保存する（上書きせず版を1つ足す。要件定義書F3）。

    本人が書いたものは `source="self"`（本人の申告）として残す。
    一般の支援者はここを呼べない（F2 の更新の案を通す）。
    組織がない本人も自分の記録には書ける（追加修正②。見られるのは本人だけのまま）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id") or "")

    try:
        source = health.source_for_role(user.role)
        fields = health.clean_fields(body.to_fields())
    except health.HealthValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        current = await asyncio.to_thread(
            health.fetch_latest, survivor_id=survivor_id, org_id=org_id
        )
        saved = await asyncio.to_thread(
            health.save,
            survivor_id=survivor_id,
            org_id=org_id,
            created_by=user.uid,
            created_role=str(user.role),
            fields=fields,
            source=source,
            current=current,
        )
    except health.HealthValidationError as exc:
        raise shared.bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("health_saved: role=%s version=%s", user.role, saved.get("version"))
    return {
        "health": health.payload(
            {
                **saved,
                "updated_at": saved.get("created_at"),
                "updated_role": saved.get("created_role"),
            }
        )
    }
