"""本人からの相談を Gemini で整え、急ぎの度合いを判断する（F8。要件定義書10-6、開発手順書 Step 8）。

**本人は相談をそのまま送る**（画面の改修の計画.md 6章。使いやすさの改修の計画 12-2 の #3）。
送ったときに、整える処理と急ぎの度合いの判断を **1回の問い合わせ（`triage`）** でまとめて行う。

- 本人には、整えた9つの項目を見せない。本人にとって、AIが分けた項目を確かめて直すのは
  難しく、言い換えで意味が変わっても気づきにくいため。整えた項目は、専門職が読むときの
  **案**として、本人の文章と並べて出す（本人の文章が正本）。
- 前は「整える（送る前）」と「急ぎの度合い（送るとき）」の2回に分けていた。1つにまとめて、
  1回の相談で呼ぶ回数を 3回 → 2回（まとめた1回と F4 の悪化の兆し）に減らした。
- 呼べなかったときは**既定の度合いに落として返す**（本人が書いた相談を捨てないため。送信は止めない）。
- 「考える時間」は 0（F2・F4 と同じ。要件定義書11-1の10秒以内に収めるため）。
- ログに相談の本文・氏名・健康の情報を出さない（要件定義書11-3）。

`structure`（送る前に整える処理）は、古い画面を開いたままの人のために、1回のデプロイの間だけ
残す（画面の改修の計画.md 9章の要確認6）。新しい画面は使わない。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from backend.services import consultations, gemini, health, survivors, visits

PROMPT_NAME = "consultation_structure"
OPERATION = "consultation_structure"
TRIAGE_PROMPT_NAME = "consultation_triage"
TRIAGE_OPERATION = "consultation_triage"

# 「考える時間」を使わない（0）。F2 の整える処理と同じ（実測 6.5〜8.2秒 → 1.2〜2.3秒）。
THINKING_BUDGET = 0

logger = logging.getLogger(__name__)


class ConsultationStructure(BaseModel):
    """整えた相談（種類・F2と同じ9つの項目・急を要するかどうか。要件定義書F8）。"""

    category: str = Field(description="相談の種類（英字のコード）")
    location: str = Field(description="今の居場所")
    condition: str = Field(description="体調")
    meal: str = Field(description="食事")
    sleep: str = Field(description="睡眠")
    medication: str = Field(description="服薬の状況")
    environment: str = Field(description="生活の環境")
    concerns: str = Field(description="困りごと")
    change: str = Field(description="前回からの変化")
    next_action: str = Field(description="本人が望んでいること・してほしいこと")
    emergency: bool = Field(description="命に関わる・急を要する様子があれば true（迷ったら true）")
    emergency_reason: str = Field(default="", description="そう判断した理由（文章の記述を引く）")


class ConsultationTriage(ConsultationStructure):
    """整えた相談と、急ぎの度合い・根拠（1回の問い合わせで返させる。画面の改修の計画.md 6章）。"""

    level: str = Field(description="急ぎの度合い（high・medium・low）")
    evidence: str = Field(default="", description="度合いの根拠（相談の記述から引く）")


# --- 1. 相談を整える ---


def build_contents(
    *,
    raw_text: str,
    survivor: dict[str, Any],
    health_row: dict[str, Any] | None,
) -> str:
    """Gemini に渡す本文を組み立てる（要件定義書10-6が渡すものと同じ）。"""
    location_type = survivor.get("location_type") or ""
    location_label = survivors.LOCATION_TYPES.get(location_type, "記載なし")
    stage_label = survivors.STAGES.get(survivor.get("stage") or "", "記載なし")
    return "\n".join(
        [
            "## ご本人が書いた文章",
            raw_text,
            "",
            "## いま登録されている居場所",
            f"- 種類：{location_label}（コード：{location_type or '記載なし'}）",
            f"- 場所の名前：{survivor.get('location_detail') or '記載なし'}",
            f"- 段階：{stage_label}",
            "",
            "## いま登録されている健康・介護の情報",
            health.summary_for_ai(health_row),
        ]
    )


def to_payload(structured: ConsultationStructure) -> dict[str, Any]:
    """Gemini の返事を、画面に返す形（種類＋9つの項目＋急を要するか）にそろえる。"""
    category = consultations.normalize_category(structured.category)
    items = consultations.clean_items(
        {name: getattr(structured, name) for name in visits.VISIT_FIELDS}
    )
    reason = (structured.emergency_reason or "").strip()
    return {
        "category": category,
        "category_label": consultations.CATEGORIES[category],
        "items": items,
        "emergency_flag": bool(structured.emergency),
        "emergency_reason": reason or None,
    }


def structure(
    *,
    raw_text: str,
    survivor: dict[str, Any],
    health_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """本人の文章を、種類と9つの項目に整えた案にする（保存はしない）。

    Raises:
        gemini.GeminiUnavailable: 呼べなかったとき。呼び出し側が手入力の欄を出す。
    """
    structured = gemini.generate_structured(
        schema=ConsultationStructure,
        contents=build_contents(raw_text=raw_text, survivor=survivor, health_row=health_row),
        system_instruction=gemini.load_prompt(PROMPT_NAME),
        operation=OPERATION,
        thinking_budget=THINKING_BUDGET,
    )
    return to_payload(structured)


def empty_payload() -> dict[str, Any]:
    """Gemini を呼べなかったときに返す形（9つの項目をすべて「記載なし」にする）。

    急を要するかどうかは**立てない**。AIが見ていない以上、画面の案内（`EMERGENCY_GUIDE`）で
    本人に判断してもらう（勝手に「要する」にすると、119番の案内が常に出て意味が薄れる）。
    """
    return {
        "category": consultations.DEFAULT_CATEGORY,
        "category_label": consultations.CATEGORIES[consultations.DEFAULT_CATEGORY],
        "items": consultations.clean_items({}),
        "emergency_flag": False,
        "emergency_reason": None,
    }


# --- 2. 送ったときに整えて、急ぎの度合いを判断する（1回の問い合わせ） ---


def build_triage_contents(
    *,
    raw_text: str,
    chosen_category: str | None,
    survivor: dict[str, Any],
    health_row: dict[str, Any] | None,
    recent_visits: list[dict[str, Any]] | None = None,
) -> str:
    """Gemini に渡す本文（本人の文章・選んだ種類・居場所・健康の情報・直近の記録）。"""
    chosen = (
        consultations.CATEGORIES[chosen_category]
        if chosen_category in consultations.CATEGORIES
        else "選んでいない"
    )
    lines = [
        build_contents(raw_text=raw_text, survivor=survivor, health_row=health_row),
        "",
        "## ご本人が選んだ相談の種類",
        f"- {chosen}" + (f"（コード：{chosen_category}）" if chosen_category in consultations.CATEGORIES else ""),
    ]
    if recent_visits:
        lines += ["", "## 直近の訪問・見守りの記録", visits.recent_items_for_ai(recent_visits)]
    return "\n".join(lines)


def fallback_triage(*, chosen_category: str | None) -> dict[str, Any]:
    """Gemini を呼べなかったときの形。9つの項目は「記載なし」、度合いは既定の値にする。

    急を要するかどうかは**立てない**（AIが見ていない。送る前の画面の 119番の案内で本人に判断してもらう）。
    """
    category = consultations.normalize_category(chosen_category)
    return {
        "category": category,
        "items": consultations.clean_items({}),
        "emergency_flag": False,
        "emergency_reason": None,
        "urgency": consultations.fallback_urgency(emergency_flag=False),
        "urgency_evidence": consultations.FALLBACK_URGENCY_EVIDENCE,
        "ai_used": False,
    }


def triage(
    *,
    raw_text: str,
    chosen_category: str | None,
    survivor: dict[str, Any],
    health_row: dict[str, Any] | None,
    recent_visits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """本人の文章を整え、急ぎの度合いと根拠を付ける（**例外を外に出さない**）。

    本人が種類を選んでいれば、その種類を使う（AIの案より本人の選択を優先する）。
    急を要するときは、度合いを必ず high にする（迷ったら高いほう。10-6の守らせること）。
    """
    chosen = chosen_category if chosen_category in consultations.CATEGORIES else None
    try:
        answer = gemini.generate_structured(
            schema=ConsultationTriage,
            contents=build_triage_contents(
                raw_text=raw_text,
                chosen_category=chosen,
                survivor=survivor,
                health_row=health_row,
                recent_visits=recent_visits,
            ),
            system_instruction=gemini.load_prompt(TRIAGE_PROMPT_NAME),
            operation=TRIAGE_OPERATION,
            thinking_budget=THINKING_BUDGET,
        )
        urgency = consultations.validate_urgency(answer.level)
    except gemini.GeminiUnavailable as exc:
        logger.warning("consultation_triage_unavailable: %s", type(exc).__name__)
        return fallback_triage(chosen_category=chosen)
    except consultations.ConsultationValidationError:
        logger.warning("consultation_triage_unreadable")
        return fallback_triage(chosen_category=chosen)

    structured = to_payload(answer)
    emergency = structured["emergency_flag"]
    return {
        "category": chosen or structured["category"],
        "items": structured["items"],
        "emergency_flag": emergency,
        "emergency_reason": structured["emergency_reason"],
        "urgency": consultations.EMERGENCY_URGENCY if emergency else urgency,
        "urgency_evidence": consultations.clean_evidence(answer.evidence),
        "ai_used": True,
    }
