"""04_scripts/seed.py のテスト（開発手順書 Step 3、要件定義書1-3・10-4・11-3）。

初期データは全国共通の支援制度だけ（2026-11-10 に組織と参加コードはやめた。追加修正の計画.md 13章）。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

import seed
from backend.services import bq

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_SQL = (REPO_ROOT / "03_sql" / "02_raw_tables.sql").read_text(encoding="utf-8")

NOW = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)


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


def test_組織と参加コードは入れない() -> None:
    # 組織はアプリの「組織を作る」で作る（要望2）。初期データの組織と参加コードはやめた
    for name in ("SEED_ORGANIZATIONS", "parse_join_codes", "generate_join_codes", "build_organization_row"):
        assert not hasattr(seed, name), name
    tables = {item.table for item in _plan_from_nothing()}
    assert tables == {"raw_programs"}


def test_設定に初期データの参加コードの項目がない() -> None:
    config = (REPO_ROOT / "02_app" / "backend" / "config.py").read_text(encoding="utf-8")
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SEED_JOIN_CODES" not in config
    assert "SEED_JOIN_CODES" not in example


# --- 制度の行 ---


def test_program_row_matches_the_raw_table_columns() -> None:
    row = seed.build_program_row("", seed.SEED_PROGRAMS[0], version=1, now=NOW)
    assert set(row) == set(columns_of("raw_programs"))


def test_全国共通の制度のIDは組織の制度とぶつからない() -> None:
    # stg_programs は program_id だけで最新の版を取る。2026-11-10 より前に入れた
    # 組織ごとの制度（org_wajima__… など）と同じIDにならないこと
    assert seed.program_id_for("", "seikatsu_saiken_kiso") == "nationwide__seikatsu_saiken_kiso"
    assert seed.program_id_for("org_wajima", "seikatsu_saiken_kiso") == "org_wajima__seikatsu_saiken_kiso"


@pytest.mark.parametrize("program", seed.SEED_PROGRAMS, ids=lambda item: item.key)
def test_every_program_field_is_filled(program: seed.SeedProgram) -> None:
    # F6 が持つことを求める項目（名前・条件・内容・申請先・期限・書類・出典）がそろっている
    for column in seed.PROGRAM_CONTENT_COLUMNS:
        value = getattr(program, column)
        assert isinstance(value, str) and value.strip(), f"{program.key} の {column} が空です"


# 被災者生活再建支援金の2つ（［4］［17］）と、使いやすさの改修の計画.md 6-2 で足した制度（［18］）
SAIKEN_KEYS = ["seikatsu_saiken_kiso", "seikatsu_saiken_kasan"]
KAKUSHU_KEYS = [
    "saigai_choui_kin",
    "saigai_shougai_mimai_kin",
    "saigai_engo_shikin",
    "seikatsu_fukushi_shikin",
    "kinkyu_shuuri",
    "juutaku_oukyuu_shuuri",
]


def _program(key: str) -> seed.SeedProgram:
    return next(program for program in seed.SEED_PROGRAMS if program.key == key)


@pytest.mark.parametrize("program", seed.SEED_PROGRAMS, ids=lambda item: item.key)
def test_every_program_cites_the_sources(program: seed.SeedProgram) -> None:
    # 出典のある公的な情報だけを登録する（要件定義書1-3・11-3）
    # ［4］＝金額と条件、［17］＝申請期間・申請窓口・必要書類、
    # ［18］＝内閣府「被災者支援に関する各種制度の概要」（6-2 で足した制度）
    if program.key in SAIKEN_KEYS:
        assert program.source_url == seed.SOURCE_URLS
        assert seed.SOURCE_URL_SUMAI in program.source_url
        assert seed.SOURCE_URL_SHINSEI in program.source_url
    else:
        assert program.source_url == seed.SOURCE_URL_KAKUSHU


def test_足した制度の出典は内閣府の防災のページ() -> None:
    assert seed.SOURCE_URL_KAKUSHU.startswith("https://www.bousai.go.jp/")


@pytest.mark.parametrize(
    "url", [seed.SOURCE_URL_SUMAI, seed.SOURCE_URL_SHINSEI, seed.SOURCE_URL_KAKUSHU]
)
def test_sources_are_public_https_pages(url: str) -> None:
    assert url.startswith("https://")
    assert url.endswith(".pdf") or url.endswith(".html")


def test_source_urls_can_be_split_on_whitespace() -> None:
    # F6 の画面で2つのリンクに分けられること（列は1つしかないため空白区切りで持つ）
    assert seed.SOURCE_URLS.split() == [seed.SOURCE_URL_SUMAI, seed.SOURCE_URL_SHINSEI]


def test_every_source_url_is_listed_in_the_requirements_document() -> None:
    # 14章に書いていない出典を登録しない
    requirements = (REPO_ROOT / "01_docs" / "要件定義書.md").read_text(encoding="utf-8")
    chapter = requirements.split("## 14. 出典")[1]
    urls = {url for program in seed.SEED_PROGRAMS for url in program.source_url.split()}
    for url in sorted(urls):
        assert url in chapter, f"{url} が要件定義書14章に載っていません"


def test_the_programs_are_the_ones_the_procedure_names() -> None:
    # 手順書の2つ（被災者生活再建支援金）と、使いやすさの改修の計画.md 6-2 で足した6つ
    assert [program.key for program in seed.SEED_PROGRAMS] == SAIKEN_KEYS + KAKUSHU_KEYS


def test_制度の名前は重ならない() -> None:
    names = [program.program_name for program in seed.SEED_PROGRAMS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("key", KAKUSHU_KEYS)
def test_足した制度は出典に無い期限や書類を作らない(key: str) -> None:
    # 資料に記載がないことは、そう書いて窓口で確かめるよう案内する
    program = _program(key)
    assert "出典の資料" in program.deadline_rule
    assert "窓口で確かめる" in program.deadline_rule
    assert "窓口で確かめる" in program.documents


def test_災害弔慰金と災害障害見舞金の額は出典のとおり() -> None:
    # 出典［18］p.1
    choui = _program("saigai_choui_kin")
    assert "500万円以下" in choui.benefit and "250万円以下" in choui.benefit
    mimai = _program("saigai_shougai_mimai_kin")
    assert "250万円以下" in mimai.benefit and "125万円以下" in mimai.benefit
    assert choui.apply_to == mimai.apply_to == "市町村"


def test_災害援護資金は貸付で出典のとおり() -> None:
    # 出典［18］p.3
    engo = _program("saigai_engo_shikin")
    assert engo.benefit.startswith("貸付")
    for text in ("150万円から350万円", "年3%以内", "3年以内（特別の場合5年）", "10年以内"):
        assert text in engo.benefit
    for text in ("220万円", "430万円", "620万円", "730万円", "1,270万円"):
        assert text in engo.conditions


def test_生活福祉資金は貸付で出典のとおり() -> None:
    # 出典［18］p.4
    fukushi = _program("seikatsu_fukushi_shikin")
    assert fukushi.benefit.startswith("貸付")
    for text in ("10万円以内", "150万円（目安）", "年1.5%", "7年以内（目安）"):
        assert text in fukushi.benefit
    assert "社会福祉協議会" in fukushi.apply_to


def test_緊急の修理と応急修理の基準額は出典のとおり() -> None:
    # 出典［18］p.29（令和8年4月の基準額）
    kinkyu = _program("kinkyu_shuuri")
    assert "5万6,400円以内（令和8年4月）" in kinkyu.benefit
    assert "10日以内" in kinkyu.deadline_rule
    oukyuu = _program("juutaku_oukyuu_shuuri")
    assert "75万7千円以内" in oukyuu.benefit
    assert "36万7千円以内（令和8年4月）" in oukyuu.benefit
    assert "り災証明" in oukyuu.conditions


def test_basic_payment_amounts_match_the_source() -> None:
    # 出典［4］p.2：全壊 100万円／大規模半壊 50万円、単数世帯は4分の3
    basic = seed.SEED_PROGRAMS[0]
    assert "100万円" in basic.benefit
    assert "50万円" in basic.benefit
    assert "4分の3" in basic.benefit
    assert "13か月以内" in basic.deadline_rule


def test_additional_payment_amounts_match_the_source() -> None:
    # 出典［4］p.2：建設・購入 200万円／補修 100万円／賃借 50万円、単数世帯は4分の3
    additional = seed.SEED_PROGRAMS[1]
    assert "200万円" in additional.benefit
    assert "100万円" in additional.benefit
    assert "50万円" in additional.benefit
    assert "4分の3" in additional.benefit
    assert "37か月以内" in additional.deadline_rule


# --- 何度流しても同じ状態になる（完了の確認） ---


def _plan_from_nothing() -> list[seed.PlannedRow]:
    return seed.plan(existing_programs={}, now=NOW)


def _as_existing(planned: list[seed.PlannedRow]) -> dict:
    """登録した行を、`stg_programs` のビューが返す形（IDで引ける辞書）にする。"""
    return {str(item.row["program_id"]): dict(item.row) for item in planned if item.row}


def test_first_run_inserts_every_row() -> None:
    inserted = [item for item in _plan_from_nothing() if item.row]
    assert len(inserted) == len(SAIKEN_KEYS + KAKUSHU_KEYS)
    assert {item.table for item in inserted} == {"raw_programs"}


def test_nationwide_programs_have_an_empty_org_and_their_own_ids() -> None:
    rows = [item.row for item in _plan_from_nothing() if item.row]
    assert [row["program_id"] for row in rows] == [
        f"nationwide__{key}" for key in SAIKEN_KEYS + KAKUSHU_KEYS
    ]
    for row in rows:
        assert row["org_id"] == ""
        assert row["source_url"] in (seed.SOURCE_URLS, seed.SOURCE_URL_KAKUSHU)
        assert row["created_by"] == "system"


def test_second_run_inserts_nothing() -> None:
    # 完了の確認「seed.py を2回流しても、stg_ のビューで重複しない」
    again = seed.plan(
        existing_programs=_as_existing(_plan_from_nothing()), now=NOW + dt.timedelta(hours=1)
    )
    assert all(item.row is None for item in again)
    assert len(again) == len(seed.SEED_PROGRAMS)


def test_changed_program_content_gets_the_next_version() -> None:
    programs = _as_existing(_plan_from_nothing())
    program_id = seed.program_id_for("", "seikatsu_saiken_kiso")
    programs[program_id]["benefit"] = "古い内容"
    programs[program_id]["version"] = 3
    rewritten = [item for item in seed.plan(existing_programs=programs, now=NOW) if item.row]
    assert len(rewritten) == 1
    assert rewritten[0].row["program_id"] == program_id
    assert rewritten[0].row["version"] == 4


def test_組織ごとの制度が入っていても全国共通の制度だけを見る() -> None:
    # 2026-11-10 より前に入れた輪島市・珠洲市の制度があっても、それには触らない
    programs = _as_existing(_plan_from_nothing())
    programs["org_wajima__seikatsu_saiken_kiso"] = {"program_id": "org_wajima__seikatsu_saiken_kiso"}
    again = seed.plan(existing_programs=programs, now=NOW)
    assert all(item.row is None for item in again)


# --- SQL に値が混ざらない（要件定義書10-3） ---


def test_insert_sql_carries_values_as_parameters_only() -> None:
    for item in _plan_from_nothing():
        assert item.row is not None
        sql, parameters = bq.build_insert("test-project", "gdgoc_raw", item.table, item.row)
        for value in item.row.values():
            if isinstance(value, str) and len(value) > 3:
                assert value not in sql, f"{item.table} の値が SQL に入っています"
        assert "@p_event_id" in sql
        assert len(parameters) == len(item.row)
