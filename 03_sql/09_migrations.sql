-- デプロイ済みのデータセットに、家族などによる代わりの登録とつなぎ（使いやすさの改修の計画.md 10章）で
-- 足した列と表を追加する
--
-- 06・07_migrations.sql と同じ考え方。02_raw_tables.sql は `CREATE TABLE IF NOT EXISTS` なので、
-- すでにある表には列を足さない。新しく作る環境では 02_raw_tables.sql が最初から同じ列と表を作るので、
-- このファイルを流しても何もしないだけで害はない（何度流しても同じ状態になる）。
--
-- すでにある環境では、**このファイルを先に流してから 03_stg_views.sql・04_int_views.sql を流し直す**
-- （stg_survivors・int_survivor_list が、ここで足す列と表を読むため）。
-- 順番：09_migrations.sql → 03_stg_views.sql → 04_int_views.sql
-- {{PROJECT}} は run_sql.py が .env の値に置き換える。

-- 代わりの登録での本人の同意（obtained＝同意を得た・unable＝同意できない状態）。
-- registered_via が proxy の記録だけに入る。ほかの記録は空のまま
ALTER TABLE `{{PROJECT}}.gdgoc_raw.raw_survivors`
  ADD COLUMN IF NOT EXISTS proxy_consent STRING OPTIONS (description = "代わりの登録での本人の同意（obtained＝同意を得た・unable＝同意できない状態。proxy のときだけ）");

-- 家族と記録のつながり。02_raw_tables.sql の17番と同じ定義にする
-- （05_tests/test_family_sql.py が、2つの定義の列がそろっているかを確かめる）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_family_links` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL OPTIONS (description = "書いたときの記録の組織のID（組織がない記録は空）"),
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  user_id STRING NOT NULL OPTIONS (description = "家族としてつながる利用者のID（一般の支援者）"),
  action STRING NOT NULL OPTIONS (description = "link（つなぐ）・unlink（外す）"),
  via STRING OPTIONS (description = "つながり方（proxy＝代わりに登録した・invite＝本人の招待コード）")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, user_id
OPTIONS (description = "家族と記録のつながり（使いやすさの改修の計画.md 10章）");
