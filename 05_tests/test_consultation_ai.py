"""相談を Gemini で整え、急ぎの度合いを判断する処理（F8。開発手順書 Step 8）。

Google Cloud にも Gemini にも接続しない。確かめるのは次のこと。

- **渡すものが要件定義書10-6どおり**（本人の文章・いまの居場所・いまの健康・介護の情報・直近の記録）
- **送ったときに、整える処理と急ぎの度合いを1回の問い合わせで行う**（画面の改修の計画.md 6章）
- **急を要するかどうかは、迷ったら「要する」にする**（10-6の守らせること）
- 読み取れない相談の種類は「その他」になる（本人が送れなくなるのを防ぐ）
- 急ぎの度合いが読み取れないときは**既定の値に落とす**（送信そのものは止めない）
- 「考える時間」を 0 で呼ぶこと（F2・F4 と同じ。要件定義書11-1の10秒以内のため）
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend.config import get_settings
from backend.services import consultation_ai, consultations, gemini, visits

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
SURVIVOR = "sv_0001"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)

RAW_TEXT = "きのうから胸が苦しい。血圧の薬が切れてしまって、どこでもらえるか分からない。"

SURVIVOR_ROW = {
    "survivor_id": SURVIVOR,
    "org_id": ORG,
    "stage": "evacuation",
    "location_type": "shelter",
    "location_detail": "市民体育館",
}


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_gemini(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("テストから Gemini を呼んではいけません")

    monkeypatch.setattr(gemini, "generate_structured", fail)


def structured(**overrides) -> consultation_ai.ConsultationStructure:
    payload = {
        "category": "health",
        "location": "記載なし",
        "condition": "胸が苦しい",
        "meal": "記載なし",
        "sleep": "記載なし",
        "medication": "血圧の薬が切れた",
        "environment": "記載なし",
        "concerns": "薬をどこでもらえるか分からない",
        "change": "記載なし",
        "next_action": "記載なし",
        "emergency": True,
        "emergency_reason": "「胸が苦しい」と書かれている",
    }
    payload.update(overrides)
    return consultation_ai.ConsultationStructure(**payload)


def triaged(**overrides) -> consultation_ai.ConsultationTriage:
    payload = {**structured().model_dump(), "level": "high", "evidence": "「胸が苦しい」と書かれている"}
    payload.update(overrides)
    return consultation_ai.ConsultationTriage(**payload)


def set_gemini(monkeypatch, result=None, *, fail: bool = False) -> list[dict]:
    calls: list[dict] = []

    def run(**kwargs):
        calls.append(kwargs)
        if fail:
            raise gemini.GeminiUnavailable("テスト")
        return result if result is not None else structured()

    monkeypatch.setattr(gemini, "generate_structured", run)
    return calls


# --- 整える処理（要件定義書F8・10-6） ---


def test_渡すものは本人の文章といまの居場所と健康の情報() -> None:
    contents = consultation_ai.build_contents(
        raw_text=RAW_TEXT, survivor=SURVIVOR_ROW, health_row=None
    )
    assert RAW_TEXT in contents
    assert "避難所" in contents  # いま登録されている居場所
    assert "健康・介護" in contents


def test_整えた案は9つの項目と種類と急を要するかどうかを返す(monkeypatch) -> None:
    set_gemini(monkeypatch)
    result = consultation_ai.structure(raw_text=RAW_TEXT, survivor=SURVIVOR_ROW, health_row=None)
    assert list(result["items"]) == list(visits.VISIT_FIELDS)
    assert result["items"]["condition"] == "胸が苦しい"
    assert result["category"] == "health"
    assert result["category_label"] == "体調"
    assert result["emergency_flag"] is True
    assert result["emergency_reason"] == "「胸が苦しい」と書かれている"


def test_読み取れない種類はその他になる(monkeypatch) -> None:
    set_gemini(monkeypatch, structured(category="わからない"))
    result = consultation_ai.structure(raw_text=RAW_TEXT, survivor=SURVIVOR_ROW, health_row=None)
    assert result["category"] == consultations.DEFAULT_CATEGORY


def test_空の項目は記載なしになる(monkeypatch) -> None:
    set_gemini(monkeypatch, structured(sleep="", meal="   "))
    result = consultation_ai.structure(raw_text=RAW_TEXT, survivor=SURVIVOR_ROW, health_row=None)
    assert result["items"]["sleep"] == visits.NOT_RECORDED
    assert result["items"]["meal"] == visits.NOT_RECORDED


def test_整える処理は考える時間を0で呼ぶ(monkeypatch) -> None:
    calls = set_gemini(monkeypatch)
    consultation_ai.structure(raw_text=RAW_TEXT, survivor=SURVIVOR_ROW, health_row=None)
    assert calls[0]["thinking_budget"] == 0


def test_呼べなかったときの案は9つの項目がすべて記載なし() -> None:
    empty = consultation_ai.empty_payload()
    assert empty["category"] == consultations.DEFAULT_CATEGORY
    assert set(empty["items"].values()) == {visits.NOT_RECORDED}
    # 急を要するかどうかは判断できていない。**迷ったら要する**にはしない
    # （本人が自分で選べるよう、画面で確かめてもらう）
    assert empty["emergency_flag"] is False


# --- 送ったときの1回の問い合わせ（整える＋急ぎの度合い。画面の改修の計画.md 6章） ---


def triage(**overrides) -> dict:
    kwargs = {
        "raw_text": RAW_TEXT,
        "chosen_category": None,
        "survivor": SURVIVOR_ROW,
        "health_row": None,
        "recent_visits": [],
    }
    kwargs.update(overrides)
    return consultation_ai.triage(**kwargs)


def test_まとめた問い合わせには文章と居場所と直近の記録と選んだ種類を渡す() -> None:
    contents = consultation_ai.build_triage_contents(
        raw_text=RAW_TEXT,
        chosen_category="housing",
        survivor=SURVIVOR_ROW,
        health_row=None,
        recent_visits=[{"visit_id": "vs_1", "visited_at": NOW, **visits.clean_items({"meal": "朝だけ"})}],
    )
    assert RAW_TEXT in contents
    assert "避難所" in contents
    assert "朝だけ" in contents
    assert "住まい" in contents


def test_1回の問い合わせで整えた項目と急ぎの度合いを返す(monkeypatch) -> None:
    calls = set_gemini(monkeypatch, triaged())
    result = triage()
    assert len(calls) == 1
    assert calls[0]["operation"] == consultation_ai.TRIAGE_OPERATION
    assert calls[0]["thinking_budget"] == 0
    assert result["category"] == "health"
    assert result["items"]["condition"] == "胸が苦しい"
    assert result["emergency_flag"] is True
    assert result["urgency"] == "high"
    assert result["urgency_evidence"] == "「胸が苦しい」と書かれている"
    assert result["ai_used"] is True


def test_本人が選んだ種類をAIの案より優先する(monkeypatch) -> None:
    set_gemini(monkeypatch, triaged(category="health"))
    assert triage(chosen_category="money")["category"] == "money"


def test_急を要するときは度合いを必ず高くする(monkeypatch) -> None:
    set_gemini(monkeypatch, triaged(emergency=True, level="low"))
    assert triage()["urgency"] == consultations.EMERGENCY_URGENCY


def test_度合いを読み取れないときは既定の値に落とす(monkeypatch) -> None:
    # 送信そのものを止めない（本人が書いた相談を捨てないため）
    set_gemini(monkeypatch, triaged(level="urgent"))
    result = triage()
    assert result["urgency"] == consultations.FALLBACK_URGENCY
    assert result["ai_used"] is False


def test_呼べなかったときは記載なしと既定の度合いで返す(monkeypatch) -> None:
    set_gemini(monkeypatch, fail=True)
    result = triage(chosen_category="life")
    assert result["ai_used"] is False
    assert result["category"] == "life"
    assert set(result["items"].values()) == {visits.NOT_RECORDED}
    assert result["emergency_flag"] is False
    assert result["urgency"] == consultations.FALLBACK_URGENCY
    assert result["urgency_evidence"] == consultations.FALLBACK_URGENCY_EVIDENCE


# --- 指示文（要件定義書10-6の守らせること） ---


def test_整える指示文に記載なしと迷ったら要するが書かれている() -> None:
    prompt = gemini.load_prompt(consultation_ai.PROMPT_NAME)
    assert "記載なし" in prompt
    assert "迷ったら" in prompt


def test_まとめた指示文に記載なしと迷ったら高いほうと診断をしないが書かれている() -> None:
    prompt = gemini.load_prompt(consultation_ai.TRIAGE_PROMPT_NAME)
    assert "記載なし" in prompt
    assert "迷ったら" in prompt
    assert "診断をしない" in prompt
    # ご本人には案を見せず、ご本人の文章が正本であること
    assert "正本" in prompt
