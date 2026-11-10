"""悪化の兆しの判断（F4。要件定義書F4・7-3-1の raw_assessments・7-3-2、開発手順書 Step 7-1）。

- **raw は追記だけ。** 度合いを変えるときも書き換えず、新しい1行を足す
  （`stg_assessment_latest` が `survivor_id` ごとの最新の1件を返す。7-3-2）。
- **医療の診断ではない。** 根拠は記録・相談の記述から引き、診断の言葉は使わない（要件定義書F4）。
  この決まりは Gemini の指示文（`prompts/risk_assessment.md`）と画面の両方に置く。
- **本人には度合いと根拠を返さない**（要件定義書F4の最後・10-3）。返す形を組み立てる
  `survivors.detail_payload` が、本人のときはこの節ごと入れない。
- 根拠（`evidence`）は JSON の列だが、**辞書で入れる**（`{"items": [...]}`）。
  根拠が0件のときに空の配列を渡すと**要素の型が決められず**、JSON の列に書けない
  （`services/bq.py` の `_array_parameter`）。辞書なら0件でも `PARSE_JSON` で書ける。
- ログに記録の本文・氏名・健康の情報を出さない（要件定義書10-2の6・11-3）。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from backend.services import bq

# 度合い（要件定義書F4・7-3-1）。値の正本はここ。画面に出す名前も同じ。
LEVELS: dict[str, str] = {
    "high": "高い",
    "medium": "中くらい",
    "low": "低い",
}

# 並べ替えの順位（`int_survivor_list.risk_rank` と同じ）。画面でも同じ順に並べられるようにする。
LEVEL_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}

# 判断したのが誰か（raw_assessments.source）。
SOURCE_AI = "ai"
SOURCE_PROFESSIONAL = "professional"
SOURCE_LABELS: dict[str, str] = {
    SOURCE_AI: "AIの判断",
    SOURCE_PROFESSIONAL: "専門職が変えた",
}

# 根拠の出どころの種類（要件定義書F4は記録と相談の両方を入力にする）。
KIND_VISIT = "visit"
KIND_CONSULTATION = "consultation"
KIND_LABELS: dict[str, str] = {
    KIND_VISIT: "訪問・見守りの記録",
    KIND_CONSULTATION: "ご本人からの相談",
}

# ホームの「対応が必要な人」に出す度合い（要件定義書F4は件数と名前を出すとだけ決めている）。
# ここを1行変えるだけで広げられるようにしておく（中くらいまで入れると人数が増えて埋もれる）。
ATTENTION_LEVELS: tuple[str, ...] = ("high",)

# 画面に必ず出す断り書き（要件定義書F4「医療の診断ではないことを画面に表示する」）。
DISCLAIMER = "医療の診断ではありません。最後に決めるのは支援者です。"

# 入力の上限。
EVIDENCE_MAX = 3
QUOTE_MAX_LENGTH = 120
NEXT_ACTION_MAX_LENGTH = 300
OVERRIDE_REASON_MAX_LENGTH = 300


class AssessmentValidationError(ValueError):
    """悪化の兆しの判断の入力が正しくない。"""


# --- 値をそろえる ---


def validate_level(value: Any) -> str:
    """度合いを確かめて返す（高・中・低のどれでもなければ断る）。"""
    if not isinstance(value, str) or value not in LEVELS:
        raise AssessmentValidationError("悪化の兆しの度合いを選んでください")
    return value


def level_label(level: Any) -> str:
    """画面に出す度合いの名前（決まっていない値のときは空にする）。"""
    return LEVELS.get(level, "") if isinstance(level, str) else ""


def clean_next_action(value: Any) -> str | None:
    """次に必要な対応をそろえる（空なら None）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:NEXT_ACTION_MAX_LENGTH]


def clean_override_reason(value: Any) -> str:
    """専門職が度合いを変えた理由。**空では受け取らない。**

    要件定義書F4は「理由を書けるようにし、記録すること」としている。AIの判断を人が
    上書きしたことだけが残って理由が無いと、あとから見直せない（mart の
    `ai_override_count` も、理由の無い上書きを数えることになる）。
    """
    if not isinstance(value, str) or not value.strip():
        raise AssessmentValidationError("度合いを変えた理由を書いてください")
    return value.strip()[:OVERRIDE_REASON_MAX_LENGTH]


def clean_evidence(items: Any, *, known_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """根拠をそろえる（引いた記述と、その記録・相談のID）。

    `known_ids` を渡すと、**渡していないIDは落とす**（Gemini が作ったIDを画面に出さない）。
    記述そのものは残す（引用は根拠として意味があるため）。
    """
    if not isinstance(items, (list, tuple)):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            continue
        kind = item.get("source_kind")
        kind = kind if kind in KIND_LABELS else ""
        source_id = item.get("source_id")
        source_id = source_id.strip() if isinstance(source_id, str) else ""
        if known_ids is not None and source_id not in known_ids:
            source_id = ""
        cleaned.append(
            {
                "quote": quote.strip()[:QUOTE_MAX_LENGTH],
                "source_kind": kind,
                "source_id": source_id,
            }
        )
        if len(cleaned) >= EVIDENCE_MAX:
            break
    return cleaned


def evidence_column(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    """`raw_assessments.evidence`（JSON の列）に入れる形にする。"""
    return {"items": list(items or [])}


def evidence_items(value: Any) -> list[dict[str, Any]]:
    """JSON の列を読み戻す（BigQuery の版によって文字列で返ることがある）。

    `visits.as_dict` と同じ理由で用意している（あちらは辞書、こちらは根拠の一覧を返す）。
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict):
        value = value.get("items")
    if not isinstance(value, list):
        return []
    return clean_evidence(value)


# --- 書き込み ---


def new_assessment_id() -> str:
    """判断のID（毎回ちがう値）。"""
    return f"as_{uuid.uuid4().hex}"


def build_row(
    *,
    survivor_id: str,
    org_id: str,
    created_by: str,
    created_role: str,
    level: str,
    evidence: list[dict[str, Any]] | None = None,
    next_action: Any = None,
    source: str = SOURCE_AI,
    override_reason: str | None = None,
    trigger_id: str | None = None,
    assessment_id: str | None = None,
) -> dict[str, Any]:
    """`raw_assessments` に入れる1行を作る（まだ書かない）。"""
    if not survivor_id or not org_id:
        raise AssessmentValidationError("記録が決まっていません")
    if source not in SOURCE_LABELS:
        raise AssessmentValidationError("判断したのが誰か決まっていません")
    return {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "assessment_id": assessment_id or new_assessment_id(),
        "level": validate_level(level),
        "evidence": evidence_column(evidence),
        "next_action": clean_next_action(next_action),
        "source": source,
        "override_reason": override_reason,
        "trigger_id": trigger_id,
    }


def save(row: dict[str, Any]) -> dict[str, Any]:
    """作った行を `raw_assessments` に追記する。"""
    bq.insert("raw_assessments", row)
    return row


def build_override_row(
    *,
    current: dict[str, Any],
    level: str,
    reason: Any,
    created_by: str,
    created_role: str,
) -> dict[str, Any]:
    """専門職が度合いを変えた1行を作る（`int_survivor_list` の行から引き継ぐ）。

    根拠・次に必要な対応は**前の判断から引き継ぐ**。人が度合いだけを直したときに根拠が
    消えると、何を見て直したのか分からなくなる。**読みは増やさない**
    （詳細の表示でもう読んでいる `int_survivor_list` の行をそのまま渡す）。

    `trigger_id` は入れない。これは記録や相談がきっかけで走った判断に付ける値で、
    人が自分の判断で変えたときのきっかけは記録ではないため（`int_survivor_list` も
    `trigger_id` を返していないので、前の判断から引き継ぐこともできない）。
    """
    return build_row(
        survivor_id=str(current.get("survivor_id") or ""),
        org_id=str(current.get("org_id") or ""),
        created_by=created_by,
        created_role=created_role,
        level=level,
        evidence=evidence_items(current.get("risk_evidence")),
        next_action=current.get("risk_next_action"),
        source=SOURCE_PROFESSIONAL,
        override_reason=clean_override_reason(reason),
        trigger_id=None,
    )


# --- 画面に返す形 ---


def risk_payload(row: dict[str, Any]) -> dict[str, Any]:
    """支援者の画面に返す「悪化の兆し」（**本人には返さない**。要件定義書F4・10-3）。

    `int_survivor_list` の `risk_*` の列から組み立てる（判断のための読みを増やさない）。
    """
    level = row.get("risk_level")
    return {
        "level": level,
        "level_label": level_label(level),
        "evidence": evidence_items(row.get("risk_evidence")),
        "next_action": row.get("risk_next_action"),
        "source": row.get("risk_source"),
        "source_label": SOURCE_LABELS.get(row.get("risk_source") or "", ""),
        "assessed_at": row.get("risk_assessed_at"),
        # 医療の診断ではないことを、画面に出す文言としてここで持つ（要件定義書F4）
        "disclaimer": DISCLAIMER,
    }


def needs_attention(row: dict[str, Any]) -> bool:
    """ホームの「対応が必要な人」に出すか（要件定義書F4）。"""
    return row.get("risk_level") in ATTENTION_LEVELS


def attention_item(row: dict[str, Any]) -> dict[str, Any]:
    """ホームに出す1人分（件数と名前。要件定義書F4）。"""
    return {
        "survivor_id": row.get("survivor_id"),
        "full_name": row.get("full_name"),
        "risk_level": row.get("risk_level"),
        "risk_level_label": level_label(row.get("risk_level")),
        "next_action": row.get("risk_next_action"),
        "days_since_last_visit": row.get("days_since_last_visit"),
        "never_visited": bool(row.get("never_visited")),
    }
