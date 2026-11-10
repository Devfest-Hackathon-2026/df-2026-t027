"""家族などによる代わりの登録とつなぎの SQL のテスト（使いやすさの改修の計画.md 10章）。

- `09_migrations.sql` は何度流しても同じ状態になり、データを変えない。
- `raw_family_links` の定義は `02_raw_tables.sql` と `09_migrations.sql` でそろっている。
- `stg_family_links` は、記録と利用者の組ごとに最新の行が link のものだけを返す。
- `int_survivor_list` は、見てよいかの判定に使う `family_user_ids` と、本人の同意を返す。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = REPO_ROOT / "03_sql"
MIGRATION = (SQL_DIR / "09_migrations.sql").read_text(encoding="utf-8")
RAW = (SQL_DIR / "02_raw_tables.sql").read_text(encoding="utf-8")
STG = (SQL_DIR / "03_stg_views.sql").read_text(encoding="utf-8")
INT = (SQL_DIR / "04_int_views.sql").read_text(encoding="utf-8")


def _block(sql: str, name: str) -> str:
    start = sql.index(f".{name}`")
    return sql[start : sql.index(";", start)]


def _columns(block: str) -> list[str]:
    return re.findall(r"^  (\w+) (?:STRING|TIMESTAMP|INT64|BOOL|JSON)", block, flags=re.MULTILINE)


def test_09は何度流しても同じ状態になる() -> None:
    for line in MIGRATION.splitlines():
        if "ADD COLUMN" in line:
            assert "IF NOT EXISTS" in line, line
        if line.startswith("CREATE"):
            assert "IF NOT EXISTS" in line, line


def test_09はデータを変えない() -> None:
    upper = MIGRATION.upper()
    for forbidden in ("UPDATE ", "DELETE ", "MERGE ", "TRUNCATE ", "DROP TABLE", "DROP COLUMN"):
        assert forbidden not in upper, f"{forbidden} を使っている"
    assert "{{PROJECT}}" in MIGRATION
    assert "{{LOCATION}}" not in MIGRATION


def test_本人の同意の列をraw_survivorsに足す() -> None:
    assert "gdgoc_raw.raw_survivors`" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS proxy_consent STRING" in MIGRATION
    assert "  proxy_consent STRING" in _block(RAW, "raw_survivors")
    assert "  proxy_consent," in _block(STG, "stg_survivors")
    # 版を足すときに引き継げるよう、一覧の1行にも入れる
    assert "  s.proxy_consent," in _block(INT, "int_survivor_list")


def test_raw_family_linksの定義が02と09でそろっている() -> None:
    in_raw = _columns(_block(RAW, "raw_family_links"))
    in_migration = _columns(_block(MIGRATION, "raw_family_links"))
    assert in_raw == in_migration
    for column in ("survivor_id", "user_id", "action", "via"):
        assert column in in_raw
    assert "CLUSTER BY org_id, survivor_id, user_id" in _block(RAW, "raw_family_links")
    assert "CLUSTER BY org_id, survivor_id, user_id" in _block(MIGRATION, "raw_family_links")


def test_stg_family_linksは最新の行がlinkの組だけを返す() -> None:
    block = _block(STG, "stg_family_links")
    assert "PARTITION BY survivor_id, user_id ORDER BY created_at DESC, event_id DESC" in block
    assert 'WHERE rn = 1 AND action = "link"' in block


def test_int_survivor_listが家族の利用者を返す() -> None:
    block = _block(INT, "int_survivor_list")
    assert "AS family_user_ids" in block
    assert "gdgoc_staging.stg_family_links`" in block
    # 家族は組織がなくてもつながるので、被災者のIDだけでつなぐ
    assert "ON f.survivor_id = s.survivor_id\n" in block
