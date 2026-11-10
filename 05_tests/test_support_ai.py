"""F6 の「受けられそうな支援」を AI に出させるところのテスト（Step 10-2）。

Gemini には接続しない（`generate_structured` を差し替える）。確かめるのは次の5つ。

- **渡した一覧にない制度のIDは候補にしない**（要件定義書10-6の1330行）
- 同じ制度が2回返っても1件にする
- **期限は読み取れる日付だけ入れる**（AIが作り出した日付を候補に残さない）
- 制度が1件も無いときは **Gemini を呼ばない**
- Gemini を呼べなくても**例外を外に出さない**（自分で選んで記録できる）
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend.services import gemini, support_ai, supports

ORG = "org_wajima"
PROGRAM = "org_wajima__seikatsu_saiken_kiso"
OTHER_PROGRAM = "org_wajima__seikatsu_saiken_kasan"


def program_row(program_id: str = PROGRAM, **overrides) -> dict:
    row = {
        "program_id": program_id,
        "org_id": ORG,
        "program_name": "被災者生活再建支援金（基礎支援金）",
        "conditions": "住宅が全壊、または解体した世帯",
        "benefit": "100万円（複数世帯）",
        "apply_to": "輪島市 福祉課",
        "deadline_rule": "災害発生日から13月以内",
        "documents": "罹災証明書",
        "source_url": "https://example.lg.jp/saiken",
    }
    row.update(overrides)
    return row


def survivor_row(**overrides) -> dict:
    row = {
        "survivor_id": "sv_0001",
        "org_id": ORG,
        "full_name": "佐藤 太郎",
        "household_size": 3,
        "stage": "temporary",
    }
    row.update(overrides)
    return row


def answer_with(*items) -> support_ai.SupportCandidates:
    return support_ai.SupportCandidates(
        candidates=[support_ai.SupportCandidate(**item) for item in items]
    )


@pytest.fixture(autouse=True)
def prompt(monkeypatch):
    monkeypatch.setattr(support_ai.gemini, "load_prompt", lambda name: f"指示文:{name}")


def answer(monkeypatch, result):
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(support_ai.gemini, "generate_structured", fake)
    return captured


def suggest(**overrides):
    kwargs = {
        "survivor": survivor_row(),
        "housing_damage": "total",
        "rebuild_intent": "rebuild",
        "household_size": 3,
        "programs": [program_row()],
    }
    kwargs.update(overrides)
    return support_ai.suggest(**kwargs)


def test_渡した一覧にない制度は候補にしない(monkeypatch):
    answer(
        monkeypatch,
        answer_with(
            {"program_id": PROGRAM, "reason": "住宅が全壊のため"},
            {"program_id": "架空の見舞金", "reason": "もらえそう"},
        ),
    )
    result = suggest()
    assert [item["program_id"] for item in result["candidates"]] == [PROGRAM]
    assert result["dropped_count"] == 1
    assert result["ai_used"] is True


def test_同じ制度が2回返っても1件にする(monkeypatch):
    answer(
        monkeypatch,
        answer_with(
            {"program_id": PROGRAM, "reason": "全壊のため"},
            {"program_id": PROGRAM, "reason": "もう一度"},
        ),
    )
    result = suggest()
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["reason"] == "全壊のため"


def test_候補の申請先や書類は制度の情報から返す(monkeypatch):
    answer(
        monkeypatch,
        answer_with({"program_id": PROGRAM, "reason": "全壊のため", "deadline": "2026-12-01"}),
    )
    item = suggest()["candidates"][0]
    # AIが書いた文章ではなく、登録された制度の値を返す
    assert item["apply_to"] == "輪島市 福祉課"
    assert item["documents"] == "罹災証明書"
    assert item["source_url"] == "https://example.lg.jp/saiken"
    assert item["deadline"] == "2026-12-01"
    assert item["status"] == supports.STATUS_NOT_APPLIED
    assert item["next_action"] == supports.NEXT_ACTIONS[supports.STATUS_NOT_APPLIED]
    assert item["disclaimer"] == supports.DISCLAIMER


def test_読み取れない期限は候補に残さない(monkeypatch):
    answer(
        monkeypatch,
        answer_with({"program_id": PROGRAM, "reason": "全壊のため", "deadline": "13月以内"}),
    )
    assert suggest()["candidates"][0]["deadline"] is None


def test_長すぎる理由は切る(monkeypatch):
    answer(
        monkeypatch,
        answer_with({"program_id": PROGRAM, "reason": "あ" * (supports.REASON_MAX_LENGTH + 20)}),
    )
    assert len(suggest()["candidates"][0]["reason"]) == supports.REASON_MAX_LENGTH


def test_制度が無いときはGeminiを呼ばない(monkeypatch):
    def fail(**kwargs):
        raise AssertionError("呼んではいけません")

    monkeypatch.setattr(support_ai.gemini, "generate_structured", fail)
    result = suggest(programs=[])
    assert result["candidates"] == []
    assert result["ai_used"] is False
    assert result["notice"] == support_ai.NO_PROGRAM_NOTICE


def test_Geminiを呼べないときは候補なしで返す(monkeypatch):
    answer(monkeypatch, gemini.GeminiUnavailable("呼べない"))
    result = suggest()
    assert result["candidates"] == []
    assert result["ai_used"] is False
    assert result["notice"] == support_ai.AI_UNAVAILABLE_NOTICE


def test_AIに渡すのは被害の程度と世帯と意向と制度の一覧だけ(monkeypatch):
    captured = answer(monkeypatch, answer_with({"program_id": PROGRAM, "reason": "全壊のため"}))
    suggest(programs=[program_row(), program_row(OTHER_PROGRAM)])
    contents = captured["contents"]
    assert "全壊" in contents
    assert "建設・購入" in contents
    assert "世帯の人数：3" in contents
    assert PROGRAM in contents
    assert OTHER_PROGRAM in contents
    # 健康・介護の情報と氏名は渡さない（F6 の判断に要らない）
    assert "佐藤" not in contents
    assert "要介護" not in contents
    # 考える時間は 0（要件定義書11-1の10秒以内に収めるため）
    assert captured["thinking_budget"] == support_ai.THINKING_BUDGET


def test_世帯の人数を渡さないときは記録の値を使う(monkeypatch):
    captured = answer(monkeypatch, answer_with())
    suggest(household_size=None, survivor=survivor_row(household_size=5))
    assert "世帯の人数：5" in captured["contents"]


# --- 相談の内容も使って調べる（使いやすさの改修の計画.md 6-3） ---


def _consultation(cid: str, *, days_ago: int, text: str = "仕事がなくなり、家賃が払えない") -> dict:
    return {
        "consultation_id": cid,
        "consulted_at": dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)
        - dt.timedelta(days=days_ago),
        "category": "money",
        "raw_text": text,
        "structured": None,
        "urgency": "medium",
    }


def test_直近90日の相談を新しい5件まで選ぶ():
    now = dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)
    rows = [_consultation(f"c{i}", days_ago=i) for i in range(7)]
    rows.append(_consultation("c_old", days_ago=91))
    picked = support_ai.recent_consultations(rows, now=now)
    assert [item["consultation_id"] for item in picked] == ["c0", "c1", "c2", "c3", "c4"]


def test_相談の内容をAIに渡す(monkeypatch):
    captured = answer(monkeypatch, answer_with())
    items = support_ai.recent_consultations(
        [_consultation("c1", days_ago=1)],
        now=dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc),
    )
    suggest(consultation_items=items)
    contents = captured["contents"]
    assert "## これまでの相談" in contents
    assert "相談のID：c1" in contents
    assert "家賃が払えない" in contents


def test_相談が無いときは無いと書く(monkeypatch):
    captured = answer(monkeypatch, answer_with())
    suggest()
    assert "- 相談はありません" in captured["contents"]


def test_根拠の相談のIDは渡したものだけ残す(monkeypatch):
    answer(
        monkeypatch,
        answer_with(
            {"program_id": PROGRAM, "reason": "相談で家賃が払えないとあるため", "consultation_ids": ["c1", "c_made_up", "c1"]}
        ),
    )
    items = support_ai.recent_consultations(
        [_consultation("c1", days_ago=1)],
        now=dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc),
    )
    result = suggest(consultation_items=items)
    assert result["candidates"][0]["consultation_ids"] == ["c1"]


def test_指示文に相談の使い方と健康のことを書かない決まりがある():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[1] / "02_app" / "backend" / "prompts" / "support_candidates.md"
    ).read_text(encoding="utf-8")
    assert "相談" in text
    assert "consultation_ids" in text
    assert "健康" in text
