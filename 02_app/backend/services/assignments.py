"""担当の割り当て（F1。要件定義書7-3-1の raw_assignment_events・7-3-2の stg_active_assignments）。

専門職が、一般の支援者を被災者の担当に割り当てる。有効になる条件は2つに分かれる（F1）。

- 本人がアカウントを持っている → **本人が認めるまで担当にしない**（`approved`）。
- 持っていない → 専門職が「口頭で同意を得た」ことを記録してから担当にする（`verbal_consent`）。

外すのは、本人と専門職の両方ができる（`removed`。要件定義書10-5のF1の行）。

### 読みを `raw_assignment_events` から行う理由

`stg_active_assignments` は**有効な担当だけ**を返すビューで、承認待ちの担当が出てこない。
本人が「認める」を押す画面には承認待ちの一覧が要るため、被災者のIDで
`raw_assignment_events` を直接読み、状態（承認待ち・有効・外れた）をここで決める。
7-3-5 の「画面は stg と int のビューから読む」から外れる1点として、開発ログに記録する。
見える範囲の判定そのものは、これまでどおり `int_survivor_list`（`stg_active_assignments`
を含む）で行う。状態の決め方は `stg_active_assignments` と同じにそろえてある
（`removed` より後に承認があれば有効）。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from backend.config import get_settings
from backend.services import bq

logger = logging.getLogger(__name__)

# raw_assignment_events.action の値（要件定義書7-3-1）。
ACTION_ASSIGNED = "assigned"
ACTION_APPROVED = "approved"
ACTION_VERBAL_CONSENT = "verbal_consent"
ACTION_REMOVED = "removed"

# 同意を表す出来事（どちらかがあれば担当として有効になる）。
CONSENT_ACTIONS = (ACTION_APPROVED, ACTION_VERBAL_CONSENT)

# 画面に出す状態。
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"


class AssignmentError(ValueError):
    """担当の割り当ての入力が正しくない。"""


@dataclass(frozen=True)
class CreatedAssignment:
    """作った担当の割り当て。"""

    assignment_id: str
    supporter_user_id: str
    status: str


def _raw_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_raw, table)


def _event(
    *,
    org_id: str,
    survivor_id: str,
    assignment_id: str,
    supporter_user_id: str,
    action: str,
    acted_by: str,
    acted_role: str,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": acted_by,
        "created_role": acted_role,
        "survivor_id": survivor_id,
        "assignment_id": assignment_id,
        "supporter_user_id": supporter_user_id,
        "action": action,
    }


# --- 書き込み ---


def create(
    *,
    survivor_id: str,
    org_id: str,
    supporter_user_id: str,
    created_by: str,
    created_role: str,
    verbal_consent: bool,
) -> CreatedAssignment:
    """担当を割り当てる。口頭の同意を記録したときだけ、その場で有効になる。"""
    if not survivor_id or not org_id:
        raise AssignmentError("被災者の記録が決まっていません")
    if not supporter_user_id or not supporter_user_id.strip():
        raise AssignmentError("担当にする支援者を選んでください")

    supporter = supporter_user_id.strip()
    assignment_id = f"as_{uuid.uuid4().hex}"
    bq.insert(
        "raw_assignment_events",
        _event(
            org_id=org_id,
            survivor_id=survivor_id,
            assignment_id=assignment_id,
            supporter_user_id=supporter,
            action=ACTION_ASSIGNED,
            acted_by=created_by,
            acted_role=created_role,
        ),
    )
    status = STATUS_PENDING
    if verbal_consent:
        bq.insert(
            "raw_assignment_events",
            _event(
                org_id=org_id,
                survivor_id=survivor_id,
                assignment_id=assignment_id,
                supporter_user_id=supporter,
                action=ACTION_VERBAL_CONSENT,
                acted_by=created_by,
                acted_role=created_role,
            ),
        )
        status = STATUS_ACTIVE

    logger.info("assignment_created: status=%s consent=%s", status, verbal_consent)
    return CreatedAssignment(
        assignment_id=assignment_id, supporter_user_id=supporter, status=status
    )


def _record(
    *,
    action: str,
    assignment_id: str,
    survivor_id: str,
    org_id: str,
    supporter_user_id: str,
    acted_by: str,
    acted_role: str,
) -> None:
    if not assignment_id:
        raise AssignmentError("担当の割り当てが決まっていません")
    bq.insert(
        "raw_assignment_events",
        _event(
            org_id=org_id,
            survivor_id=survivor_id,
            assignment_id=assignment_id,
            supporter_user_id=supporter_user_id,
            action=action,
            acted_by=acted_by,
            acted_role=acted_role,
        ),
    )
    logger.info("assignment_action: action=%s", action)


def approve(
    *,
    assignment_id: str,
    survivor_id: str,
    org_id: str,
    supporter_user_id: str,
    acted_by: str,
    acted_role: str,
) -> None:
    """本人が担当を認める。"""
    _record(
        action=ACTION_APPROVED,
        assignment_id=assignment_id,
        survivor_id=survivor_id,
        org_id=org_id,
        supporter_user_id=supporter_user_id,
        acted_by=acted_by,
        acted_role=acted_role,
    )


def remove(
    *,
    assignment_id: str,
    survivor_id: str,
    org_id: str,
    supporter_user_id: str,
    acted_by: str,
    acted_role: str,
) -> None:
    """担当を外す（本人・専門職のどちらもできる）。"""
    _record(
        action=ACTION_REMOVED,
        assignment_id=assignment_id,
        survivor_id=survivor_id,
        org_id=org_id,
        supporter_user_id=supporter_user_id,
        acted_by=acted_by,
        acted_role=acted_role,
    )


# --- 読み ---


def list_for_survivor(*, survivor_id: str, org_id: str) -> list[dict[str, Any]]:
    """その被災者の担当を、状態つきで返す（承認待ちも含む）。

    並び順は、有効 → 承認待ち → 外れた の順にし、同じ状態では新しいものを先に出す。
    """
    sql = (
        "SELECT assignment_id, supporter_user_id, action, created_at, created_by, created_role\n"
        f"FROM {_raw_table('raw_assignment_events')}\n"
        "WHERE org_id = @org_id AND survivor_id = @survivor_id\n"
        "ORDER BY created_at, event_id"
    )
    events = bq.query(sql, {"survivor_id": survivor_id, "org_id": org_id})
    return _fold(events)


_STATUS_ORDER = {STATUS_ACTIVE: 0, STATUS_PENDING: 1, STATUS_REMOVED: 2}


def _fold(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """出来事の並びを、割り当てごとの状態にまとめる（`stg_active_assignments` と同じ判定）。"""
    folded: dict[str, dict[str, Any]] = {}
    for item in events:
        assignment_id = item.get("assignment_id")
        if not assignment_id:
            continue
        state = folded.setdefault(
            assignment_id,
            {
                "assignment_id": assignment_id,
                "supporter_user_id": item.get("supporter_user_id"),
                "assigned_at": None,
                "assigned_by": None,
                "consented_at": None,
                "consent_type": None,
                "removed_at": None,
            },
        )
        if item.get("supporter_user_id"):
            state["supporter_user_id"] = item.get("supporter_user_id")
        action = item.get("action")
        created_at = item.get("created_at")
        if action == ACTION_ASSIGNED:
            if state["assigned_at"] is None:
                state["assigned_at"] = created_at
                state["assigned_by"] = item.get("created_by")
        elif action in CONSENT_ACTIONS:
            state["consented_at"] = created_at
            state["consent_type"] = action
        elif action == ACTION_REMOVED:
            state["removed_at"] = created_at

    rows: list[dict[str, Any]] = []
    for state in folded.values():
        state["status"] = _status(state)
        rows.append(state)
    rows.sort(
        key=lambda row: (
            _STATUS_ORDER.get(row["status"], 9),
            # 新しいものを先に（日時が無い行は後ろ）
            -(row["assigned_at"].timestamp() if row.get("assigned_at") else 0),
        )
    )
    return rows


def _status(state: dict[str, Any]) -> str:
    consented_at = state.get("consented_at")
    removed_at = state.get("removed_at")
    if removed_at is not None and (consented_at is None or removed_at > consented_at):
        return STATUS_REMOVED
    if consented_at is not None:
        return STATUS_ACTIVE
    return STATUS_PENDING


def find(rows: list[dict[str, Any]], assignment_id: str) -> dict[str, Any] | None:
    """まとめた一覧から、割り当てを1つ探す。"""
    for row in rows:
        if row.get("assignment_id") == assignment_id:
            return row
    return None
