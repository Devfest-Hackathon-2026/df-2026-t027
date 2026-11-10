"""本人からの相談（F8）の組み立てと値の検査のテスト（要件定義書F8・7-3-1、開発手順書 Step 8）。

Google Cloud には接続しない。確かめるのは次の5つ。

- 相談の種類が要件定義書F8の6つで、**読み取れない値は「その他」になる**（保存を止めない）
- `raw_consultations`・`raw_consultation_events` の列と、作る行の列が合っていること
- **本人に返す形に急ぎの度合いが入らない**（支援者向けの判断を本人に見せない。要件定義書10-3）
- 組織の一覧が**未対応から・急ぎの順**に並ぶこと（要件定義書F8）
- F4 に渡す形（`risk_ai` が読む項目の名前）が合っていること
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from backend.config import get_settings
from backend.services import consultations, visits

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    """.env が無い環境（git worktree など）でも、読みの SQL を組み立てられるようにする。"""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gdgoc-test-project")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
RAW_SQL = (REPO_ROOT / "03_sql" / "02_raw_tables.sql").read_text(encoding="utf-8")

ORG = "org_wajima"
SURVIVOR = "sv_0001"
SELF_UID = "uid-self-1"
PRO_UID = "uid-pro-1"
CONSULTATION = "cs_0001"
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)

RAW_TEXT = "きのうから胸が苦しい。薬が切れてしまって、どこでもらえるか分からない。"


def columns_of(table: str) -> list[str]:
    """03_sql/02_raw_tables.sql から、その表の列の名前を取り出す。"""
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS `\{\{PROJECT\}\}\.gdgoc_raw\."
        + re.escape(table)
        + r"` \((.*?)\n\)",
        RAW_SQL,
        re.DOTALL,
    )
    assert match, f"{table} の定義が 02_raw_tables.sql に見つかりません"
    return re.findall(r"^\s{2}(\w+) ", match.group(1), re.MULTILINE)


def filled_items() -> dict[str, str]:
    return consultations.clean_items({"condition": "胸が苦しい", "medication": "薬が切れた"})


def build_row(**overrides):
    row = {
        "survivor_id": SURVIVOR,
        "org_id": ORG,
        "created_by": SELF_UID,
        "created_role": "survivor",
        "raw_text": RAW_TEXT,
        "category": "health",
        "items": filled_items(),
        "emergency_flag": False,
        "urgency": "high",
        "urgency_evidence": "「胸が苦しい」と書かれている",
    }
    row.update(overrides)
    return consultations.build_row(**row)


# --- 相談の種類（要件定義書F8） ---


def test_相談の種類は要件定義書の6つ() -> None:
    assert list(consultations.CATEGORIES.values()) == [
        "体調",
        "介護",
        "住まい",
        "お金と制度",
        "生活",
        "その他",
    ]


def test_読み取れない種類はその他にする() -> None:
    # 種類が決まらないだけで相談を保存できないと、本人が送れなくなる
    assert consultations.normalize_category("unknown") == consultations.DEFAULT_CATEGORY
    assert consultations.normalize_category("") == consultations.DEFAULT_CATEGORY
    assert consultations.normalize_category(None) == consultations.DEFAULT_CATEGORY


def test_日本語の名前で返ってきた種類もコードに直す() -> None:
    assert consultations.normalize_category("住まい") == "housing"


# --- 9つの項目（F2と同じ項目を使う。要件定義書F8） ---


def test_項目はF2と同じ9つ() -> None:
    assert list(consultations.clean_items({})) == list(visits.VISIT_FIELDS)


def test_書かれていない項目は記載なしになる() -> None:
    items = consultations.clean_items({"condition": "胸が苦しい"})
    assert items["condition"] == "胸が苦しい"
    assert items["sleep"] == visits.NOT_RECORDED


# --- 文章と急ぎの度合いの検査 ---


def test_相談の文章が空なら断る() -> None:
    with pytest.raises(consultations.ConsultationValidationError):
        consultations.clean_raw_text("   ")


def test_相談の文章が長すぎれば断る() -> None:
    with pytest.raises(consultations.ConsultationValidationError):
        consultations.clean_raw_text("あ" * (consultations.RAW_TEXT_MAX_LENGTH + 1))


def test_急ぎの度合いは高中低だけ() -> None:
    assert consultations.validate_urgency("high") == "high"
    with pytest.raises(consultations.ConsultationValidationError):
        consultations.validate_urgency("urgent")


def test_AIで判断できないときの急ぎの度合い() -> None:
    # 急を要すると整える処理が見たときだけ「高い」。そうでなければ「中」にする
    # （全部「高い」にすると専門職のホームが埋まり、急ぎの順が意味を失う）
    assert consultations.fallback_urgency(emergency_flag=True) == "high"
    assert consultations.fallback_urgency(emergency_flag=False) == "medium"


# --- raw_consultations に入れる行 ---


def test_相談の行は表の列とそろっている() -> None:
    row = build_row()
    assert sorted(row) == sorted(columns_of("raw_consultations"))


def test_整えた項目はJSONの列に辞書で入れる() -> None:
    # 配列をそのまま渡すと、0件のときに要素の型が決められず JSON の列に書けない
    row = build_row()
    assert isinstance(row["structured"], dict)
    assert row["structured"]["items"]["condition"] == "胸が苦しい"


def test_文章も項目も無ければ断る() -> None:
    with pytest.raises(consultations.ConsultationValidationError):
        build_row(raw_text="", items=consultations.clean_items({}))


def test_文章があれば項目がすべて記載なしでも保存できる() -> None:
    # ご本人の文章が正本。AIで整えられなくても相談を捨てない（画面の改修の計画.md 6章）
    row = build_row(raw_text="薬が足りない", items=consultations.clean_items({}))
    assert row["raw_text"] == "薬が足りない"


def test_相談のIDは毎回ちがう() -> None:
    assert consultations.new_consultation_id() != consultations.new_consultation_id()
    assert consultations.new_consultation_id().startswith("cs_")


def test_相談を保存すると追記される(monkeypatch) -> None:
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        consultations.bq, "insert", lambda table, row: written.append((table, row)) or 1
    )
    row = build_row()
    consultations.save(row)
    assert written[0][0] == "raw_consultations"
    assert written[0][1]["consultation_id"] == row["consultation_id"]


# --- raw_consultation_events（返事と状況の変更） ---


def event_row(**overrides):
    row = {
        "survivor_id": SURVIVOR,
        "org_id": ORG,
        "consultation_id": CONSULTATION,
        "created_by": PRO_UID,
        "created_role": "professional",
        "action": consultations.ACTION_REPLIED,
        "reply_text": "きょうの午後に伺います。お薬は訪問時に確かめます",
        "next_action": "午後に訪問する",
        "status": consultations.STATUS_IN_PROGRESS,
    }
    row.update(overrides)
    return consultations.build_event_row(**row)


def test_返事の行は表の列とそろっている() -> None:
    assert sorted(event_row()) == sorted(columns_of("raw_consultation_events"))


def test_返事が空なら断る() -> None:
    with pytest.raises(consultations.ConsultationValidationError):
        event_row(reply_text="  ")


def test_状況の変更は3つの値だけ() -> None:
    row = event_row(
        action=consultations.ACTION_STATUS_CHANGED,
        reply_text=None,
        next_action=None,
        status=consultations.STATUS_CLOSED,
    )
    assert row["status"] == consultations.STATUS_CLOSED
    with pytest.raises(consultations.ConsultationValidationError):
        event_row(action=consultations.ACTION_STATUS_CHANGED, reply_text=None, status="done")


def test_状況を変えるときは状況が必須() -> None:
    with pytest.raises(consultations.ConsultationValidationError):
        event_row(action=consultations.ACTION_STATUS_CHANGED, reply_text=None, status=None)


def test_知らない行為は断る() -> None:
    with pytest.raises(consultations.ConsultationValidationError):
        event_row(action="deleted")


# --- 本人の書き足し（要望7。要件定義書には無い追加の機能） ---


def test_本人の書き足しの行は表の列とそろっている() -> None:
    row = event_row(
        created_by=SELF_UID,
        created_role="survivor",
        action=consultations.ACTION_MESSAGE,
        reply_text="その後、熱も出てきました",
        next_action=None,
        status=None,
    )
    assert sorted(row) == sorted(columns_of("raw_consultation_events"))
    assert row["reply_text"] == "その後、熱も出てきました"


def test_本人の書き足しが空なら断る() -> None:
    with pytest.raises(consultations.ConsultationValidationError):
        event_row(
            created_by=SELF_UID,
            created_role="survivor",
            action=consultations.ACTION_MESSAGE,
            reply_text="  ",
            next_action=None,
            status=None,
        )


def test_本人の書き足しは状況や次の対応を変えない() -> None:
    # 専門職の返事と違い、本人は状況（未対応・対応中・対応済み）を変えられない。
    # status や next_action を渡しても無視する（要件定義書10-3「使える人」の考え方）
    row = event_row(
        created_by=SELF_UID,
        created_role="survivor",
        action=consultations.ACTION_MESSAGE,
        reply_text="その後、熱も出てきました",
        next_action="無視されるはず",
        status=consultations.STATUS_CLOSED,
    )
    assert row["status"] is None
    assert row["next_action"] is None


# --- 画面に返す形 ---


def stg_row(**overrides) -> dict:
    row = {
        "consultation_id": CONSULTATION,
        "org_id": ORG,
        "survivor_id": SURVIVOR,
        "raw_text": RAW_TEXT,
        "category": "health",
        "structured": {"items": filled_items()},
        "emergency_flag": True,
        "urgency": "high",
        "urgency_evidence": "「胸が苦しい」と書かれている",
        "ai_used": True,
        "consulted_at": NOW,
        "consulted_by": SELF_UID,
        "consulted_role": "survivor",
        "status": consultations.STATUS_OPEN,
        "first_replied_at": None,
        "reply_count": 0,
        "first_reply_minutes": None,
        "last_reply_text": None,
        "last_next_action": None,
        "last_replied_at": None,
        "is_unanswered": True,
    }
    row.update(overrides)
    return row


def test_本人に返す形に急ぎの度合いを入れない() -> None:
    # 急ぎの度合いと根拠は支援者が訪ねる順番を決めるためのもの（要件定義書10-3・F4の考え方）
    payload = consultations.payload(stg_row(), role="survivor")
    assert "urgency" not in payload
    assert "urgency_evidence" not in payload
    assert payload["status_label"] == "未対応"
    assert payload["category_label"] == "体調"
    assert payload["items"]["condition"] == "胸が苦しい"


def test_専門職に返す形には急ぎの度合いが入る() -> None:
    payload = consultations.payload(stg_row(), role="professional")
    assert payload["urgency"] == "high"
    assert payload["urgency_label"] == consultations.URGENCY_LEVELS["high"]
    assert payload["urgency_evidence"] == "「胸が苦しい」と書かれている"


def test_本人にも返事と状況は返す() -> None:
    payload = consultations.payload(
        stg_row(
            status=consultations.STATUS_IN_PROGRESS,
            reply_count=1,
            last_reply_text="午後に伺います",
            last_next_action="午後に訪問する",
            last_replied_at=NOW,
            is_unanswered=False,
        ),
        role="survivor",
    )
    assert payload["last_reply_text"] == "午後に伺います"
    assert payload["last_next_action"] == "午後に訪問する"
    assert payload["status_label"] == "対応中"


def test_JSONの列が文字列で返ってきても項目を読める() -> None:
    payload = consultations.payload(
        stg_row(structured='{"items": {"condition": "胸が苦しい"}}'), role="survivor"
    )
    assert payload["items"]["condition"] == "胸が苦しい"


# --- 読みの SQL ---


def test_組織の一覧は未対応から急ぎの順に並べる(monkeypatch) -> None:
    captured: dict = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(consultations.bq, "query", fake_query)
    consultations.list_for_org(org_id=ORG)
    sql = captured["sql"]
    assert "stg_consultations" in sql
    # 氏名を出すために被災者の記録とつなぐ（読みは1回のままにする）
    assert "stg_survivors" in sql
    assert sql.index("is_unanswered DESC") < sql.index("urgency_rank DESC")
    assert captured["params"] == {"org_id": ORG}


def test_組織の一覧は組織の外を読まない(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        consultations.bq,
        "query",
        lambda sql, params=None: captured.update(sql=sql, params=params) or [],
    )
    consultations.list_for_org(org_id=ORG)
    # 絞るのは本人の記録のいまの組織（追加修正②⑦。送った時点の組織ではない）
    assert "s.org_id = @org_id" in captured["sql"]
    assert "c.org_id = @org_id" not in captured["sql"]


def test_その人の相談は新しい順に読む(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        consultations.bq,
        "query",
        lambda sql, params=None: captured.update(sql=sql, params=params) or [],
    )
    consultations.list_for_survivor(survivor_id=SURVIVOR, org_id=ORG)
    assert "ORDER BY consulted_at DESC" in captured["sql"]
    # 被災者のIDだけで読む（組織がないときに送った相談も、組織を変える前の相談も出す）
    assert captured["params"] == {"survivor_id": SURVIVOR}
    assert "org_id = @org_id" not in captured["sql"]


def test_組織やIDが空なら読まない(monkeypatch) -> None:
    monkeypatch.setattr(
        consultations.bq, "query", lambda *a, **k: pytest.fail("読んではいけません")
    )
    assert consultations.list_for_org(org_id="") == []
    assert consultations.list_for_survivor(survivor_id="", org_id=ORG) == []


def test_公開の一覧は組織のない相談だけを氏名なしで読む(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        consultations.bq,
        "query",
        lambda sql, params=None: captured.update(sql=sql, params=params) or [],
    )
    consultations.list_public()
    sql = captured["sql"]
    # 相談の行にも、本人の記録のいまの版にも組織がないものだけ
    assert "IFNULL(c.org_id, '') = ''" in sql
    assert "IFNULL(s.org_id, '') = ''" in sql
    # 氏名・年齢は読まない（市区町村まで）
    assert "full_name" not in sql
    assert "s.age" not in sql
    assert "municipality_code" in sql
    assert sql.index("is_unanswered DESC") < sql.index("urgency_rank DESC")


def test_公開の相談の形に氏名と被災者のIDを入れない() -> None:
    row = {
        "consultation_id": CONSULTATION,
        "survivor_id": SURVIVOR,
        "full_name": "佐藤 太郎",
        "age": 78,
        "category": "health",
        "structured": {"items": {"condition": "胸が苦しい"}},
        "raw_text": RAW_TEXT,
        "status": "open",
        "urgency": "high",
        "is_unanswered": True,
        "municipality_code": "172049",
    }
    item = consultations.public_payload(row)
    assert "survivor_id" not in item
    assert "full_name" not in item
    assert "age" not in item
    assert "佐藤" not in str(item)
    assert item["municipality_code"] == "172049"
    assert item["is_public"] is True


@pytest.mark.parametrize(
    ("consultation_org", "survivor_org", "expected"),
    [
        # 組織がないときに送り、いまも組織がない → 公開
        ("", "", True),
        (None, None, True),
        # 組織がないときに送ったが、いまは組織につないだ → その組織の相談（公開ではない）
        ("", ORG, False),
        # 組織に宛てて送り、のちに組織を外した → 本人だけが見る（公開にしない）
        (ORG, "", False),
        (ORG, ORG, False),
    ],
)
def test_公開かどうかは本人のいまの組織で決める(consultation_org, survivor_org, expected) -> None:
    assert (
        consultations.is_public(
            consultation_org_id=consultation_org, survivor_org_id=survivor_org
        )
        is expected
    )


def test_組織がない本人の相談は組織のIDを空にして残す() -> None:
    row = consultations.build_row(
        survivor_id=SURVIVOR,
        org_id="",
        created_by=SELF_UID,
        created_role="survivor",
        raw_text=RAW_TEXT,
        category="health",
        items={"condition": "胸が苦しい"},
        emergency_flag=False,
        urgency="medium",
    )
    assert row["org_id"] == ""


# --- 相談のやり取り（要望7） ---


def test_やり取りは古い順に読み返事と書き足しだけを対象にする(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        consultations.bq,
        "query",
        lambda sql, params=None: captured.update(sql=sql, params=params) or [],
    )
    consultations.list_events(consultation_id=CONSULTATION, survivor_id=SURVIVOR, org_id=ORG)
    assert "ORDER BY created_at ASC" in captured["sql"]
    # 公開だったときのやり取りも、組織を変える前のやり取りも、相談のIDで読む（追加修正②⑦）
    assert captured["params"] == {
        "consultation_id": CONSULTATION,
        "survivor_id": SURVIVOR,
        "actions": [consultations.ACTION_REPLIED, consultations.ACTION_MESSAGE],
    }


def test_やり取りはIDが空なら読まない(monkeypatch) -> None:
    monkeypatch.setattr(
        consultations.bq, "query", lambda *a, **k: pytest.fail("読んではいけません")
    )
    assert consultations.list_events(consultation_id="", survivor_id=SURVIVOR, org_id=ORG) == []


def test_やり取りの1件分は立場が分かる() -> None:
    reply = consultations.event_payload(
        {
            "event_id": "evt_1",
            "created_at": NOW,
            "created_role": "professional",
            "action": consultations.ACTION_REPLIED,
            "reply_text": "きょうの午後に伺います",
            "next_action": "午後に訪問する",
        }
    )
    assert reply["from_survivor"] is False
    assert reply["text"] == "きょうの午後に伺います"

    message = consultations.event_payload(
        {
            "event_id": "evt_2",
            "created_at": NOW,
            "created_role": "survivor",
            "action": consultations.ACTION_MESSAGE,
            "reply_text": "その後、熱も出てきました",
            "next_action": None,
        }
    )
    assert message["from_survivor"] is True
    assert message["next_action"] is None


# --- F4（悪化の兆し）に渡す形 ---


def test_F4に渡す形はrisk_aiが読む項目の名前にそろえる() -> None:
    rows = consultations.recent_for_ai([stg_row()])
    assert rows[0]["consultation_id"] == CONSULTATION
    assert rows[0]["consulted_at"] == NOW
    assert rows[0]["category"] == "体調"
    assert rows[0]["urgency"] == "high"
    # 本文は、整えた項目から組み立てる（本人が書いた文章そのままではない）
    assert "胸が苦しい" in rows[0]["body"]


def test_F4に渡すのは直近の数件だけ() -> None:
    rows = consultations.recent_for_ai([stg_row() for _ in range(10)], limit=3)
    assert len(rows) == 3


# --- 送信前の案内（要件定義書F8） ---


def test_命に関わるときの案内に119番が入っている() -> None:
    assert "119" in consultations.EMERGENCY_GUIDE
