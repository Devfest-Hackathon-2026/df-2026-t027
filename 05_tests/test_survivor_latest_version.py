"""記録の最新の版を、書いた時刻で選ぶ（使いやすさの改修の計画.md 5章）。

同じ記録のIDで登録し直すと、新しい行の版の番号は1に戻る。版の番号を先に並べると、
古い版2以上が勝ち、登録し直した値（世帯の人数など）が出なくなる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.config import get_settings
from backend.services import bq, survivors

SQL = (Path(__file__).resolve().parents[1] / "03_sql" / "03_stg_views.sql").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    """.env が無い作業場所でも、読みの SQL を組み立てられるようにする。"""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gdgoc-test-project")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _view(name: str) -> str:
    match = re.search(rf"VIEW `[^`]*\.{name}` AS(.*?);", SQL, flags=re.S)
    assert match, name
    return match.group(1)


def test_stg_survivorsは書いた時刻の新しい版を選ぶ():
    view = _view("stg_survivors")
    assert "PARTITION BY survivor_id ORDER BY created_at DESC, version DESC" in view


def test_居場所の履歴も書いた時刻の順に前の版を引く():
    view = _view("stg_location_history")
    assert "ORDER BY version," not in view
    assert view.count("ORDER BY created_at, version, event_id") == 4


def test_居場所の履歴は変えた時刻の新しい順に読む(monkeypatch):
    seen = {}

    def fake_query(sql, params=None):
        seen["sql"] = sql
        return []

    monkeypatch.setattr(bq, "query", fake_query)
    survivors.location_history(survivor_id="sv_test")
    assert "ORDER BY changed_at DESC" in seen["sql"]
