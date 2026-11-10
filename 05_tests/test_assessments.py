"""悪化の兆しの判断の組み立て（F4。開発手順書 Step 7）。

Google Cloud にも Gemini にも接続しない。確かめるのは次の4つ。

- **決まっていない度合いを保存しない**（一覧の並べ替えで黙って最下位になるため）
- **根拠は渡したIDしか残さない**（Gemini が作ったIDを画面に出さない）
- **根拠が0件でも JSON の列に書ける**（空の配列は要素の型が決まらず書けない）
- **専門職が度合いを変えたときは、理由が必ず残り、前の判断の根拠を引き継ぐ**
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend.services import assessments, bq

PROJECT = "gdgoc-test-project"
ORG = "org_wajima"
SURVIVOR = "sv_0001"
VISIT = "vs_0001"
PRO_UID = "uid-pro-1"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)


def evidence(**overrides) -> dict:
    item = {"quote": "ごはんは朝だけ食べた", "source_kind": "visit", "source_id": VISIT}
    item.update(overrides)
    return item


def current_row(**overrides) -> dict:
    """`int_survivor_list` の1行（AIの判断が入っている状態）。"""
    row = {
        "survivor_id": SURVIVOR,
        "org_id": ORG,
        "risk_level": "high",
        "risk_evidence": {"items": [evidence()]},
        "risk_next_action": "今日のうちに訪問して水分がとれているか確かめる",
        "risk_source": assessments.SOURCE_AI,
        "risk_assessed_at": NOW,
    }
    row.update(overrides)
    return row


# --- 度合い ---


@pytest.mark.parametrize("level", ["high", "medium", "low"])
def test_度合いは3つだけ受け取る(level):
    assert assessments.validate_level(level) == level


@pytest.mark.parametrize("level", ["", "とても高い", "HIGH", None, 3, True])
def test_決まっていない度合いは断る(level):
    # 決まっていない値を保存すると、一覧の並べ替え（risk_rank）で黙って最下位になる
    with pytest.raises(assessments.AssessmentValidationError):
        assessments.validate_level(level)


def test_度合いの画面に出す名前は日本語():
    assert assessments.level_label("high") == "高い"
    assert assessments.level_label("unknown") == ""
    assert assessments.level_label(None) == ""


# --- 根拠 ---


def test_渡していないIDの根拠はIDだけ落とす():
    # 記述は根拠として意味があるので残し、Gemini が作ったIDは画面に出さない
    items = assessments.clean_evidence(
        [evidence(source_id="vs_作られたID")], known_ids={VISIT}
    )
    assert items[0]["quote"] == "ごはんは朝だけ食べた"
    assert items[0]["source_id"] == ""


def test_渡したIDの根拠はそのまま残る():
    items = assessments.clean_evidence([evidence()], known_ids={VISIT})
    assert items[0]["source_id"] == VISIT
    assert items[0]["source_kind"] == "visit"


def test_知らない出どころの種類は空にする():
    items = assessments.clean_evidence([evidence(source_kind="diary")])
    assert items[0]["source_kind"] == ""


def test_記述のない根拠は落とす():
    assert assessments.clean_evidence([{"source_id": VISIT}, evidence(quote="   ")]) == []


def test_根拠は3つまでで引用は120文字まで():
    items = assessments.clean_evidence([evidence(quote="あ" * 200)] * 5)
    assert len(items) == assessments.EVIDENCE_MAX == 3
    assert len(items[0]["quote"]) == assessments.QUOTE_MAX_LENGTH == 120


def test_根拠が一覧でなければ空にする():
    assert assessments.clean_evidence(None) == []
    assert assessments.clean_evidence("ごはんが食べられていない") == []


def test_文字列で返ってきたJSONの根拠も読み戻せる():
    # BigQuery の版によって JSON の列が文字列で返ることがある（visits.as_dict と同じ理由）
    text = '{"items": [{"quote": "眠れていない", "source_kind": "visit", "source_id": "vs_0001"}]}'
    items = assessments.evidence_items(text)
    assert items == [{"quote": "眠れていない", "source_kind": "visit", "source_id": VISIT}]


def test_読み戻せない根拠は空にする():
    assert assessments.evidence_items(None) == []
    assert assessments.evidence_items("{壊れた") == []
    assert assessments.evidence_items({"items": "文字列"}) == []


# --- 書き込む行 ---


def test_判断の行には要件定義書の列がそろう():
    row = assessments.build_row(
        survivor_id=SURVIVOR,
        org_id=ORG,
        created_by=PRO_UID,
        created_role="professional",
        level="medium",
        evidence=[evidence()],
        next_action="明日もう一度様子を見る",
        trigger_id=VISIT,
    )
    assert set(row) == {
        "event_id",
        "org_id",
        "created_at",
        "created_by",
        "created_role",
        "survivor_id",
        "assessment_id",
        "level",
        "evidence",
        "next_action",
        "source",
        "override_reason",
        "trigger_id",
    }
    assert row["assessment_id"].startswith("as_")
    assert row["source"] == assessments.SOURCE_AI
    assert row["trigger_id"] == VISIT


def test_根拠が0件でもJSONの列に書ける():
    # 空の配列を渡すと要素の型が決まらず JSON の列に書けない（services/bq.py）。
    # 辞書で持つことで 0件でも PARSE_JSON で書ける
    row = assessments.build_row(
        survivor_id=SURVIVOR,
        org_id=ORG,
        created_by=PRO_UID,
        created_role="professional",
        level="low",
    )
    assert row["evidence"] == {"items": []}
    sql, _ = bq.build_insert(PROJECT, "gdgoc_raw", "raw_assessments", row)
    assert "PARSE_JSON(@p_evidence)" in sql


def test_記録が決まっていなければ行を作らない():
    with pytest.raises(assessments.AssessmentValidationError):
        assessments.build_row(
            survivor_id="",
            org_id=ORG,
            created_by=PRO_UID,
            created_role="professional",
            level="low",
        )


def test_判断したのが誰か決まっていなければ断る():
    with pytest.raises(assessments.AssessmentValidationError):
        assessments.build_row(
            survivor_id=SURVIVOR,
            org_id=ORG,
            created_by=PRO_UID,
            created_role="professional",
            level="low",
            source="guess",
        )


def test_判断はraw_assessmentsに追記する(monkeypatch):
    inserted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        assessments.bq, "insert", lambda table, row: inserted.append((table, row))
    )

    assessments.save(
        assessments.build_row(
            survivor_id=SURVIVOR,
            org_id=ORG,
            created_by=PRO_UID,
            created_role="professional",
            level="high",
        )
    )

    assert [table for table, _ in inserted] == ["raw_assessments"]


# --- 専門職が度合いを変える ---


def test_理由のない上書きは断る():
    # 要件定義書F4は理由を記録することを求めている。理由が無いとあとから見直せない
    for reason in ["", "   ", None, 3]:
        with pytest.raises(assessments.AssessmentValidationError):
            assessments.build_override_row(
                current=current_row(),
                level="low",
                reason=reason,
                created_by=PRO_UID,
                created_role="professional",
            )


def test_上書きは前の判断の根拠と次の対応を引き継ぐ():
    row = assessments.build_override_row(
        current=current_row(),
        level="low",
        reason="本人に会って確かめた。食事はとれていた",
        created_by=PRO_UID,
        created_role="professional",
    )
    assert row["level"] == "low"
    assert row["source"] == assessments.SOURCE_PROFESSIONAL
    assert row["override_reason"] == "本人に会って確かめた。食事はとれていた"
    assert row["evidence"] == {"items": [evidence()]}
    assert row["next_action"] == "今日のうちに訪問して水分がとれているか確かめる"
    # きっかけは記録ではなく人の判断なので入れない
    assert row["trigger_id"] is None


def test_上書きの度合いも3つだけ():
    with pytest.raises(assessments.AssessmentValidationError):
        assessments.build_override_row(
            current=current_row(),
            level="とても低い",
            reason="確かめた",
            created_by=PRO_UID,
            created_role="professional",
        )


# --- 画面に返す形 ---


def test_画面に返す悪化の兆しには断り書きが入る():
    # 要件定義書F4「医療の診断ではないことを画面に表示する」。文言の正本はここ
    payload = assessments.risk_payload(current_row())
    assert payload["disclaimer"] == assessments.DISCLAIMER
    assert "診断ではありません" in payload["disclaimer"]
    assert payload["level_label"] == "高い"
    assert payload["source_label"] == "AIの判断"
    assert payload["evidence"] == [evidence()]


def test_判断がまだない人も形は同じ():
    payload = assessments.risk_payload({"survivor_id": SURVIVOR, "org_id": ORG})
    assert payload["level"] is None
    assert payload["level_label"] == ""
    assert payload["evidence"] == []
    assert payload["disclaimer"] == assessments.DISCLAIMER


# --- 対応が必要な人 ---


def test_対応が必要な人は度合いが高い人だけ():
    assert assessments.needs_attention(current_row()) is True
    assert assessments.needs_attention(current_row(risk_level="medium")) is False
    assert assessments.needs_attention(current_row(risk_level=None)) is False


def test_ホームに出す1人分には名前と日数が入る():
    item = assessments.attention_item(
        {**current_row(), "full_name": "佐藤 太郎", "days_since_last_visit": 5, "never_visited": False}
    )
    assert item["full_name"] == "佐藤 太郎"
    assert item["risk_level_label"] == "高い"
    assert item["days_since_last_visit"] == 5
    assert item["never_visited"] is False
