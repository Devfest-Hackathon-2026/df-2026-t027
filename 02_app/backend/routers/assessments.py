"""F4 悪化の兆しの API（要件定義書F4・10-5、開発手順書 Step 7-3）。

- `PUT /api/survivors/{id}/assessment`  専門職が度合いを変える（理由を残す）

守ること（要件定義書F4・10-3・10-5）：

- **度合いを出す処理は、画面から呼ぶAPIにしない。** F2 の記録と F8 の相談を保存した
  直後に、サーバーの中で続けて走らせる（`services/risk_ai.py`。10-5の注）。
  ここにあるのは「人が確かめて直す」1本だけ。
- **直せるのは専門職だけ**（要件定義書10-5の「使える人」）。一般の支援者と本人は 403。
- 見る権限のない記録は 404（`shared.load_visible`）。
- **理由のない上書きは受け取らない**（`assessments.clean_override_reason`）。
- 氏名・記録の本文・度合いを変えた理由をログに出さない（10-2の6・11-3）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend import auth
from backend.routers import shared
from backend.services import assessments, bq, users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/survivors", tags=["F4"])


class AssessmentOverrideRequest(BaseModel):
    """専門職がAIの判断を確かめ、度合いを変える（要件定義書F4）。"""

    level: str = Field(default="", description="度合い（high・medium・low）")
    reason: str = Field(default="", description="そう変えた理由（必須）")


@router.put("/{survivor_id}/assessment")
async def override_assessment(
    survivor_id: str,
    body: AssessmentOverrideRequest,
    user: auth.CurrentUser = Depends(auth.require_roles(users.ROLE_PROFESSIONAL)),
) -> dict[str, Any]:
    """悪化の兆しの度合いを、専門職が変える（前の判断は消さず1行足す。要件定義書F4）。

    根拠と次に必要な対応は前の判断から引き継ぐ。**読みは増やさない**
    （見てよいかを確かめるときに読んだ `int_survivor_list` の行をそのまま使う）。
    """
    row = await shared.load_visible(survivor_id, user)

    try:
        override = assessments.build_override_row(
            current=row,
            level=body.level,
            reason=body.reason,
            created_by=user.uid,
            created_role=str(user.role),
        )
    except assessments.AssessmentValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(assessments.save, override)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("assessment_override: level=%s", saved.get("level"))
    return {
        "risk": assessments.risk_payload(
            {
                **row,
                "risk_level": saved.get("level"),
                "risk_evidence": saved.get("evidence"),
                "risk_next_action": saved.get("next_action"),
                "risk_source": saved.get("source"),
                "risk_assessed_at": saved.get("created_at"),
            }
        )
    }
