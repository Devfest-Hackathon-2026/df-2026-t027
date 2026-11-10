-- デプロイ済みのデータセットに、あとから足した列を追加する（要件定義書7-3には無い）
--
-- 02_raw_tables.sql は `CREATE TABLE IF NOT EXISTS` なので、すでにある表には列を
-- 足さない（表を作り直さない限り変わらない）。すでにデプロイした環境（本番・各自の
-- ローカル）に列を足すには、このファイルを流す。新しく作る環境では、02_raw_tables.sql が
-- 最初から列を持った表を作るので、このファイルを流しても ADD COLUMN IF NOT EXISTS が
-- 何もしないだけで害はない（開発手順書 Step 2-2と同じ、何度流しても同じ状態になる）。
--
-- 04_scripts/run_sql.py --all で、ほかのファイルと一緒に流れる（番号が最後なので最後に流れる）。
-- {{PROJECT}} は run_sql.py が .env の値に置き換える。

-- 要望8：市区町村と住所
ALTER TABLE `{{PROJECT}}.gdgoc_raw.raw_organizations`
  ADD COLUMN IF NOT EXISTS municipality_code STRING OPTIONS (description = "受け持つ市区町村の団体コード（6桁。要望8）");

ALTER TABLE `{{PROJECT}}.gdgoc_raw.raw_survivors`
  ADD COLUMN IF NOT EXISTS municipality_code STRING OPTIONS (description = "住んでいる市区町村の団体コード（6桁。要望8）"),
  ADD COLUMN IF NOT EXISTS address STRING OPTIONS (description = "住所（町名・番地など。任意。要望8）");
