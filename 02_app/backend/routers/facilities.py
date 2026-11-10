"""F5 受け入れ先の施設と移る先の決定の API（要件定義書F5・10-5、開発手順書 Step 9-1・9-2）。

- `GET   /api/facilities`                            施設の一覧（専門職だけ）
- `POST  /api/facilities`                            施設を登録する（専門職だけ）
- `PATCH /api/facilities/{facility_id}`              空き・断水・停電を更新する（版を足す）
- `POST  /api/survivors/{id}/facility-candidates`    施設の候補を最大3件、理由とともに返す
- `POST  /api/survivors/{id}/placements`             移る先を決める（理由を残す）

施設の職員（使いやすさの改修の計画.md 9章）：

- `GET   /api/facilities/{facility_id}/staff-join-code`  施設の参加コードを見る（専門職だけ）
- `POST  /api/facilities/{facility_id}/staff-join-code`  施設の参加コードを発行し直す（専門職だけ）
- `GET   /api/my-facility`                              自分の施設を読む（施設の職員だけ）
- `PATCH /api/my-facility`                              自分の施設の空き・断水・停電・対応できる
                                                        介護と医療を直す（施設の職員だけ。版を足す）

守ること（要件定義書F5・10-3・10-5・10-6）：

- **使えるのは専門職だけ。** 本人と一般の支援者は 403（10-5の「使える人」。一般の支援者は
  施設の情報を扱わない＝要件定義書446行）。
- **空きがない施設と、必要な医療に対応できない施設は、候補を並べる前に外す**（F5）。
  外した施設と理由は画面に返す（なぜ候補に出ないかが分かるようにする）。
- **候補はルールで並べ、AI は使わない**（画面の改修の計画.md 7章。`facilities.rank_candidates`）。
- **候補にない施設も選べる。** 選べる施設の一覧を応答に入れる（F5）。
- **移る先に保存するのは、その組織に実在する施設だけ。** 要求に書かれた候補をそのまま
  保存しない（10-3）。確かめるのは1回の読み（組織の施設の一覧）で済ませる。
- 上書きはしない。施設の更新は版を1つ足す（10-4）。
- 見る権限のない記録は 404（`shared.load_visible`）。
- 施設の名前・被災者の氏名・健康の情報をログに出さない（10-2の6・11-3）。
- **施設の職員は、自分の施設だけ**を読み書きする。施設のIDは要求から受け取らず、
  `stg_users` から読んだ値（`auth.require_facility_staff`）を使う。組織でも絞って読むので、
  施設が別の組織に属していれば 404 になる。被災者の記録のAPIは `auth.registered_user` が断る。
- 施設の参加コードの値はログに出さない。ハッシュは画面に返さない。

BigQuery は同期なので、読み書きはすべて `asyncio.to_thread` で別のスレッドに出す。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from backend import auth
from backend.routers import shared
from backend.services import bq, facilities, health, users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["F5"])

# 専門職だけが使う（要件定義書F5）。1か所にまとめて、取り違えを防ぐ。
only_professional = auth.require_roles(users.ROLE_PROFESSIONAL)


# --- 要求の形 ---


class FacilityCreateRequest(BaseModel):
    """施設を登録する（要件定義書F5の持つ情報）。"""

    facility_name: str = Field(default="", description="施設の名前")
    facility_type: str = Field(default="", description="種類（福祉避難所・介護施設・病院など）")
    address: str | None = Field(default=None, description="場所")
    vacancies: int | None = Field(default=None, description="空きの数")
    capabilities: list[str] = Field(
        default_factory=list, description="対応できる介護・医療（選択肢のコード）"
    )
    water_outage: bool = Field(default=False, description="断水しているか")
    power_outage: bool = Field(default=False, description="停電しているか")


class FacilityUpdateRequest(BaseModel):
    """空き・断水・停電を更新する（渡さなかった項目は前の版のまま）。"""

    vacancies: int | None = Field(default=None, description="空きの数")
    water_outage: bool | None = Field(default=None, description="断水しているか")
    power_outage: bool | None = Field(default=None, description="停電しているか")


class MyFacilityUpdateRequest(FacilityUpdateRequest):
    """施設の職員が、自分の施設を直す（9章）。対応できる介護・医療も直せる。

    渡さなかった項目は前の版のまま。対応できる介護・医療は、渡したときは**その一覧に
    置き換える**（空の一覧なら「なし」にする）。
    """

    capabilities: list[str] | None = Field(
        default=None, description="対応できる介護・医療（選択肢のコード）"
    )


class PlacementRequest(BaseModel):
    """移る先を決める（要件定義書F5）。"""

    chosen_facility_id: str = Field(default="", description="選んだ施設のID")
    chosen_reason: str = Field(default="", description="選んだ理由（必須）")
    candidates: list[dict[str, Any]] = Field(
        default_factory=list, description="AIが出した候補（施設のIDと理由）"
    )


# --- F5-1. 施設の一覧 ---


@router.get("/facilities")
async def list_facilities(
    user: auth.CurrentUser = Depends(only_professional),
) -> dict[str, Any]:
    """組織の施設の一覧を返す（要件定義書F5）。

    最終更新から24時間以上たった施設には、そのことを表す知らせを付ける
    （古いかどうかの判断は `stg_facilities_latest.is_stale` が正本）。
    """
    try:
        rows = await asyncio.to_thread(facilities.list_facilities, org_id=str(user.org_id))
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    items = [facilities.payload(row) for row in rows]
    return {
        "facilities": items,
        "count": len(items),
        "stale_count": sum(1 for item in items if item["is_stale"]),
    }


# --- F5-2. 施設の登録 ---


@router.post("/facilities", status_code=status.HTTP_201_CREATED)
async def create_facility(
    body: FacilityCreateRequest,
    user: auth.CurrentUser = Depends(only_professional),
) -> dict[str, Any]:
    """施設を登録する（版1として追記する。要件定義書F5）。"""
    try:
        row = facilities.build_row(
            org_id=str(user.org_id),
            created_by=user.uid,
            created_role=str(user.role),
            fields=body.model_dump(),
            version=1,
        )
    except facilities.FacilityValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(facilities.save, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    # 施設の名前はログに出さない
    logger.info("facility_created: type=%s", saved.get("facility_type"))
    return {"facility": facilities.payload(_as_stg_row(saved))}


# --- F5-3. 施設の更新（空き・断水・停電） ---


@router.patch("/facilities/{facility_id}")
async def update_facility(
    facility_id: str,
    body: FacilityUpdateRequest,
    user: auth.CurrentUser = Depends(only_professional),
) -> dict[str, Any]:
    """空きの数と断水・停電を更新する（**版を1つ足す**。要件定義書F5・10-4）。

    名前・種類・場所・対応できる介護・医療は前の版から引き継ぐ。
    ほかの組織の施設は、存在しないときと同じ 404（組織で絞って読む）。
    """
    try:
        # 施設の参加コードも読む（新しい版に引き継ぐため。9章）
        current = await asyncio.to_thread(
            facilities.fetch_facility,
            facility_id=facility_id,
            org_id=str(user.org_id),
            with_join_code=True,
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if not current:
        raise shared.not_found()

    try:
        row = facilities.build_update_row(
            current=current,
            created_by=user.uid,
            created_role=str(user.role),
            vacancies=body.vacancies,
            water_outage=body.water_outage,
            power_outage=body.power_outage,
        )
    except facilities.FacilityValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(facilities.save, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("facility_updated: version=%s", saved.get("version"))
    return {"facility": facilities.payload(_as_stg_row(saved))}


def _as_stg_row(saved: dict[str, Any]) -> dict[str, Any]:
    """保存した行を `stg_facilities_latest` の1行と同じ形にする（読み直さずに返すため）。

    いま書いたばかりなので `is_stale` は必ず false。
    """
    return {
        **saved,
        "updated_at": saved.get("created_at"),
        "updated_by": saved.get("created_by"),
        "updated_role": saved.get("created_role"),
        "is_stale": False,
    }


# --- 施設の参加コード（使いやすさの改修の計画.md 9章。専門職だけ） ---
#
# 施設を登録した組織の専門職（オーナーも専門職）が、施設の画面で見る・発行し直す。
# ほかの組織の施設は、組織で絞って読むので 404 になる。


@router.get("/facilities/{facility_id}/staff-join-code")
async def get_staff_join_code(
    facility_id: str,
    user: auth.CurrentUser = Depends(only_professional),
) -> dict[str, Any]:
    """施設の参加コード（元のコード）を返す。まだ発行していなければ `available` が false。"""
    try:
        current = await asyncio.to_thread(
            facilities.fetch_facility,
            facility_id=facility_id,
            org_id=str(user.org_id),
            with_join_code=True,
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if not current:
        raise shared.not_found()
    return facilities.join_code_payload(current)


@router.post("/facilities/{facility_id}/staff-join-code")
async def issue_staff_join_code(
    facility_id: str,
    user: auth.CurrentUser = Depends(only_professional),
) -> dict[str, Any]:
    """施設の参加コードを発行し直す（版を1つ足す）。前のコードでは、もう入れない。

    すでに入っている施設の職員は、そのまま使える（立場は `stg_users` が持つため）。
    """
    try:
        current = await asyncio.to_thread(
            facilities.fetch_facility,
            facility_id=facility_id,
            org_id=str(user.org_id),
            with_join_code=True,
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if not current:
        raise shared.not_found()

    try:
        row, _code = facilities.build_join_code_row(
            current=current, created_by=user.uid, created_role=str(user.role)
        )
    except facilities.FacilityValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(facilities.save, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    # コードの値はログに出さない
    logger.info("facility_join_code_issued: version=%s", saved.get("version"))
    return facilities.join_code_payload(saved)


# --- 施設の職員が自分の施設を直す（使いやすさの改修の計画.md 9章） ---


async def _load_my_facility(user: auth.CurrentUser, *, with_join_code: bool) -> dict[str, Any]:
    """施設の職員の施設を読む。施設のIDも組織も `stg_users` から読んだ値だけを使う。

    施設が見つからない（別の組織に属している・IDが壊れている）ときは 404。
    """
    try:
        current = await asyncio.to_thread(
            facilities.fetch_facility,
            facility_id=str(user.facility_id),
            org_id=str(user.org_id),
            with_join_code=with_join_code,
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if not current:
        raise shared.not_found()
    return current


@router.get("/my-facility")
async def get_my_facility(
    user: auth.CurrentUser = Depends(auth.require_facility_staff),
) -> dict[str, Any]:
    """自分の施設を返す（施設の職員だけ）。参加コードは返さない。"""
    current = await _load_my_facility(user, with_join_code=False)
    return {"facility": facilities.payload(current)}


@router.patch("/my-facility")
async def update_my_facility(
    body: MyFacilityUpdateRequest,
    user: auth.CurrentUser = Depends(auth.require_facility_staff),
) -> dict[str, Any]:
    """自分の施設の空き・断水・停電・対応できる介護と医療を直す（**版を1つ足す**）。

    名前・種類・場所は前の版のまま（施設の職員は変えない。変えるのは専門職が登録し直す）。
    施設の参加コードも前の版から引き継ぐ。
    """
    current = await _load_my_facility(user, with_join_code=True)
    try:
        # 対応できる介護・医療は、選択肢のコードだけを通す（build_row の clean_fields が確かめる）
        row = facilities.build_update_row(
            current=current,
            created_by=user.uid,
            created_role=str(user.role),
            vacancies=body.vacancies,
            water_outage=body.water_outage,
            power_outage=body.power_outage,
            capabilities=body.capabilities,
        )
    except facilities.FacilityValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(facilities.save, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("my_facility_updated: version=%s", saved.get("version"))
    return {"facility": facilities.payload(_as_stg_row(saved))}


# --- F5-4. 施設の候補（要件定義書F5、画面の改修の計画.md 7章） ---


@router.post("/survivors/{survivor_id}/facility-candidates")
async def suggest_facilities(
    survivor_id: str,
    user: auth.CurrentUser = Depends(only_professional),
) -> dict[str, Any]:
    """合う施設の候補を最大3件、理由とともに返す（**保存はしない**。要件定義書F5）。

    **空きがない施設と必要な医療に対応できない施設は、並べる前に外す。** 残りはルールで並べる
    （AI は使わない。画面の改修の計画.md 7章）。外した施設と理由も返す
    （なぜ候補に出ないかが画面で分かるようにする）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id"))

    # 健康・介護の情報と施設の一覧は、互いに関係がないので同時に読む
    try:
        health_row, rows = await asyncio.gather(
            asyncio.to_thread(health.fetch_latest, survivor_id=survivor_id, org_id=org_id),
            asyncio.to_thread(facilities.list_facilities, org_id=org_id),
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    medical_needs = list((health_row or {}).get("medical_needs") or [])
    _, unmatched = facilities.required_capabilities(medical_needs)
    eligible, excluded = facilities.filter_eligible(rows, medical_needs=medical_needs)

    candidates = facilities.rank_candidates(
        eligible, medical_needs=medical_needs, health_row=health_row
    )

    logger.info(
        "facility_candidates: eligible=%d excluded=%d candidates=%d",
        len(eligible),
        len(excluded),
        len(candidates),
    )
    return {
        "survivor_id": survivor_id,
        "candidates": candidates,
        "excluded": excluded,
        # 候補にない施設も選べる（要件定義書F5）。渡した施設の一覧を返す
        "selectable": [facilities.payload(row) for row in eligible],
        "unmatched_needs": unmatched,
        "unmatched_notice": facilities.UNMATCHED_NOTICE if unmatched else None,
        # AI は使わない（画面の互換のために項目は残す。7-2）
        "ai_used": False,
        "notice": None if eligible else facilities.NO_ELIGIBLE_NOTICE,
    }


# --- F5-5. 移る先の決定 ---


@router.post("/survivors/{survivor_id}/placements", status_code=status.HTTP_201_CREATED)
async def decide_placement(
    survivor_id: str,
    body: PlacementRequest,
    user: auth.CurrentUser = Depends(only_professional),
) -> dict[str, Any]:
    """選んだ施設と理由を残す（要件定義書F5）。

    **保存するのは、その組織に実在する施設だけ。** 候補のIDも組織の施設の一覧と
    照らして、実在しないものは落とす（要求に書かれた値をそのまま保存しない。10-3）。
    決めたあとは、居場所の変更（F1）と引き継ぎシート（F3）へ進めるための値を返す。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id"))

    try:
        rows = await asyncio.to_thread(facilities.list_facilities, org_id=org_id)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    known = {str(row.get("facility_id")): row for row in rows}
    chosen_id = (body.chosen_facility_id or "").strip()
    chosen = known.get(chosen_id)
    if not chosen:
        raise shared.bad_request(
            "選んだ施設が見つかりません。施設の一覧から選び直してください"
        )

    # 候補も、組織の施設にあるものだけを残す
    candidates = [
        item
        for item in facilities.clean_candidates(body.candidates)
        if item["facility_id"] in known
    ]

    try:
        row = facilities.build_placement_row(
            survivor_id=survivor_id,
            org_id=org_id,
            created_by=user.uid,
            created_role=str(user.role),
            candidates=candidates,
            chosen_facility_id=chosen_id,
            chosen_reason=body.chosen_reason,
        )
    except facilities.FacilityValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(facilities.save_placement, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info(
        "placement_decided: from_candidates=%s", saved.get("chosen_from_candidates")
    )
    names = {
        facility_id: str(item.get("facility_name") or "")
        for facility_id, item in known.items()
    }
    facility_payload = facilities.payload(chosen)
    return {
        "placement": facilities.placement_payload(
            {
                **saved,
                "decided_at": saved.get("created_at"),
                "decided_by": saved.get("created_by"),
                "decided_role": saved.get("created_role"),
            },
            facility_names=names,
        ),
        "facility": facility_payload,
        # 施設を決めたら、居場所の変更と引き継ぎシートへ進める（要件定義書F5）
        "next_steps": {
            "location_type": _location_type_for(chosen),
            "location_detail": facility_payload["facility_name"],
            "handover_destination": facility_payload["facility_name"],
        },
    }


# 施設の種類から、居場所の種類（F1）の候補を決める。
# **要件定義書は対応づけを決めていない**ため、ここで決めた（画面の初期値にするだけで、
# 変えるのは専門職。開発ログの要確認に記録する）。
_LOCATION_TYPE_BY_FACILITY_TYPE = {
    "welfare_shelter": "welfare_shelter",
    "care_facility": "care_facility",
    "hospital": "hospital",
    "clinic": "hospital",
    "secondary": "hotel",
}


def _location_type_for(facility: dict[str, Any]) -> str | None:
    return _LOCATION_TYPE_BY_FACILITY_TYPE.get(str(facility.get("facility_type") or ""))
