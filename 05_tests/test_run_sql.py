"""04_scripts/run_sql.py のテスト（開発手順書 Step 2-2）。Google Cloud には接続しない。"""

from __future__ import annotations

from pathlib import Path

import pytest

import run_sql

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_all_lists_the_five_files_in_order() -> None:
    # 06_migrations.sql は要件定義書7-3に無い追加のファイル（要望8。マイグレーション）。
    # 番号順に流れるよう、01〜05のあとに来ることだけ確かめる
    names = [path.name for path in run_sql.sql_files()]
    assert names == [
        "01_datasets.sql",
        "02_raw_tables.sql",
        "03_stg_views.sql",
        "04_int_views.sql",
        "05_mart.sql",
        "06_migrations.sql",
        # 追加修正④⑥で足した列（01_docs/追加修正の計画.md 12章）
        "07_migrations.sql",
        # 施設の職員で足した列（01_docs/使いやすさの改修の計画.md 9章）
        "08_migrations.sql",
        # 家族などによる代わりの登録とつなぎ（使いやすさの改修の計画.md 10章）
        "09_migrations.sql",
        # 周りの地域の状況の表（使いやすさの改修の計画.md 8章）
        "10_migrations.sql",
    ]


def test_render_replaces_the_placeholders_with_the_env_values() -> None:
    sql = run_sql.render(
        "CREATE SCHEMA `{{PROJECT}}.gdgoc_raw` OPTIONS (location = \"{{LOCATION}}\")",
        project="gdgoc-production-514015",
        location="asia-northeast1",
    )
    assert sql == (
        'CREATE SCHEMA `gdgoc-production-514015.gdgoc_raw` OPTIONS (location = "asia-northeast1")'
    )
    assert "{{" not in sql


def test_render_leaves_nothing_unreplaced_in_the_real_files() -> None:
    for path in run_sql.sql_files():
        sql = run_sql.render(
            path.read_text(encoding="utf-8"), project="p", location="asia-northeast1"
        )
        assert "{{" not in sql, path.name


def test_render_refuses_an_unknown_placeholder() -> None:
    with pytest.raises(run_sql.UnknownPlaceholder):
        run_sql.render("SELECT '{{DATASET}}'", project="p", location="asia-northeast1")


def test_render_refuses_an_empty_project() -> None:
    with pytest.raises(ValueError):
        run_sql.render("SELECT 1", project="", location="asia-northeast1")


def test_render_refuses_a_project_or_location_that_is_not_an_identifier() -> None:
    """置き換える値も検査する（文字列をつないで SQL を作るため。要件定義書10-3）。"""
    with pytest.raises(ValueError):
        run_sql.render("SELECT 1", project="p`; DROP SCHEMA gdgoc_raw; --", location="asia-northeast1")
    with pytest.raises(ValueError):
        run_sql.render("SELECT 1", project="p", location='asia-northeast1"; --')
