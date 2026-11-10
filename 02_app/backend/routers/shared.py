"""API で共通に使う断り方と、記録の見える範囲の確認（要件定義書10-3）。

F1（`routers/survivors.py`）と F2・F3（`routers/visits.py`）が同じ規則で断るようにする。
規則を2か所に書くと、片方だけ直したときに**見えない記録が 403 で「ある」と分かってしまう**。

- **見る権限のない記録は、存在しないときと同じ 404**（記録の有無も分からないようにする）。
- 立場で使えない操作は 403。この2つは混ぜない。
- BigQuery を呼べないときは 503（利用者には「もう一度」と伝える）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException, status

from backend import auth
from backend.services import bq, survivors, users

logger = logging.getLogger(__name__)


def bad_request(detail: Any) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def not_found() -> HTTPException:
    # 見る権限がないときも同じ応答にする（記録の有無を分からないようにする。要件定義書10-3）
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="記録が見つかりませんでした")


def unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="読み書きできませんでした。通信を確かめて、もう一度お試しください",
    )


async def load_visible(
    survivor_id: str, user: auth.CurrentUser, *, as_family: bool = False
) -> dict[str, Any]:
    """記録を読み、その人が見てよいか確かめる。見られないものは 404。

    **記録そのものの読みだけは単独で先に行う**（見てよいか確かめる前に関連の読みを走らせない）。

    `as_family=True` は家族の画面（`routers/family.py`）だけが使う。家族としてつながって
    いるかだけで決める（使いやすさの改修の計画.md 10章。`survivors.can_view` を参照）。
    """
    try:
        row = await asyncio.to_thread(survivors.fetch_survivor, survivor_id)
    except bq.BigQueryUnavailable as exc:
        raise unavailable() from exc
    if not survivors.can_view(
        row, role=user.role, user_id=user.uid, org_id=user.org_id, as_family=as_family
    ):
        logger.info("survivor_access_denied: role=%s family=%s", user.role, as_family)
        raise not_found()
    assert row is not None  # can_view が None を通さない
    return row


def supporter_names(org_id: str | None) -> dict[str, str]:
    """一般の支援者のIDと表示名の対応（担当や記録した人の表示に使う）。"""
    if not org_id:
        return {}
    try:
        rows = users.list_by_role(org_id=org_id, role=users.ROLE_VOLUNTEER)
    except bq.BigQueryUnavailable:
        # 名前が出ないだけなので、表示そのものは止めない
        logger.warning("supporter_names_failed")
        return {}
    return {str(row.get("user_id")): str(row.get("display_name") or "") for row in rows}


def member_rows(org_id: str | None) -> list[dict[str, Any]]:
    """専門職と一般の支援者の一覧（**読みは1回**）。

    記録した人の名前（要件定義書594行の「記録した人と立場」）を出すには専門職も要るが、
    担当の候補に出せるのは一般の支援者だけなので、`role` の列ごと返して呼ぶ側に選ばせる。
    """
    if not org_id:
        return []
    try:
        return users.list_by_role(
            org_id=org_id, role=(users.ROLE_PROFESSIONAL, users.ROLE_VOLUNTEER)
        )
    except bq.BigQueryUnavailable:
        # 名前が出ないだけなので、表示そのものは止めない
        logger.warning("member_rows_failed")
        return []


def names_for_ids(org_id: str | None, user_ids: Any) -> dict[str, str]:
    """必要な人の表示名だけを引く（組織の一覧は読まない）。

    一般の支援者はほかの支援者の一覧を読まない方針なので、この立場ではこちらを使う。
    """
    if not org_id:
        return {}
    try:
        rows = users.names_by_ids(org_id=org_id, user_ids=list(user_ids))
    except bq.BigQueryUnavailable:
        logger.warning("names_for_ids_failed")
        return {}
    return {str(row.get("user_id")): str(row.get("display_name") or "") for row in rows}
