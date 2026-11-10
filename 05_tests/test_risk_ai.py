"""悪化の兆しを Gemini で判断する処理（F4。開発手順書 Step 7）。

Google Cloud にも Gemini にも接続しない。確かめるのは次の4つ。

- **渡すものが要件定義書10-6どおり**（直近の記録と相談を合わせて最大5件＋F3の情報。IDも渡す）
- **根拠に、渡していないIDを残さない**
- **呼べなかったときは何も保存せず、例外を外に出さない**（記録の保存を失敗にしないため）
- **度合いを読み取れなかったときも何も保存しない**
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend.config import get_settings
from backend.services import assessments, gemini, health, risk_ai, visits

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
SURVIVOR = "sv_0001"
VISIT = "vs_0001"
OLD_VISIT = "vs_0002"
CONSULTATION = "cs_0001"
PRO_UID = "uid-pro-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)


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


SURVIVOR_ROW = {
    "survivor_id": SURVIVOR,
    "org_id": ORG,
    "stage": "evacuation",
    "location_type": "shelter",
    "location_detail": "市民体育館",
}


def visit_row(**overrides) -> dict:
    row = {
        "visit_id": VISIT,
        "visited_at": NOW,
        **visits.clean_items({"condition": "熱っぽい", "meal": "朝だけ"}),
    }
    row.update(overrides)
    return row


def consultation_row(**overrides) -> dict:
    row = {
        "consultation_id": CONSULTATION,
        "consulted_at": NOW,
        "category": "健康",
        "body": "薬が残り2日分しかない",
        "urgency": "high",
    }
    row.update(overrides)
    return row


def structured(**overrides) -> risk_ai.RiskAssessment:
    payload = {
        "level": "high",
        "evidence": [{"quote": "朝だけ", "source_kind": "visit", "source_id": VISIT}],
        "next_action": "今日のうちに訪問する",
    }
    payload.update(overrides)
    return risk_ai.RiskAssessment(**payload)


def set_gemini(monkeypatch, result=None, *, fail: bool = False) -> list[dict]:
    """Gemini の問い合わせを差し替える（渡された引数を記録する）。"""
    calls: list[dict] = []

    def run(**kwargs):
        calls.append(kwargs)
        if fail:
            raise gemini.GeminiUnavailable("テスト")
        return result if result is not None else structured()

    monkeypatch.setattr(gemini, "generate_structured", run)
    return calls


def catch_saves(monkeypatch) -> list[dict]:
    saved: list[dict] = []
    monkeypatch.setattr(assessments, "save", lambda row: saved.append(row) or row)
    return saved


def set_reads(monkeypatch, *, visit_rows=None, health_row=None) -> dict:
    """直近の記録と健康の情報の読みを差し替える。"""
    reads: dict = {}

    def list_visits(**kwargs):
        reads["list_visits"] = kwargs
        return visit_rows if visit_rows is not None else [visit_row()]

    def fetch_latest(**kwargs):
        reads["fetch_latest"] = kwargs
        return health_row

    monkeypatch.setattr(visits, "list_visits", list_visits)
    monkeypatch.setattr(health, "fetch_latest", fetch_latest)
    return reads


# --- 渡すもの（要件定義書10-6） ---


def test_渡す本文に記録のIDと日付が入る():
    contents = risk_ai.build_contents(
        survivor=SURVIVOR_ROW, health_row=None, recent_visits=[visit_row()]
    )
    assert f"ID：{VISIT}" in contents
    assert "2026-11-08" in contents
    assert "体調：熱っぽい" in contents
    # 書かれていない項目は「記載なし」のまま渡す（推測で埋めさせない）
    assert "睡眠：記載なし" in contents


def test_渡す本文に段階と居場所と健康の情報が入る():
    contents = risk_ai.build_contents(
        survivor=SURVIVOR_ROW,
        health_row={"care_level": "要介護2", "chronic_diseases": ["高血圧"]},
        recent_visits=[],
    )
    assert "③避難生活" in contents
    assert "避難所" in contents
    assert "要介護2" in contents


def test_記録も相談も無いときはそう書いて渡す():
    contents = risk_ai.build_contents(survivor=SURVIVOR_ROW, health_row=None)
    assert "まだ記録も相談もありません" in contents


def test_相談も同じ本文に入る():
    # F8（Step 8）で相談が渡されるようになる。項目は raw_consultations にそろえてある
    contents = risk_ai.build_contents(
        survivor=SURVIVOR_ROW,
        health_row=None,
        recent_visits=[visit_row()],
        recent_consultations=[consultation_row()],
    )
    assert f"ID：{CONSULTATION}" in contents
    assert "薬が残り2日分しかない" in contents
    assert "種類：consultation" in contents


def test_渡したIDだけを根拠に使わせる():
    ids = risk_ai.known_ids([visit_row(), visit_row(visit_id=OLD_VISIT)], [consultation_row()])
    assert ids == {VISIT, OLD_VISIT, CONSULTATION}


# --- 返事をそろえる ---


def test_返事の根拠から作られたIDを落とす():
    payload = risk_ai.to_payload(
        structured(
            evidence=[{"quote": "朝だけ", "source_kind": "visit", "source_id": "vs_作られたID"}]
        ),
        allowed_ids={VISIT},
    )
    assert payload["evidence"][0]["source_id"] == ""
    assert payload["evidence"][0]["quote"] == "朝だけ"


def test_決まっていない度合いの返事は断る():
    with pytest.raises(assessments.AssessmentValidationError):
        risk_ai.to_payload(structured(level="とても高い"))


# --- 判断して保存する ---


def test_判断すると渡した件数は5件まで(monkeypatch):
    reads = set_reads(monkeypatch)
    set_gemini(monkeypatch)
    catch_saves(monkeypatch)

    risk_ai.assess_and_save(
        survivor=SURVIVOR_ROW, created_by=PRO_UID, created_role="professional", trigger_id=VISIT
    )

    # 要件定義書10-6「直近の記録と相談を合わせて最大5件」
    assert reads["list_visits"]["limit"] == risk_ai.RECENT_LIMIT == 5
    assert reads["fetch_latest"]["survivor_id"] == SURVIVOR


def test_判断した結果はAIの判断として保存される(monkeypatch):
    set_reads(monkeypatch)
    set_gemini(monkeypatch)
    saved = catch_saves(monkeypatch)

    result = risk_ai.assess_and_save(
        survivor=SURVIVOR_ROW, created_by=PRO_UID, created_role="volunteer", trigger_id=VISIT
    )

    assert len(saved) == 1
    assert saved[0]["level"] == "high"
    assert saved[0]["source"] == assessments.SOURCE_AI
    assert saved[0]["trigger_id"] == VISIT
    assert saved[0]["evidence"] == {
        "items": [{"quote": "朝だけ", "source_kind": "visit", "source_id": VISIT}]
    }
    assert result is not None


def test_Geminiを呼べないときは何も保存しない(monkeypatch):
    # 応答の前に走る処理なので、例外を外に出すと記録の保存まで失敗したように見える
    set_reads(monkeypatch)
    set_gemini(monkeypatch, fail=True)
    saved = catch_saves(monkeypatch)

    result = risk_ai.assess_and_save(
        survivor=SURVIVOR_ROW, created_by=PRO_UID, created_role="professional"
    )

    assert result is None
    assert saved == []


def test_度合いを読み取れないときは何も保存しない(monkeypatch):
    set_reads(monkeypatch)
    set_gemini(monkeypatch, result=structured(level=""))
    saved = catch_saves(monkeypatch)

    assert (
        risk_ai.assess_and_save(
            survivor=SURVIVOR_ROW, created_by=PRO_UID, created_role="professional"
        )
        is None
    )
    assert saved == []


def test_読みが失敗しても例外を外に出さない(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("BigQuery を呼べなかった")

    monkeypatch.setattr(visits, "list_visits", fail)
    saved = catch_saves(monkeypatch)

    assert (
        risk_ai.assess_and_save(
            survivor=SURVIVOR_ROW, created_by=PRO_UID, created_role="professional"
        )
        is None
    )
    assert saved == []


def test_記録が決まっていなければ判断しない(monkeypatch):
    saved = catch_saves(monkeypatch)

    assert (
        risk_ai.assess_and_save(
            survivor={"survivor_id": "", "org_id": ORG},
            created_by=PRO_UID,
            created_role="professional",
        )
        is None
    )
    assert saved == []


def test_考える時間を使わない設定にしてある(monkeypatch):
    # 応答の前に走るので、遅いとそのまま保存の待ち時間になる（F2 で 6.5秒→1.3秒になった設定）
    set_reads(monkeypatch)
    calls = set_gemini(monkeypatch)
    catch_saves(monkeypatch)

    risk_ai.assess_and_save(
        survivor=SURVIVOR_ROW, created_by=PRO_UID, created_role="professional"
    )

    assert calls[0]["thinking_budget"] == 0
    assert calls[0]["operation"] == risk_ai.OPERATION


def test_指示文に診断をしない決まりが書いてある():
    prompt = gemini.load_prompt(risk_ai.PROMPT_NAME)
    assert "医療の診断をしない" in prompt
    # 迷ったら高いほうにする（見落とすほうが取り返しがつかない）
    assert "高いほう" in prompt
