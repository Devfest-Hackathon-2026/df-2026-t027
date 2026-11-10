"""受けられそうな支援を Gemini に出させる（F6。要件定義書F6・10-6、開発手順書 Step 10-2）。

- **渡すのは `stg_programs` に登録された制度の一覧だけ。** 制度そのものをAIに作らせない。
- **返ったIDが、渡した一覧にあるかを必ず確かめる**（要件定義書10-6・1330行）。無いIDは
  黙って落とし、落とした件数を返す（画面とログで分かるようにする）。
- 申請先・必要な書類・期限の決まり・出典は**制度の情報（正本）から返す**。AIに書かせない。
- 期限の日付だけはAIが出した値を受け取るが、`supports.clean_deadline` で範囲を確かめる
  （読み取れない値・遠すぎる値は落として「期限なし」にする）。
- 呼べなかったときは**候補なしで返す**（例外を外に出さない）。専門職と本人は、制度の一覧から
  自分で選んで記録できる。
- 「考える時間」は 0（F2・F4・F5・F8 と同じ。要件定義書11-1の10秒以内に収めるため）。
- ログに氏名・住宅の被害の程度・制度の名前を出さない（要件定義書11-3）。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from pydantic import BaseModel, Field

from backend.services import consultations, gemini, supports, survivors

PROMPT_NAME = "support_candidates"
OPERATION = "support_candidates"

# 「考える時間」を使わない（0）。ほかのAIの呼び出しと同じ。
THINKING_BUDGET = 0

# Gemini を呼べなかったときに画面へ出す案内。
AI_UNAVAILABLE_NOTICE = (
    "AIで受けられそうな支援を出せませんでした。下の制度の一覧から選んで記録できます"
)

# 制度が1件も登録されていないときに画面へ出す案内。
NO_PROGRAM_NOTICE = (
    "支援制度がまだ登録されていません。制度の登録（初期データ）を確かめてください"
)

# 調べるときに渡す相談（使いやすさの改修の計画.md 6-3）。直近90日のうち新しい5件まで。
CONSULTATION_LIMIT = 5
CONSULTATION_DAYS = 90

logger = logging.getLogger(__name__)


def recent_consultations(
    rows: list[dict[str, Any]], *, now: dt.datetime | None = None
) -> list[dict[str, Any]]:
    """その人の相談（新しい順）から、渡すものを選ぶ（直近90日・5件まで）。

    渡す形は F4 と同じ `consultations.recent_for_ai`（相談の種類と、整えた項目）。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=CONSULTATION_DAYS)
    recent = []
    for row in rows:
        consulted_at = row.get("consulted_at")
        if isinstance(consulted_at, dt.datetime):
            if consulted_at.tzinfo is None:
                consulted_at = consulted_at.replace(tzinfo=dt.timezone.utc)
            if consulted_at < since:
                continue
        recent.append(row)
    return consultations.recent_for_ai(recent, limit=CONSULTATION_LIMIT)


def _consultation_line(item: dict[str, Any]) -> str:
    consulted_at = item.get("consulted_at")
    when = consulted_at.date().isoformat() if isinstance(consulted_at, dt.datetime) else "日付不明"
    return (
        f"- 相談のID：{item.get('consultation_id')}／送った日：{when}／"
        f"種類：{item.get('category')}／内容：{item.get('body')}"
    )


class SupportCandidate(BaseModel):
    """候補1件（渡した一覧の制度のIDと、その理由）。"""

    program_id: str = Field(description="渡した一覧にある制度のID（作り出さない）")
    reason: str = Field(default="", description="受けられそうな理由（渡された事実だけで書く）")
    deadline: str = Field(
        default="", description="申請の期限（YYYY-MM-DD。決まりから日付にできないときは空）"
    )
    consultation_ids: list[str] = Field(
        default_factory=list,
        description="当てはまると考えた根拠の相談のID（渡した相談のIDだけ。無ければ空）",
    )


class SupportCandidates(BaseModel):
    """受けられそうな支援（渡した制度の中から選ぶ）。"""

    candidates: list[SupportCandidate] = Field(
        default_factory=list,
        description="受けられそうな制度（対象になりそうな制度が無ければ空）",
    )


def build_contents(
    *,
    survivor: dict[str, Any],
    housing_damage: str,
    rebuild_intent: str,
    household_size: Any,
    programs: list[dict[str, Any]],
    consultation_items: list[dict[str, Any]] | None = None,
) -> str:
    """Gemini に渡す本文を組み立てる（要件定義書10-6が渡すものと同じ）。

    渡すのは「住宅の被害の程度・世帯・再建の意向」「直近の相談」「制度の一覧」。
    相談は `recent_consultations` が選んだもの（使いやすさの改修の計画.md 6-3）。
    健康・介護の情報は渡さない（F6 の判断に要らない。要配慮個人情報を広げない）。
    """
    stage = survivors.STAGES.get(str(survivor.get("stage") or ""), "記載なし")
    size = household_size if household_size is not None else survivor.get("household_size")
    lines = [
        "## 被災者の状況",
        f"- 住宅の被害の程度：{supports.HOUSING_DAMAGES.get(housing_damage, '記載なし')}",
        f"- 世帯の人数：{size if size is not None else '記載なし'}",
        f"- 再建の方法の意向：{supports.REBUILD_INTENTS.get(rebuild_intent, '記載なし')}",
        f"- いまの段階：{stage}",
        "",
        "## これまでの相談（新しい順）",
        *([_consultation_line(item) for item in consultation_items or []] or ["- 相談はありません"]),
        "",
        "## 登録されている支援制度の一覧（この中のIDだけを返すこと）",
        *[supports.summary_for_ai(row) for row in programs],
    ]
    return "\n".join(lines)


def _to_candidates(
    answer: SupportCandidates,
    *,
    programs: list[dict[str, Any]],
    consultation_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """返ったIDを渡した一覧と照らし、候補の形にそろえる（要件定義書10-6）。

    根拠の相談のIDも、渡した相談のIDと照らし、無いものは落とす。
    """
    known_consultations = consultation_ids or set()
    by_id = {str(row.get("program_id")): row for row in programs}
    candidates: list[dict[str, Any]] = []
    dropped = 0
    for item in answer.candidates:
        program_id = (item.program_id or "").strip()
        row = by_id.get(program_id)
        if row is None:
            # 渡していない制度を作り出した。候補にしない
            dropped += 1
            continue
        if any(existing["program_id"] == program_id for existing in candidates):
            continue
        reason = (item.reason or "").strip()
        deadline = supports.clean_deadline(item.deadline)
        based_on = []
        for cid in item.consultation_ids:
            cid = (cid or "").strip()
            if cid in known_consultations and cid not in based_on:
                based_on.append(cid)
        candidates.append(
            {
                **supports.program_payload(row),
                "reason": reason[: supports.REASON_MAX_LENGTH],
                # 期限は日付にできたときだけ入れる（作り出した日付は落とす）
                "deadline": deadline.isoformat() if deadline else None,
                # どの相談から当てはまると考えたか（渡した相談のIDだけ）
                "consultation_ids": based_on,
                "status": supports.STATUS_NOT_APPLIED,
                "status_label": supports.STATUSES[supports.STATUS_NOT_APPLIED],
                "next_action": supports.NEXT_ACTIONS[supports.STATUS_NOT_APPLIED],
                "disclaimer": supports.DISCLAIMER,
            }
        )
    return candidates, dropped


def suggest(
    *,
    survivor: dict[str, Any],
    housing_damage: str,
    rebuild_intent: str,
    household_size: Any = None,
    programs: list[dict[str, Any]],
    consultation_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """受けられそうな支援を理由つきで返す（**例外を外に出さない**）。

    `consultation_items` は `recent_consultations` が選んだ直近の相談（使いやすさの改修の計画.md 6-3）。

    制度が1件も無いときは Gemini を呼ばない（選べるものが無い）。
    """
    if not programs:
        return {
            "candidates": [],
            "ai_used": False,
            "dropped_count": 0,
            "notice": NO_PROGRAM_NOTICE,
        }

    try:
        answer = gemini.generate_structured(
            schema=SupportCandidates,
            contents=build_contents(
                survivor=survivor,
                housing_damage=housing_damage,
                rebuild_intent=rebuild_intent,
                household_size=household_size,
                programs=programs,
                consultation_items=consultation_items or [],
            ),
            system_instruction=gemini.load_prompt(PROMPT_NAME),
            operation=OPERATION,
            thinking_budget=THINKING_BUDGET,
        )
    except gemini.GeminiUnavailable as exc:
        logger.warning("support_candidates_unavailable: %s", type(exc).__name__)
        return {
            "candidates": [],
            "ai_used": False,
            "dropped_count": 0,
            "notice": AI_UNAVAILABLE_NOTICE,
        }

    candidates, dropped = _to_candidates(
        answer,
        programs=programs,
        consultation_ids={str(item.get("consultation_id")) for item in consultation_items or []},
    )
    if dropped:
        # 制度の名前は出さない（件数だけ）
        logger.warning("support_candidates_dropped: count=%d", dropped)
    return {
        "candidates": candidates,
        "ai_used": True,
        "dropped_count": dropped,
        "notice": None,
    }
