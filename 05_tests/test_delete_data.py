"""`04_scripts/delete_data.py` のテスト（開発手順書 Step 12-3、要件定義書11-3）。

BigQuery には接続しない。確かめるのは3つ。

- **確認の入力を通らないと消さない**（プロジェクトIDをそのまま打ち込ませる）。
- 消す表の一覧を `03_sql/02_raw_tables.sql` から読む（表が増えても消し漏れが出ない）。
- 既定（旗なし）では**数えるだけで消さない**。
"""

from __future__ import annotations

import pytest

import delete_data
from backend.config import get_settings

PROJECT = "gdgoc-test-project"


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def no_delete(monkeypatch):
    """消す処理を呼んだら記録する（呼ばれてはいけない場面の確認に使う）。"""
    called: list[str] = []

    def rows(tables, *, project, dataset):
        called.append(f"rows:{dataset}")

    def datasets(datasets_, *, project):
        called.append(f"datasets:{','.join(datasets_)}")

    monkeypatch.setattr(delete_data, "delete_rows", rows)
    monkeypatch.setattr(delete_data, "delete_datasets", datasets)
    return called


@pytest.fixture(autouse=True)
def fake_counts(monkeypatch):
    """行数を数えるところは差し替える（BigQuery を呼ばない）。"""
    monkeypatch.setattr(
        delete_data,
        "count_rows",
        lambda tables, *, project, dataset: [(table, 0) for table in tables],
    )


def answer(monkeypatch, text: str) -> None:
    monkeypatch.setattr("builtins.input", lambda *args: text)


# --- 消す表の一覧 ---


def test_rawの表の一覧をSQLから読む():
    tables = delete_data.raw_tables()

    # 7-3-1 の表（Step 2 で作ったもの）がそろっている
    assert "raw_survivors" in tables
    assert "raw_visits" in tables
    assert "raw_health_versions" in tables
    assert "raw_consultations" in tables
    assert len(tables) >= 16
    assert len(tables) == len(set(tables)), "同じ表が2回入っています"
    assert all(table.startswith("raw_") for table in tables)


def test_表を読み取れなければ止まる(tmp_path):
    path = tmp_path / "empty.sql"
    path.write_text("-- 表を作らない SQL\n", encoding="utf-8")

    with pytest.raises(delete_data.NoTablesFound):
        delete_data.raw_tables(path)


# --- 確認の入力 ---


def test_確認はプロジェクトIDがそのまま打ち込まれたときだけ通る():
    assert delete_data.confirm(PROJECT, action="消すこと", reader=lambda: PROJECT) is True
    # 前後の空白は許す（貼り付けたときに付くため）
    assert delete_data.confirm(PROJECT, action="消すこと", reader=lambda: f" {PROJECT} ") is True


@pytest.mark.parametrize("typed", ["", "yes", "y", "はい", "gdgoc-test", "GDGOC-TEST-PROJECT"])
def test_確認は違う文字では通らない(typed):
    assert delete_data.confirm(PROJECT, action="消すこと", reader=lambda: typed) is False


def test_確認は何も入力できないときは通らない():
    def reader():
        raise EOFError

    assert delete_data.confirm(PROJECT, action="消すこと", reader=reader) is False


# --- main の分かれ方 ---


def test_旗なしでは消さない(no_delete, capsys):
    code = delete_data.main([])

    assert code == 0
    assert no_delete == []
    assert "消していません" in capsys.readouterr().out


def test_rows_onlyは確認を通らなければ消さない(monkeypatch, no_delete):
    answer(monkeypatch, "yes")

    code = delete_data.main(["--rows-only"])

    assert code == 1
    assert no_delete == []


def test_rows_onlyは確認を通れば消す(monkeypatch, no_delete):
    answer(monkeypatch, PROJECT)

    code = delete_data.main(["--rows-only"])

    assert code == 0
    assert no_delete == ["rows:gdgoc_raw"]


def test_datasetsは確認を通らなければ消さない(monkeypatch, no_delete):
    answer(monkeypatch, "")

    code = delete_data.main(["--datasets"])

    assert code == 1
    assert no_delete == []


def test_datasetsは3つのデータセットを消す(monkeypatch, no_delete, capsys):
    answer(monkeypatch, PROJECT)

    code = delete_data.main(["--datasets"])

    assert code == 0
    assert no_delete == ["datasets:gdgoc_raw,gdgoc_staging,gdgoc_mart"]
    # 作り直し方を必ず出す（消したままでは動かないため）
    out = capsys.readouterr().out
    assert "run_sql.py --all" in out
    assert "seed.py" in out


def test_rows_onlyとdatasetsは同時に指定できない():
    with pytest.raises(SystemExit):
        delete_data.main(["--rows-only", "--datasets"])


def test_プロジェクトIDが無ければ何もしない(monkeypatch, no_delete):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    get_settings.cache_clear()
    answer(monkeypatch, PROJECT)

    code = delete_data.main(["--datasets"])

    assert code == 1
    assert no_delete == []


# --- 消す SQL の形 ---


def test_delete_rowsは表ごとにWHERE_TRUEのDELETEを出す(monkeypatch):
    sent: list[tuple[str, str]] = []

    def fake_execute(sql, params=None, *, operation="execute"):
        sent.append((sql, operation))

    monkeypatch.setattr(delete_data.bq, "execute", fake_execute)

    delete_data.delete_rows(["raw_visits", "raw_survivors"], project=PROJECT, dataset="gdgoc_raw")

    assert len(sent) == 2
    assert sent[0][0] == f"DELETE FROM `{PROJECT}.gdgoc_raw.raw_visits` WHERE TRUE"
    assert sent[0][1] == "delete_data"


def test_delete_rowsは表の名前を検査する(monkeypatch):
    monkeypatch.setattr(delete_data.bq, "execute", lambda *args, **kwargs: None)

    with pytest.raises(delete_data.bq.InvalidIdentifier):
        delete_data.delete_rows(["raw_visits; DROP"], project=PROJECT, dataset="gdgoc_raw")


def test_delete_datasetsは中身ごと消す(monkeypatch):
    calls: list[tuple[str, bool, bool]] = []

    class FakeClient:
        def delete_dataset(self, name, delete_contents=False, not_found_ok=False):
            calls.append((name, delete_contents, not_found_ok))

    monkeypatch.setattr(delete_data.bq, "get_client", lambda: FakeClient())

    delete_data.delete_datasets(["gdgoc_raw", "gdgoc_mart"], project=PROJECT)

    assert calls == [
        (f"{PROJECT}.gdgoc_raw", True, True),
        (f"{PROJECT}.gdgoc_mart", True, True),
    ]


def test_delete_datasetsはデータセットの名前を検査する(monkeypatch):
    monkeypatch.setattr(delete_data.bq, "get_client", lambda: None)

    with pytest.raises(delete_data.bq.InvalidIdentifier):
        delete_data.delete_datasets(["gdgoc_raw; DROP"], project=PROJECT)
