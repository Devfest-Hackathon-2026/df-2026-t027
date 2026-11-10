"""F3 引き継ぎシートの API（要件定義書F3・10-5、開発手順書 Step 9-3）。

- `POST /api/survivors/{id}/handovers/draft`  要約と気をつけることの案を返す（保存しない）
- `POST /api/survivors/{id}/handovers`        確かめた引き継ぎシートを保存する
- `GET  /api/survivors/{id}/handovers`        その人の引き継ぎシートの一覧（追加修正⑪）

### 見られる人（追加修正⑪）

その人の記録に関わる利用者は、その人の引き継ぎシートをすべて見られる。「関わる利用者」は
**記録を見られる人と同じ**にする（`shared.load_visible`）。シートは被災者のIDで読み、組織の
IDでは絞らない（組織がない本人も自分のシートを見られ、本人が組織を変えても、前の組織で
作ったシートが本人と新しい組織の専門職に見える）。前の組織の専門職・担当を外れた一般の
支援者は、記録を見られないので 404 になる。作れる人は今のまま（専門職と担当の一般の支援者）。

守ること（要件定義書F3・10-3・10-5）：

- **作れるのは専門職と、担当の一般の支援者だけ**（10-5の「使える人」は 専・一）。
  本人は 403（本人は**見るだけ**。詳細の応答には入る＝要件定義書7-1）。
  担当でない一般の支援者は 404（`shared.load_visible` が見える範囲を決める）。
- **案を作る処理は保存しない。** 作った人が確かめて直したあと、別の要求で保存する
  （F2・F8 と同じ形。10-5の「AI」の列）。
- **Gemini を呼べなくても保存できる。** 案が空で返るので、画面で手で書いて保存する
  （`ai_used: false` として残す）。
- **どこへ向けて作ったかを必ず残す**（移る先のないシートは 400）。
- 載せた健康・介護の情報の**版**を残す（要件定義書7-3-1）。版そのものを読み直すビューは
  無いため、あとで開いたシートには**いまの**情報が出る（`handovers.payload` が、保存した
  版と違うときに知らせを付ける）。
- 氏名・記録の本文・健康の情報をログに出さない（10-2の6・11-3）。

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
from backend.services import (
    bq,
    consultations,
    handover_ai,
    handovers,
    health,
    users,
    visits,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/survivors", tags=["F3"])

# 作れるのは専門職と担当の一般の支援者（要件定義書10-5）。
# 担当かどうかは `shared.load_visible` が確かめる（担当でなければ 404）。
can_create = auth.require_roles(users.ROLE_PROFESSIONAL, users.ROLE_VOLUNTEER)


# --- 要求の形 ---


class HandoverDraftRequest(BaseModel):
    """引き継ぎシートの案を作る（保存はしない）。"""

    destination: str = Field(default="", description="移る先（施設や支援者の名前）")


class HandoverSaveRequest(BaseModel):
    """確かめた引き継ぎシートを保存する。"""

    destination: str = Field(default="", description="移る先")
    summary: str = Field(default="", description="直近の記録と相談の要約")
    cautions: str | None = Field(default=None, description="気をつけること")
    health_version: int | None = Field(
        default=None, description="載せた健康・介護の情報の版"
    )
    ai_used: bool = Field(default=True, description="AIで要約を作ったか（手書きは false）")


# --- F3-0. その人の引き継ぎシートの一覧（追加修正⑪） ---


@router.get("/{survivor_id}/handovers")
async def list_handovers(
    survivor_id: str,
    user: auth.CurrentUser = Depends(auth.registered_or_self),
) -> dict[str, Any]:
    """その人の引き継ぎシートを新しい順に返す（印刷の画面にそのまま出せる形）。

    本人のホームの「引き継ぎシート」から、記録の詳細を開かずに開くためのAPI。
    見てよいかは記録と同じ（`shared.load_visible`。見られないものは 404）。
    印刷の画面に出す、いまの健康・介護の情報も一緒に返す。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id") or "")

    try:
        rows, health_row = await asyncio.gather(
            asyncio.to_thread(handovers.list_for_survivor, survivor_id=survivor_id),
            asyncio.to_thread(health.fetch_latest, survivor_id=survivor_id),
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    # 作った人の名前は、いまの組織の人だけ引ける（前の組織で作ったシートは名前が空になる）
    names = await asyncio.to_thread(
        shared.names_for_ids, org_id, [row.get("created_by") for row in rows]
    )
    health_version_now = (health_row or {}).get("version")
    return {
        "survivor": {
            "survivor_id": survivor_id,
            "full_name": survivor.get("full_name"),
            "age": survivor.get("age"),
        },
        "handovers": [
            handovers.payload(row, creator_names=names, health_version_now=health_version_now)
            for row in rows
        ],
        "health": health.payload(health_row),
    }


# --- F3-1. 案を作る（保存しない） ---


@router.post("/{survivor_id}/handovers/draft")
async def draft_handover(
    survivor_id: str,
    body: HandoverDraftRequest,
    user: auth.CurrentUser = Depends(can_create),
) -> dict[str, Any]:
    """要約と気をつけることの案を返す（要件定義書F3・10-6）。

    渡すのは「健康・介護の情報」「直近の訪問・見守りの記録」「直近の相談」「移る先」。
    保存はしない（作った人が確かめて直してから `POST /handovers`）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id"))

    try:
        destination = handovers.clean_destination(body.destination)
    except handovers.HandoverValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    # 互いに関係のない読みを同時に走らせる（順に読むと1秒ずつ積み上がる）
    try:
        health_row, visit_rows, consultation_rows = await asyncio.gather(
            asyncio.to_thread(health.fetch_latest, survivor_id=survivor_id, org_id=org_id),
            asyncio.to_thread(
                visits.list_visits,
                survivor_id=survivor_id,
                org_id=org_id,
                limit=handovers.RECENT_VISITS_FOR_AI,
            ),
            asyncio.to_thread(
                consultations.list_for_survivor,
                survivor_id=survivor_id,
                org_id=org_id,
                limit=handovers.RECENT_CONSULTATIONS_FOR_AI,
            ),
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    draft = await asyncio.to_thread(
        handover_ai.draft,
        survivor=survivor,
        health_row=health_row,
        recent_visits=visit_rows,
        recent_consultations=consultations.recent_for_ai(
            consultation_rows, limit=handovers.RECENT_CONSULTATIONS_FOR_AI
        ),
        destination=destination,
    )

    logger.info("handover_draft: ai_used=%s", draft["ai_used"])
    return {
        "survivor_id": survivor_id,
        "destination": destination,
        "draft": draft,
        # 保存のときにそのまま送れるように、載せる版を返す
        "health_version": (health_row or {}).get("version"),
        "health": health.payload(health_row),
        # AIの案であることを必ず画面に出す（要件定義書F3）
        "ai_disclaimer": handovers.AI_DISCLAIMER,
    }


# --- F3-2. 確かめたシートを保存する ---


@router.post("/{survivor_id}/handovers", status_code=status.HTTP_201_CREATED)
async def save_handover(
    survivor_id: str,
    body: HandoverSaveRequest,
    user: auth.CurrentUser = Depends(can_create),
) -> dict[str, Any]:
    """確かめた引き継ぎシートを保存する（要件定義書F3）。

    印刷の画面にそのまま出せるように、いまの健康・介護の情報も返す。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = str(survivor.get("org_id"))

    try:
        row = handovers.build_row(
            survivor_id=survivor_id,
            org_id=org_id,
            created_by=user.uid,
            created_role=str(user.role),
            destination=body.destination,
            summary=body.summary,
            cautions=body.cautions,
            health_version=body.health_version,
            ai_used=body.ai_used,
        )
    except handovers.HandoverValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved, health_row = await asyncio.gather(
            asyncio.to_thread(handovers.save, row),
            asyncio.to_thread(health.fetch_latest, survivor_id=survivor_id, org_id=org_id),
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("handover_saved: ai_used=%s", saved.get("ai_used"))
    return {
        "handover": handovers.payload(
            saved,
            creator_names={user.uid: str(user.display_name or "")},
            health_version_now=(health_row or {}).get("version"),
        ),
        "health": health.payload(health_row),
        "survivor": {
            "survivor_id": survivor_id,
            "full_name": survivor.get("full_name"),
            "age": survivor.get("age"),
        },
    }
