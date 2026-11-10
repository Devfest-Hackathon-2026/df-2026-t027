"""F7 組織全体の状況の API（要件定義書F7・10-5、開発手順書 Step 11-1）。

- `GET /api/dashboard`  組織全体の集計（専門職だけ）

守ること（要件定義書F7・10-3・10-5）：

- **使えるのは専門職だけ**（10-5の表の「専」）。本人と一般の支援者は 403。
  本人に悪化の兆しの度合いを返さない決まり（10-3）とも合う。
- 読むのは mart の `mart_org_status` 1表だけで、**組織は自分の組織に固定する**
  （要求で組織を指定させない。指定させると別の組織の人数を読める経路になる）。
- mart がまだ作られていない・BigQuery を呼べないときは 503。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends

from backend import auth
from backend.routers import shared
from backend.services import bq, dashboard, users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["F7"])


@router.get("/dashboard")
async def read_dashboard(
    user: auth.CurrentUser = Depends(auth.require_roles(users.ROLE_PROFESSIONAL)),
) -> dict[str, Any]:
    """自分の組織の被災者の人数を、段階・居場所の種類・悪化の兆しの度合いごとに返す。"""
    try:
        payload = await asyncio.to_thread(dashboard.load, str(user.org_id))
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    return payload
