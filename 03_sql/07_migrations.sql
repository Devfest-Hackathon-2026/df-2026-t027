-- デプロイ済みのデータセットに、追加修正（01_docs/追加修正の計画.md）で足した列を追加する
--
-- 06_migrations.sql と同じ考え方。02_raw_tables.sql は `CREATE TABLE IF NOT EXISTS` なので、
-- すでにある表には列を足さない。すでにデプロイした環境（本番・各自のローカル）に列を足すには、
-- このファイルを流す。新しく作る環境では、02_raw_tables.sql が最初から列を持った表を作るので、
-- このファイルを流しても ADD COLUMN IF NOT EXISTS が何もしないだけで害はない。
--
-- すでにある環境では、**このファイルを先に流してから 03_stg_views.sql を流し直す**
-- （stg_users・stg_organizations のビューが、ここで足す列を読むため）。
-- {{PROJECT}} は run_sql.py が .env の値に置き換える。

-- 追加修正④：支援者の連絡先・活動する市区町村・住所
ALTER TABLE `{{PROJECT}}.gdgoc_raw.raw_users`
  ADD COLUMN IF NOT EXISTS contact STRING OPTIONS (description = "支援者の連絡先（電話番号など。追加修正④）"),
  ADD COLUMN IF NOT EXISTS municipality_code STRING OPTIONS (description = "支援者が活動する市区町村の団体コード（6桁。追加修正④）"),
  ADD COLUMN IF NOT EXISTS address STRING OPTIONS (description = "支援者の住所（町名・番地など。任意。追加修正④）");

-- 追加修正⑥：組織のオーナーの権限と、参加コードの元の値
-- 権限は立場（role）とは別に持つ（owner か空）。付ける・外すときは新しい版の行を足す。
ALTER TABLE `{{PROJECT}}.gdgoc_raw.raw_users`
  ADD COLUMN IF NOT EXISTS org_permission STRING OPTIONS (description = "組織の中の権限（owner か空。追加修正⑥）");

-- 元のコードは、組織のオーナーがいつでも見られるように保存する。照合は今までどおりハッシュで行う。
-- この列より前に作った組織（初期データなど）は空のまま。オーナーが発行し直すと入る。
ALTER TABLE `{{PROJECT}}.gdgoc_raw.raw_organizations`
  ADD COLUMN IF NOT EXISTS pro_join_code STRING OPTIONS (description = "専門職用の参加コード（元の値。オーナーだけに返す。追加修正⑥）"),
  ADD COLUMN IF NOT EXISTS vol_join_code STRING OPTIONS (description = "一般の支援者用の参加コード（元の値。オーナーだけに返す。追加修正⑥）");
