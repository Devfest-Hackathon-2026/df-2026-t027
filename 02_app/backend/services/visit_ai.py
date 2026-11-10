"""訪問・見守りの記録を Gemini で整える（F2。要件定義書10-6の1行目、開発手順書 Step 6-1）。

- 構造化出力で9つの項目と、居場所・健康の更新の案を受け取る。
- **保存はしない。** 返すのは案だけで、人が確かめたあとの別の要求で保存する（要件定義書10-5）。
- 指示文は `prompts/visit_structure.md`（処理ごとにファイルを分ける。要件定義書10-6）。
- 呼べなかったときは `gemini.GeminiUnavailable` がそのまま上がる。呼び出し側は
  **手入力の欄を出して続けられるようにする**（要件定義書F2・10-6）。
- ログに文章・氏名・健康の情報を出さない（要件定義書11-3）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from backend.services import gemini, health, survivors, visits

PROMPT_NAME = "visit_structure"
OPERATION = "visit_structure"

# 「考える時間」を使わない（0）。項目に振り分けるだけの処理なので、考えさせても答えは変わらず、
# **速さだけが変わる**（実測：6.5〜8.2秒 → 1.2〜2.3秒。居場所・健康の案の精度も同じだった）。
# 要件定義書11-1 の「10秒以内」を、BigQuery の読み書きと合わせて満たすために必要。
THINKING_BUDGET = 0

# Gemini が居場所を日本語の名前で返したときに、英字のコードへ戻すための対応。
_LABEL_TO_LOCATION_TYPE = {label: value for value, label in survivors.LOCATION_TYPES.items()}


class LocationProposal(BaseModel):
    """居場所の更新の案（要件定義書F2・10-6）。"""

    change_suggested: bool = Field(description="居場所が変わったことが文章に書かれていれば true")
    location_type: str = Field(default="", description="居場所の種類（英字のコード）")
    location_detail: str = Field(default="", description="場所の名前（避難所名・地区など）")
    reason: str = Field(default="", description="そう判断した理由（文章の記述を引く）")


class HealthProposal(BaseModel):
    """健康・介護の更新の案（変わった項目だけを入れる。要件定義書F2・F3）。"""

    change_suggested: bool = Field(
        description="健康・介護の情報が変わったことが文章に書かれていれば true"
    )
    reason: str = Field(default="", description="そう判断した理由（文章の記述を引く）")
    chronic_diseases: list[str] = Field(default_factory=list, description="持病")
    medications: list[str] = Field(default_factory=list, description="飲んでいる薬")
    allergies: str = Field(default="", description="アレルギー")
    primary_clinic: str = Field(default="", description="かかりつけの医療機関")
    medical_needs: list[str] = Field(default_factory=list, description="必要な医療")
    care_level: str = Field(default="", description="要介護度")
    assistance_mobility: str = Field(default="", description="移動で必要な介助")
    assistance_meal: str = Field(default="", description="食事で必要な介助")
    assistance_toilet: str = Field(default="", description="排せつで必要な介助")
    dementia_notes: str = Field(default="", description="認知症の有無と配慮すること")
    family_contact: str = Field(default="", description="連絡してほしい家族など")


class VisitStructure(BaseModel):
    """整えた9つの項目と更新の案（要件定義書F2）。書かれていない項目は「記載なし」。"""

    location: str = Field(description="今の居場所")
    condition: str = Field(description="体調")
    meal: str = Field(description="食事")
    sleep: str = Field(description="睡眠")
    medication: str = Field(description="服薬の状況")
    environment: str = Field(description="生活の環境（断水・停電・寒さ・暑さなど）")
    concerns: str = Field(description="困りごと")
    change: str = Field(description="前回からの変化")
    next_action: str = Field(description="次に必要な対応")
    location_proposal: LocationProposal
    health_proposal: HealthProposal


def build_contents(
    *,
    raw_text: str,
    survivor: dict[str, Any],
    health_row: dict[str, Any] | None,
    recent_visits: list[dict[str, Any]] | None = None,
) -> str:
    """Gemini に渡す本文を組み立てる（要件定義書10-6が渡すものと同じ）。"""
    location_type = survivor.get("location_type") or ""
    location_label = survivors.LOCATION_TYPES.get(location_type, "記載なし")
    detail = survivor.get("location_detail")
    stage_label = survivors.STAGES.get(survivor.get("stage") or "", "記載なし")
    parts = [
        "## 支援者が書いた文章",
        raw_text,
        "",
        "## いま登録されている居場所",
        f"- 種類：{location_label}（コード：{location_type or '記載なし'}）",
        f"- 場所の名前：{detail or '記載なし'}",
        f"- 段階：{stage_label}",
        "",
        "## いま登録されている健康・介護の情報",
        health.summary_for_ai(health_row),
    ]
    if recent_visits:
        parts += ["", "## 直近の記録（前回からの変化を見るため）", visits.recent_items_for_ai(recent_visits)]
    return "\n".join(parts)


def _location_type_code(value: str) -> str:
    """英字のコードに直す（日本語の名前で返ってきた場合も受け取る）。"""
    text = (value or "").strip()
    if text in survivors.LOCATION_TYPES:
        return text
    return _LABEL_TO_LOCATION_TYPE.get(text, "")


def to_proposal_payload(structured: VisitStructure) -> dict[str, Any]:
    """Gemini の返事を、画面に返す形（9つの項目＋更新の案）にそろえる。"""
    items = visits.clean_items(
        {name: getattr(structured, name) for name in visits.VISIT_FIELDS}
    )
    location_proposal = visits.normalize_location_proposal(
        {
            "change_suggested": structured.location_proposal.change_suggested,
            "location_type": _location_type_code(structured.location_proposal.location_type),
            "location_detail": structured.location_proposal.location_detail,
            "reason": structured.location_proposal.reason,
        }
    )
    health_source = structured.health_proposal.model_dump()
    health_proposal = visits.normalize_health_proposal(
        {
            "change_suggested": health_source.pop("change_suggested"),
            "reason": health_source.pop("reason"),
            "fields": health_source,
        }
    )
    return {
        "items": items,
        "location_proposal": location_proposal,
        "health_proposal": health_proposal,
    }


def structure(
    *,
    raw_text: str,
    survivor: dict[str, Any],
    health_row: dict[str, Any] | None,
    recent_visits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """文章を9つの項目に整えた案を返す（保存はしない）。

    Raises:
        gemini.GeminiUnavailable: 呼べなかったとき。呼び出し側が手入力の欄を出す。
    """
    structured = gemini.generate_structured(
        schema=VisitStructure,
        contents=build_contents(
            raw_text=raw_text,
            survivor=survivor,
            health_row=health_row,
            recent_visits=recent_visits,
        ),
        system_instruction=gemini.load_prompt(PROMPT_NAME),
        operation=OPERATION,
        thinking_budget=THINKING_BUDGET,
    )
    return to_proposal_payload(structured)


def empty_payload() -> dict[str, Any]:
    """Gemini を呼べなかったときに返す形（9つの項目をすべて「記載なし」にする）。"""
    return {"items": visits.clean_items({}), "location_proposal": None, "health_proposal": None}
