"""F6 受けられる支援と申請の状況の API（要件定義書F6・10-5、開発手順書 Step 10）。

- `GET   /api/programs`                               見られる支援制度の一覧（AIを使わない）
- `GET   /api/survivors/{id}/supports`                その人に記録した支援の一覧
- `POST  /api/survivors/{id}/support-candidates`      受けられそうな支援を理由つきで返す
- `POST  /api/survivors/{id}/supports`                示した支援を記録する（未申請で始まる）
- `PATCH /api/survivors/{id}/supports/{support_id}`   申請の状況を更新する

一覧の2本は、本人の「支援」の画面のために足した（使いやすさの改修の計画.md 6-2）。

守ること（要件定義書F6・10-3・10-5・10-6）：

- **使えるのは本人と専門職だけ。** 一般の支援者は 403（要件定義書446行の「支援制度の候補
  （F6）は扱わない」・601行の「見られる人は本人と同じ組織の専門職」）。
- **渡した制度の一覧にないIDは記録しない**（10-6。AIが返したIDも、要求に書かれたIDも、
  その組織の `stg_programs` と照らしてから保存する）。
- **上書きしない。** 示したときと状況を変えたときを追記する（10-4）。
- 見る権限のない記録は 404（`shared.load_visible`）。
- 「目安であり、受けられるかは自治体が決める」は**応答に必ず入れる**（文言の正本は
  `services/supports.py`。画面に書き写すとずれる。Step 8 と同じ形）。
- 氏名・住宅の被害の程度・制度の名前をログに出さない（10-2の6・11-3）。

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
from backend.services import bq, consultations, support_ai, supports, survivors, users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["F6"])

# 使えるのは本人と専門職（要件定義書F6の「使える人」）。1か所にまとめて取り違えを防ぐ。
# 組織がない本人も、全国共通の制度を見られる（追加修正②。`require_roles_or_self`）。
survivor_or_professional = auth.require_roles_or_self(
    users.ROLE_SURVIVOR, users.ROLE_PROFESSIONAL
)


# --- 要求の形 ---


class CandidatesRequest(BaseModel):
    """住宅の被害の程度・世帯の人数・再建の方法の意向（要件定義書F6）。"""

    housing_damage: str = Field(default="", description="住宅の被害の程度（選択肢のコード）")
    rebuild_intent: str = Field(default="", description="再建の方法の意向（選択肢のコード）")
    household_size: int | None = Field(
        default=None,
        description="世帯の人数（記録に無いときだけ使う。記録は変えない。使いやすさの改修の計画.md 6-4）",
    )


class SupportItem(BaseModel):
    """記録する支援1件（制度のIDと理由・期限）。"""

    program_id: str = Field(default="", description="制度のID（組織に登録されたものだけ）")
    reason: str | None = Field(default=None, description="受けられそうな理由")
    deadline: str | None = Field(default=None, description="申請の期限（YYYY-MM-DD）")


class SupportsCreateRequest(BaseModel):
    """示した支援をまとめて記録する（要件定義書F6）。"""

    housing_damage: str = Field(default="", description="示したときの住宅の被害の程度")
    rebuild_intent: str = Field(default="", description="示したときの再建の方法の意向")
    items: list[SupportItem] = Field(default_factory=list, description="記録する支援の一覧")
    from_list: bool = Field(
        default=False,
        description=(
            "制度の一覧から記録するか（真なら被害の程度と意向を空にできる。"
            "すでに記録した制度は記録し直さない。使いやすさの改修の計画.md 6-2）"
        ),
    )


class SupportStatusRequest(BaseModel):
    """申請の状況を更新する（要件定義書F6）。"""

    status: str = Field(default="", description="未申請・申請中・受給済み・対象外のどれか")
    deadline: str | None = Field(
        default=None, description="申請の期限（渡さなければいまの期限のまま）"
    )


# --- F6-0. 一覧（AIを使わない。使いやすさの改修の計画.md 6-2） ---


@router.get("/programs")
async def get_programs(
    user: auth.CurrentUser = Depends(survivor_or_professional),
) -> dict[str, Any]:
    """見られる支援制度をすべて返す（**Gemini は呼ばない**）。

    - 全国共通の制度と、その人の組織の制度だけ（`supports.list_programs`）。
    - 組織は要求に書かれた値ではなく、`stg_users` から読み直した値を使う（要件定義書10-3）。
      組織がない本人は、全国共通の制度だけになる。
    - 一般の支援者は 403（F6 は扱わない。要件定義書446行）。
    """
    try:
        programs = await asyncio.to_thread(supports.list_programs, org_id=user.org_id or "")
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    logger.info("programs_listed: role=%s count=%d", user.role, len(programs))
    return {
        "programs": [supports.program_payload(row) for row in programs],
        "count": len(programs),
        # 目安であることは必ず返す（要件定義書F6の最後）
        "disclaimer": supports.DISCLAIMER,
    }


@router.get("/survivors/{survivor_id}/supports")
async def get_supports(
    survivor_id: str,
    user: auth.CurrentUser = Depends(survivor_or_professional),
) -> dict[str, Any]:
    """その人に記録した支援と申請の状況を返す（詳細の画面の読みをしない軽い版）。

    見てよいかを先に確かめ、見られない記録は 404（`shared.load_visible`。要件定義書10-3）。
    本人のホームの知らせと、「あなたの支援」のタブが使う。
    """
    await shared.load_visible(survivor_id, user)
    try:
        rows = await asyncio.to_thread(supports.list_for_survivor, survivor_id=survivor_id)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    logger.info("supports_listed: role=%s count=%d", user.role, len(rows))
    return {
        "survivor_id": survivor_id,
        "supports": [supports.payload(row, role=str(user.role)) for row in rows],
        # 本人のホームに知らせを出す日数（期限切れも含む。11-2）
        "alert_days": supports.SURVIVOR_ALERT_DAYS,
        "disclaimer": supports.DISCLAIMER,
    }


# --- F6-1. 受けられそうな支援（保存はしない） ---


@router.post("/survivors/{survivor_id}/support-candidates")
async def suggest_supports(
    survivor_id: str,
    body: CandidatesRequest,
    user: auth.CurrentUser = Depends(survivor_or_professional),
) -> dict[str, Any]:
    """受けられそうな支援を理由・申請先・期限つきで返す（**保存はしない**。要件定義書F6）。

    渡すのは `stg_programs` に登録された制度の一覧だけで、返ったIDは一覧と照らす
    （`services/support_ai.py`。要件定義書10-6）。

    - 世帯の人数は**記録の値を使う**。記録に無いときだけ、要求の値をその場で使う。
      **この画面からは記録を変えない**（使いやすさの改修の計画.md 5章・6-4。前は記録と違う数で
      調べると、何も聞かずに記録の版を足していた）。直すのはプロフィールか基本の情報の画面。
    - その人の直近の相談も渡す（使いやすさの改修の計画.md 6-3。`support_ai.recent_consultations`）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id") or "")

    try:
        housing_damage = supports.validate_housing_damage(body.housing_damage)
        rebuild_intent = supports.validate_rebuild_intent(body.rebuild_intent)
    except supports.SupportValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    household_size = survivor.get("household_size")
    household_from_record = household_size is not None
    if not household_from_record and body.household_size is not None:
        try:
            household_size = survivors.validate_household_size(body.household_size)
        except survivors.SurvivorValidationError as exc:
            raise shared.bad_request(str(exc)) from exc

    # 制度の一覧・すでに示した支援・相談は互いに関係がないので同時に読む
    try:
        programs, existing, consultation_rows = await asyncio.gather(
            asyncio.to_thread(supports.list_programs, org_id=org_id),
            asyncio.to_thread(
                supports.list_for_survivor, survivor_id=survivor_id, org_id=org_id
            ),
            asyncio.to_thread(consultations.list_for_survivor, survivor_id=survivor_id),
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    consultation_items = support_ai.recent_consultations(consultation_rows)

    result = await asyncio.to_thread(
        support_ai.suggest,
        survivor=survivor,
        housing_damage=housing_damage,
        rebuild_intent=rebuild_intent,
        household_size=household_size,
        programs=programs,
        consultation_items=consultation_items,
    )

    # すでに記録してある制度は、二重に記録しないように印を付ける
    recorded = {str(row.get("program_id")) for row in existing}
    candidates = [
        {**item, "already_recorded": item["program_id"] in recorded}
        for item in result["candidates"]
    ]

    logger.info(
        "support_candidates: programs=%d consultations=%d candidates=%d dropped=%d ai_used=%s",
        len(programs),
        len(consultation_items),
        len(candidates),
        result["dropped_count"],
        result["ai_used"],
    )
    return {
        "survivor_id": survivor_id,
        "housing_damage": housing_damage,
        "rebuild_intent": rebuild_intent,
        "household_size": household_size,
        # 世帯の人数が記録の値か（画面は記録の値なら欄を出さず、値だけを見せる）
        "household_from_record": household_from_record,
        # 調べるのに使った相談の数（画面に「相談の内容も使って調べました」と出す）
        "consultation_count": len(consultation_items),
        "candidates": candidates,
        # 候補に出なかった制度も自分で選べる（制度の一覧を返す。要件定義書F6）
        "programs": [supports.program_payload(row) for row in programs],
        "supports": [supports.payload(row, role=str(user.role)) for row in existing],
        "ai_used": result["ai_used"],
        "notice": result["notice"],
        # 目安であることは必ず返す（要件定義書F6の最後）
        "disclaimer": supports.DISCLAIMER,
    }


# --- F6-2. 示した支援の記録 ---


@router.post("/survivors/{survivor_id}/supports", status_code=status.HTTP_201_CREATED)
async def create_supports(
    survivor_id: str,
    body: SupportsCreateRequest,
    user: auth.CurrentUser = Depends(survivor_or_professional),
) -> dict[str, Any]:
    """示した支援を記録する（状況は**未申請**から始まる。要件定義書F6）。

    **保存するのは、その組織に登録された制度だけ。** 要求に書かれたIDをそのまま信じない
    （要件定義書10-3）。確かめるのは1回の読み（制度の一覧）で済ませる。

    制度の一覧から記録するとき（`from_list`。使いやすさの改修の計画.md 6-2）は、
    住宅の被害の程度と意向を空にできる。空でない値は今までどおり選択肢と照らす。
    また、**すでに記録した制度は記録し直さない**（記録し直すと、申請中などの状況が
    未申請に戻るため）。記録した支援の読みは、制度の一覧の読みと同時に走らせる。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id") or "")

    try:
        if body.from_list:
            housing_damage = supports.optional_housing_damage(body.housing_damage)
            rebuild_intent = supports.optional_rebuild_intent(body.rebuild_intent)
        else:
            housing_damage = supports.validate_housing_damage(body.housing_damage)
            rebuild_intent = supports.validate_rebuild_intent(body.rebuild_intent)
    except supports.SupportValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    if not body.items:
        raise shared.bad_request("記録する支援が選ばれていません")

    recorded: set[str] = set()
    try:
        if body.from_list:
            programs, existing = await asyncio.gather(
                asyncio.to_thread(supports.list_programs, org_id=org_id),
                asyncio.to_thread(supports.list_for_survivor, survivor_id=survivor_id),
            )
            recorded = {str(row.get("program_id")) for row in existing}
        else:
            programs = await asyncio.to_thread(supports.list_programs, org_id=org_id)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    known = {str(row.get("program_id")): row for row in programs}
    if body.from_list and body.items and all(
        (item.program_id or "").strip() in recorded for item in body.items
    ):
        raise shared.bad_request("この支援は、もう記録してあります。「あなたの支援」で見られます")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set(recorded)
    for item in body.items:
        program_id = (item.program_id or "").strip()
        if program_id not in known or program_id in seen:
            # 登録されていない制度と、同じ制度の二重指定は落とす（黙って作らない）
            continue
        seen.add(program_id)
        try:
            rows.append(
                supports.build_suggested_row(
                    org_id=org_id,
                    survivor_id=survivor_id,
                    created_by=user.uid,
                    created_role=str(user.role),
                    program_id=program_id,
                    reason=item.reason,
                    deadline=item.deadline,
                    housing_damage=housing_damage,
                    rebuild_intent=rebuild_intent,
                    # 全国共通の制度は、組織に関係なく同じ記録のIDにする（追加修正②）
                    program_org_id=str(known[program_id].get("org_id") or ""),
                    conditions_optional=body.from_list,
                )
            )
        except supports.SupportValidationError as exc:
            raise shared.bad_request(str(exc)) from exc

    if not rows:
        raise shared.bad_request(
            "登録されている制度が選ばれていません。制度の一覧から選び直してください"
        )

    saved: list[dict[str, Any]] = []
    for row in rows:
        try:
            saved.append(await asyncio.to_thread(supports.save, row))
        except bq.BigQueryUnavailable as exc:
            # 途中で落ちても、書けた分は残る（追記だけなので、やり直しても二重にならない）
            raise shared.unavailable() from exc

    logger.info("supports_created: count=%d dropped=%d", len(saved), len(body.items) - len(saved))
    return {
        "survivor_id": survivor_id,
        "supports": [
            supports.payload(_as_stg_row(row, known), role=str(user.role)) for row in saved
        ],
        "count": len(saved),
        "disclaimer": supports.DISCLAIMER,
    }


# --- F6-3. 申請の状況の更新 ---


@router.patch("/survivors/{survivor_id}/supports/{support_id}")
async def update_support_status(
    survivor_id: str,
    support_id: str,
    body: SupportStatusRequest,
    user: auth.CurrentUser = Depends(survivor_or_professional),
) -> dict[str, Any]:
    """申請の状況を更新する（**前の行は消さず、追記する**。要件定義書F6・10-4）。

    ほかの人・ほかの組織の支援の記録は、存在しないときと同じ 404（読みで絞る）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id") or "")

    try:
        current = await asyncio.to_thread(
            supports.fetch_support,
            support_id=support_id,
            survivor_id=survivor_id,
            org_id=org_id,
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if not current:
        raise shared.not_found()

    try:
        row = supports.build_status_row(
            current=current,
            created_by=user.uid,
            created_role=str(user.role),
            status=body.status,
            deadline=body.deadline,
        )
    except supports.SupportValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(supports.save, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("support_status_changed: status=%s role=%s", saved.get("status"), user.role)
    return {
        "survivor_id": survivor_id,
        "support": supports.payload(
            {
                **current,
                "status": saved.get("status"),
                "deadline": saved.get("deadline"),
                # ビューが計算した日数は古くなるので、期限から数え直させる
                "days_to_deadline": None,
                "updated_at": saved.get("created_at"),
                "updated_by": saved.get("created_by"),
            },
            role=str(user.role),
        ),
        "disclaimer": supports.DISCLAIMER,
    }


def _as_stg_row(saved: dict[str, Any], programs: dict[str, Any]) -> dict[str, Any]:
    """保存した行を `stg_supports_latest`（制度の情報つき）の1行と同じ形にする。

    いま書いたばかりなので、読み直さずに画面へ返せる。
    """
    program = programs.get(str(saved.get("program_id"))) or {}
    return {
        **saved,
        "suggested_reason": saved.get("reason"),
        "suggested_at": saved.get("created_at"),
        "updated_at": saved.get("created_at"),
        "updated_by": saved.get("created_by"),
        "days_to_deadline": None,
        "program_name": program.get("program_name"),
        "conditions": program.get("conditions"),
        "benefit": program.get("benefit"),
        "apply_to": program.get("apply_to"),
        "deadline_rule": program.get("deadline_rule"),
        "documents": program.get("documents"),
        "source_url": program.get("source_url"),
    }
