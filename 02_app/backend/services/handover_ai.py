"""引き継ぎシートの要約と気をつけることを Gemini に作らせる（F3。要件定義書10-6、
開発手順書 Step 9-3）。

- 渡すのは**健康・介護の情報、直近の訪問・見守りの記録と相談、移る先**（要件定義書10-6）。
- **記録にないことを書かせない**（10-6の守らせること）。指示文で固定する。
- 返すのは**案だけ**。保存はしない（作った人が確かめて直してから `handovers.save`）。
- 呼べなかったときは**空の案を返す**（例外を外に出さない）。手で書いて保存できるようにする
  （F2・F8 と同じ考え方。AIが落ちても作業が止まらないようにする）。
- 長すぎる案は、保存できる長さに切る（表に入らない案を画面に出さない）。
- 「考える時間」は 0（F2・F4・F8 と同じ。要件定義書11-1の10秒以内に収めるため）。
- ログに記録の本文・氏名・健康の情報を出さない（要件定義書11-3）。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from backend.services import gemini, handovers, health, survivors, visits

PROMPT_NAME = "handover_draft"
OPERATION = "handover_draft"

# 「考える時間」を使わない（0）。F2・F4・F8 と同じ。
THINKING_BUDGET = 0

# Gemini を呼べなかったときに画面へ出す案内。
AI_UNAVAILABLE_NOTICE = (
    "AIで要約を作れませんでした。要約と気をつけることを、ご自身で書いて保存してください"
)

logger = logging.getLogger(__name__)


class HandoverDraft(BaseModel):
    """引き継ぎシートの案（要約と気をつけること。要件定義書10-6）。"""

    summary: str = Field(default="", description="直近の記録と相談の要約")
    cautions: str = Field(default="", description="移る先で気をつけること")


def _consultations_for_ai(rows: list[dict[str, Any]] | None) -> str:
    """直近の相談を文章にする（`consultations.recent_for_ai` が作った形を読む）。"""
    if not rows:
        return "（相談はありません）"
    lines: list[str] = []
    for row in rows[: handovers.RECENT_CONSULTATIONS_FOR_AI]:
        lines.append(
            f"- 種類：{row.get('category') or '記載なし'}／{row.get('body') or '記載なし'}"
        )
    return "\n".join(lines)


def build_contents(
    *,
    survivor: dict[str, Any],
    health_row: dict[str, Any] | None,
    recent_visits: list[dict[str, Any]] | None,
    recent_consultations: list[dict[str, Any]] | None,
    destination: str,
) -> str:
    """Gemini に渡す本文を組み立てる（要件定義書10-6が渡すものと同じ）。"""
    location_type = survivor.get("location_type") or ""
    return "\n".join(
        [
            "## 移る先",
            destination or "記載なし",
            "",
            "## 移る方の情報",
            f"- 年齢：{survivor.get('age') if survivor.get('age') is not None else '記載なし'}",
            f"- いまの段階：{survivors.STAGES.get(survivor.get('stage') or '', '記載なし')}",
            f"- いまの居場所：{survivors.LOCATION_TYPES.get(location_type, '記載なし')}"
            f"（{survivor.get('location_detail') or '記載なし'}）",
            "",
            "## 健康・介護の情報（F3）",
            health.summary_for_ai(health_row),
            "",
            "## 直近の訪問・見守りの記録",
            visits.recent_items_for_ai(
                recent_visits or [], limit=handovers.RECENT_VISITS_FOR_AI
            ),
            "",
            "## 直近の相談",
            _consultations_for_ai(recent_consultations),
        ]
    )


def draft(
    *,
    survivor: dict[str, Any],
    health_row: dict[str, Any] | None,
    recent_visits: list[dict[str, Any]] | None,
    recent_consultations: list[dict[str, Any]] | None,
    destination: str,
) -> dict[str, Any]:
    """要約と気をつけることの案を返す（保存はしない。**例外を外に出さない**）。"""
    try:
        answer = gemini.generate_structured(
            schema=HandoverDraft,
            contents=build_contents(
                survivor=survivor,
                health_row=health_row,
                recent_visits=recent_visits,
                recent_consultations=recent_consultations,
                destination=destination,
            ),
            system_instruction=gemini.load_prompt(PROMPT_NAME),
            operation=OPERATION,
            thinking_budget=THINKING_BUDGET,
        )
    except gemini.GeminiUnavailable as exc:
        logger.warning("handover_draft_unavailable: %s", type(exc).__name__)
        return {
            "summary": "",
            "cautions": "",
            "ai_used": False,
            "notice": AI_UNAVAILABLE_NOTICE,
        }

    return {
        "summary": (answer.summary or "").strip()[: handovers.SUMMARY_MAX_LENGTH],
        "cautions": (answer.cautions or "").strip()[: handovers.CAUTIONS_MAX_LENGTH],
        "ai_used": True,
        "notice": None,
    }
