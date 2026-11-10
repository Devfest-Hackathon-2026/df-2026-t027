"""03_sql/06_migrations.sql のテスト（要望8。要件定義書7-3には無い追加のSQL）。

`02_raw_tables.sql` は `CREATE TABLE IF NOT EXISTS` なので、すでにデプロイした
データセットには列を足さない。`06_migrations.sql` が、あとから足した列を
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` で追加する。

`test_sql_files.py` の数のテスト（raw16・stg16など）はこのファイルを見ない
（`03_sql/02_raw_tables.sql`などの5ファイルだけを対象にしているため）。
ここでは、このファイルだけに通用する決まりを確かめる。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL = (REPO_ROOT / "03_sql" / "06_migrations.sql").read_text(encoding="utf-8")


def test_ファイルがある() -> None:
    assert SQL.strip() != ""


def test_何度流しても同じ状態になる() -> None:
    # ADD COLUMN IF NOT EXISTS だけを使う（2回目はなにもしない）
    assert "ALTER TABLE" in SQL
    for line in SQL.splitlines():
        if "ADD COLUMN" in line:
            assert "IF NOT EXISTS" in line, line


def test_データは変えない() -> None:
    upper = SQL.upper()
    for forbidden in ("UPDATE ", "DELETE ", "MERGE ", "TRUNCATE ", "DROP TABLE", "DROP COLUMN"):
        assert forbidden not in upper, f"{forbidden} を使っている"


def test_置き換えはPROJECTだけ() -> None:
    assert "{{PROJECT}}" in SQL
    assert "{{LOCATION}}" not in SQL


def test_足す列は要望8のものだけ() -> None:
    assert "raw_organizations" in SQL
    assert "raw_survivors" in SQL
    assert "municipality_code" in SQL
    assert "address" in SQL


# --- 07_migrations.sql（追加修正④⑥。01_docs/追加修正の計画.md 4章・6章・12章） ---

SQL_07 = (REPO_ROOT / "03_sql" / "07_migrations.sql").read_text(encoding="utf-8")
RAW_TABLES_SQL = (REPO_ROOT / "03_sql" / "02_raw_tables.sql").read_text(encoding="utf-8")
STG_VIEWS_SQL = (REPO_ROOT / "03_sql" / "03_stg_views.sql").read_text(encoding="utf-8")


def _raw_block(table: str) -> str:
    start = RAW_TABLES_SQL.index(f"gdgoc_raw.{table}`")
    return RAW_TABLES_SQL[start : RAW_TABLES_SQL.index(";", start)]


def _stg_block(view: str) -> str:
    start = STG_VIEWS_SQL.index(f"gdgoc_staging.{view}`")
    return STG_VIEWS_SQL[start : STG_VIEWS_SQL.index(";", start)]


def test_07は何度流しても同じ状態になる() -> None:
    assert "ALTER TABLE" in SQL_07
    for line in SQL_07.splitlines():
        if "ADD COLUMN" in line:
            assert "IF NOT EXISTS" in line, line


def test_07はデータを変えない() -> None:
    upper = SQL_07.upper()
    for forbidden in ("UPDATE ", "DELETE ", "MERGE ", "TRUNCATE ", "DROP TABLE", "DROP COLUMN"):
        assert forbidden not in upper, f"{forbidden} を使っている"
    assert "{{PROJECT}}" in SQL_07
    assert "{{LOCATION}}" not in SQL_07


SUPPORTER_COLUMNS = ("contact", "municipality_code", "address")


def test_支援者の列をraw_usersに足す() -> None:
    assert "gdgoc_raw.raw_users`" in SQL_07
    for column in SUPPORTER_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column} STRING" in SQL_07
        # 新しく作る環境でも同じ列を持つ
        assert f"  {column} STRING" in _raw_block("raw_users")


def test_stg_usersが支援者の列を返す() -> None:
    block = _stg_block("stg_users")
    for column in SUPPORTER_COLUMNS:
        assert f"  {column}," in block


def test_オーナーの権限の列をraw_usersに足す() -> None:
    assert "ADD COLUMN IF NOT EXISTS org_permission STRING" in SQL_07
    assert "  org_permission STRING" in _raw_block("raw_users")
    assert "  org_permission," in _stg_block("stg_users")


def test_参加コードの元の値の列をraw_organizationsに足す() -> None:
    assert "gdgoc_raw.raw_organizations`" in SQL_07
    for column in ("pro_join_code", "vol_join_code"):
        assert f"ADD COLUMN IF NOT EXISTS {column} STRING" in SQL_07
        assert f"  {column} STRING" in _raw_block("raw_organizations")
        assert f"  {column}," in _stg_block("stg_organizations")
    # 照合に使うハッシュの列も残す
    assert "  pro_join_code_hash," in _stg_block("stg_organizations")


# --- 10_migrations.sql（周りの地域の状況。使いやすさの改修の計画.md 8章） ---

SQL_10 = (REPO_ROOT / "03_sql" / "10_migrations.sql").read_text(encoding="utf-8")


def _ddl_body(text: str) -> str:
    """CREATE TABLE の文だけを取り出し、空白をそろえる（2つのファイルを比べるため）。"""
    start = text.index("CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_disaster_status`")
    return " ".join(text[start : text.index(";", start)].split())


def test_10は表を作るだけで何度流しても同じ状態になる() -> None:
    assert "CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_disaster_status`" in SQL_10
    for line in SQL_10.splitlines():
        if line.strip().startswith("CREATE"):
            assert "IF NOT EXISTS" in line, line


def test_10はデータを変えない() -> None:
    upper = SQL_10.upper()
    for forbidden in ("UPDATE ", "DELETE ", "MERGE ", "TRUNCATE ", "DROP TABLE", "DROP COLUMN"):
        assert forbidden not in upper, f"{forbidden} を使っている"
    assert "{{PROJECT}}" in SQL_10
    assert "{{LOCATION}}" not in SQL_10


def test_10の表と02の表は同じ定義() -> None:
    # 新しく作る環境（02）と、すでにある環境（10）で表の形がずれないようにする
    assert _ddl_body(SQL_10) == _ddl_body(RAW_TABLES_SQL)


def test_周りの地域の状況の表は版と出典と終わった印を持つ() -> None:
    block = _raw_block("raw_disaster_status")
    for column in (
        "status_id STRING NOT NULL",
        "version INT64 NOT NULL",
        "municipality_code STRING NOT NULL",
        "prefecture_code STRING NOT NULL",
        "disaster_name STRING",
        "item_kind STRING",
        "state STRING",
        "ended BOOL",
        "deadline DATE",
        "source_url STRING",
        "note STRING",
    ):
        assert column in block, column


def test_stg_disaster_statusは最新の版を1件だけ返す() -> None:
    block = _stg_block("stg_disaster_status")
    assert "PARTITION BY status_id ORDER BY version DESC" in block
    assert "WHERE rn = 1" in block
    # 終わった印は空を false にそろえる（消さずに印の版を足すため）
    assert "IFNULL(ended, FALSE) AS ended" in block
    for column in ("prefecture_code,", "source_url,", "updated_at,"):
        assert column in block, column
