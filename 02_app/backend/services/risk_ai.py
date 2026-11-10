"""悪化の兆しを Gemini で判断する（F4。要件定義書F4・10-6の2行目、開発手順書 Step 7-1）。

- **画面から呼ぶ処理ではない。** F2 の記録と F8 の相談を保存した直後に、サーバーの中で
  続けて走らせる（要件定義書10-5の注）。本人が相談を送ったときにも、本人には判断を返さない。
- 渡すものは、**直近の記録と相談を合わせて最大5件**と、F3の健康・介護の情報（要件定義書10-6）。
- 返させるものは、度合い（高・中・低）・根拠（記録のどの記述か）・次に必要な対応。
  根拠には、渡した記録・相談のIDを付けさせる（`known_ids` に無いIDは落とす）。
- **診断の言葉を使わせない。** 守らせることは `prompts/risk_assessment.md` に書く（10-6）。
- 呼べなかったときは**何も保存しない**。前の判断がそのまま残り、一覧の順番も変わらない
  （`assess_and_save` が例外を外に出さないので、保存した記録そのものは失敗にならない）。
- ログに記録の本文・氏名・健康の情報を出さない（要件定義書10-2の6・11-3）。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from backend.services import assessments, gemini, health, survivors, visits

logger = logging.getLogger(__name__)

PROMPT_NAME = "risk_assessment"
OPERATION = "risk_assessment"

# 渡す記録と相談の合計の件数（要件定義書10-6）。
RECENT_LIMIT = 5

# 「考える時間」を使わない（0）。F2 の整える処理と同じ理由（実測で 6.5〜8.2秒 → 1.2〜2.3秒）。
# この処理は記録の保存と相談の送信の**応答を返す前に**走るので、遅いとそのまま
# 保存の待ち時間になる（要件定義書11-1の10秒の目安に含まれる）。
# 度合いが雑になるときは、この行を None にすれば考えさせられる。
THINKING_BUDGET = 0


class EvidenceItem(BaseModel):
    """根拠の1つ（引いた記述と、その記録・相談のID。要件定義書F4）。"""

    quote: str = Field(description="記録・相談から引いた記述（言い換えずにそのまま）")
    source_kind: str = Field(default="", description="visit（記録）か consultation（相談）")
    source_id: str = Field(default="", description="渡された記録・相談のID")


class RiskAssessment(BaseModel):
    """悪化の兆しの判断（要件定義書10-6の「返させるもの」）。"""

    level: str = Field(description="度合い。high・medium・low のどれか1つ")
    evidence: list[EvidenceItem] = Field(
        default_factory=list, description="根拠（多くても3つ）"
    )
    next_action: str = Field(default="", description="次に必要な対応（1つだけ）")


# --- Gemini に渡す本文 ---


def _when(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if isinstance(value, dt.datetime) else "日付なし"


def _visit_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        parts = [
            f"{label}：{row.get(name) or visits.NOT_RECORDED}"
            for name, label in visits.VISIT_FIELDS.items()
        ]
        lines.append(
            f"- 種類：{assessments.KIND_VISIT}／ID：{row.get('visit_id')}／"
            f"日付：{_when(row.get('visited_at'))}／" + "／".join(parts)
        )
    return lines


def _consultation_lines(rows: list[dict[str, Any]]) -> list[str]:
    """本人からの相談（F8。Step 8 で渡されるようになる）。

    Step 7 の時点では相談の表に行が無いので、呼ぶ側が空のまま渡す。項目の名前は
    要件定義書7-3-1 の `raw_consultations`（`consultation_id`・`consulted_at`・
    `category`・`body`・`urgency`）にそろえてある。
    """
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"- 種類：{assessments.KIND_CONSULTATION}／ID：{row.get('consultation_id')}／"
            f"日付：{_when(row.get('consulted_at'))}／"
            f"種別：{row.get('category') or '記載なし'}／"
            f"急ぎの度合い：{row.get('urgency') or '記載なし'}／"
            f"内容：{row.get('body') or '記載なし'}"
        )
    return lines


def build_contents(
    *,
    survivor: dict[str, Any],
    health_row: dict[str, Any] | None,
    recent_visits: list[dict[str, Any]] | None = None,
    recent_consultations: list[dict[str, Any]] | None = None,
) -> str:
    """Gemini に渡す本文を組み立てる（要件定義書10-6が渡すものと同じ）。"""
    stage_label = survivors.STAGES.get(survivor.get("stage") or "", "記載なし")
    location_label = survivors.LOCATION_TYPES.get(survivor.get("location_type") or "", "記載なし")
    lines = _visit_lines(recent_visits or []) + _consultation_lines(recent_consultations or [])
    parts = [
        "## いまの段階と居場所",
        f"- 段階：{stage_label}",
        f"- 居場所：{location_label}",
        "",
        "## いま登録されている健康・介護の情報",
        health.summary_for_ai(health_row),
        "",
        "## 直近の記録と相談（新しいものから）",
        "\n".join(lines) if lines else "（まだ記録も相談もありません）",
    ]
    return "\n".join(parts)


def known_ids(
    recent_visits: list[dict[str, Any]] | None,
    recent_consultations: list[dict[str, Any]] | None = None,
) -> set[str]:
    """渡した記録・相談のID（根拠に作ったIDが入らないようにするため）。"""
    ids = {str(row.get("visit_id")) for row in (recent_visits or []) if row.get("visit_id")}
    ids |= {
        str(row.get("consultation_id"))
        for row in (recent_consultations or [])
        if row.get("consultation_id")
    }
    return ids


# --- 判断 ---


def to_payload(
    structured: RiskAssessment, *, allowed_ids: set[str] | None = None
) -> dict[str, Any]:
    """Gemini の返事を、保存できる形にそろえる。

    度合いが高・中・低のどれでもなければ `AssessmentValidationError` が上がる
    （決まっていない値を保存すると、一覧の並べ替えで黙って最下位になる）。
    """
    return {
        "level": assessments.validate_level(structured.level),
        "evidence": assessments.clean_evidence(
            [item.model_dump() for item in structured.evidence], known_ids=allowed_ids
        ),
        "next_action": assessments.clean_next_action(structured.next_action),
    }


def assess(
    *,
    survivor: dict[str, Any],
    health_row: dict[str, Any] | None,
    recent_visits: list[dict[str, Any]] | None = None,
    recent_consultations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """悪化の兆しの度合い・根拠・次に必要な対応を返す（保存はしない）。

    Raises:
        gemini.GeminiUnavailable: 呼べなかったとき。
        assessments.AssessmentValidationError: 度合いを読み取れなかったとき。
    """
    structured = gemini.generate_structured(
        schema=RiskAssessment,
        contents=build_contents(
            survivor=survivor,
            health_row=health_row,
            recent_visits=recent_visits,
            recent_consultations=recent_consultations,
        ),
        system_instruction=gemini.load_prompt(PROMPT_NAME),
        operation=OPERATION,
        thinking_budget=THINKING_BUDGET,
    )
    return to_payload(
        structured, allowed_ids=known_ids(recent_visits, recent_consultations)
    )


def assess_and_save(
    *,
    survivor: dict[str, Any],
    created_by: str,
    created_role: str,
    trigger_id: str | None = None,
    recent_consultations: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """記録・相談を保存した直後に、サーバーの中で判断して保存する（要件定義書10-5の注）。

    **保存の後、応答を返す前に**呼ばれる。例外を外に出さない（判断ができなくても、
    保存した記録そのものは成功のままにする）。失敗したときは何も保存せず None を返す。
    前の判断がそのまま残るので、一覧の順番は変わらない。

    同期の関数にしてある。呼び出し側が `asyncio.to_thread` で別のスレッドで走らせるため、
    ここで `asyncio` を使う必要がない（読みは2回・書きは1回で、順に行う）。
    """
    survivor_id = str(survivor.get("survivor_id") or "")
    org_id = str(survivor.get("org_id") or "")
    if not survivor_id or not org_id:
        logger.warning("risk_assessment_skipped: 記録が決まっていません")
        return None

    started = time.monotonic()
    try:
        recent = visits.list_visits(survivor_id=survivor_id, org_id=org_id, limit=RECENT_LIMIT)
        health_row = health.fetch_latest(survivor_id=survivor_id, org_id=org_id)
        judged = assess(
            survivor=survivor,
            health_row=health_row,
            recent_visits=recent,
            recent_consultations=recent_consultations,
        )
        saved = assessments.save(
            assessments.build_row(
                survivor_id=survivor_id,
                org_id=org_id,
                created_by=created_by,
                created_role=created_role,
                level=judged["level"],
                evidence=judged["evidence"],
                next_action=judged["next_action"],
                source=assessments.SOURCE_AI,
                trigger_id=trigger_id,
            )
        )
    except Exception as exc:  # noqa: BLE001  保存を失敗に見せないため、種類を問わず外に出さない
        logger.warning(
            "risk_assessment_failed: error=%s elapsed=%.1fs",
            type(exc).__name__,
            time.monotonic() - started,
        )
        return None

    logger.info(
        "risk_assessment_saved: level=%s evidence=%d elapsed=%.1fs",
        saved.get("level"),
        len(judged["evidence"]),
        time.monotonic() - started,
    )
    return saved
