-- デプロイ済みのデータセットに、使いやすさの改修（01_docs/使いやすさの改修の計画.md）で足した列を追加する
--
-- 06・07_migrations.sql と同じ考え方。02_raw_tables.sql は `CREATE TABLE IF NOT EXISTS` なので、
-- すでにある表には列を足さない。すでにデプロイした環境（本番・各自のローカル）に列を足すには、
-- このファイルを流す。新しく作る環境では、02_raw_tables.sql が最初から列を持った表を作るので、
-- このファイルを流しても ADD COLUMN IF NOT EXISTS が何もしないだけで害はない。
--
-- 列を足したあとに、その列を読むビュー（stg_users・stg_facilities_latest）を作り直す。
-- ビューの中身は 03_stg_views.sql と同じにする（05_tests/test_migrations_sql.py が確かめる）。
-- そのため、すでにある環境では**このファイルだけを流せば**よい（03 を流し直さなくてよい）。
-- {{PROJECT}} は run_sql.py が .env の値に置き換える。

-- 9章：施設の職員（立場の値 facility_staff）の、自分の施設
ALTER TABLE `{{PROJECT}}.gdgoc_raw.raw_users`
  ADD COLUMN IF NOT EXISTS facility_id STRING OPTIONS (description = "施設の職員の場合、自分の施設のID（9章）");

-- 9章：施設の参加コード（ハッシュと元の値）と、版を足した理由
-- 前からある施設は空のまま。その組織の専門職が施設の画面で発行すると入る。
ALTER TABLE `{{PROJECT}}.gdgoc_raw.raw_facilities`
  ADD COLUMN IF NOT EXISTS staff_join_code_hash STRING OPTIONS (description = "施設の職員用の参加コードのハッシュ（9章）"),
  ADD COLUMN IF NOT EXISTS staff_join_code STRING OPTIONS (description = "施設の職員用の参加コード（元の値。その組織の専門職だけに返す。9章）"),
  ADD COLUMN IF NOT EXISTS change_kind STRING OPTIONS (description = "版を足した理由（info＝空きなど・join_code＝参加コードの発行。空は info。9章）");

-- 足した列を読むビューを作り直す（03_stg_views.sql と同じ中身）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_users` AS
SELECT
  user_id,
  org_id,
  role,
  profession,
  display_name,
  login_method,
  linked_survivor_id,
  -- 支援者の連絡先・活動する市区町村・住所（追加修正④）
  contact,
  municipality_code,
  address,
  -- 組織の中の権限（追加修正⑥）
  org_permission,
  -- 施設の職員の、自分の施設（使いやすさの改修の計画.md 9章）
  facility_id,
  created_at AS updated_at
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC, event_id DESC) AS rn
  FROM `{{PROJECT}}.gdgoc_raw.raw_users`
)
WHERE rn = 1;

CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_facilities_latest` AS
WITH latest AS (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY facility_id ORDER BY version DESC, created_at DESC, event_id DESC) AS rn
  FROM `{{PROJECT}}.gdgoc_raw.raw_facilities`
),
latest_info AS (
  SELECT
    facility_id,
    created_at AS updated_at,
    created_by AS updated_by,
    created_role AS updated_role
  FROM (
    SELECT
      facility_id,
      created_at,
      created_by,
      created_role,
      ROW_NUMBER() OVER (PARTITION BY facility_id ORDER BY version DESC, created_at DESC, event_id DESC) AS info_rn
    FROM `{{PROJECT}}.gdgoc_raw.raw_facilities`
    WHERE IFNULL(change_kind, "info") != "join_code"
  )
  WHERE info_rn = 1
)
SELECT
  l.facility_id,
  l.org_id,
  l.version,
  l.facility_name,
  l.facility_type,
  l.address,
  l.vacancies,
  l.capabilities,
  l.water_outage,
  l.power_outage,
  COALESCE(i.updated_at, l.created_at) AS updated_at,
  COALESCE(i.updated_by, l.created_by) AS updated_by,
  -- 最後に空きなどを更新した人の立場（施設の職員か専門職か。9章）
  COALESCE(i.updated_role, l.created_role) AS updated_role,
  -- 施設の参加コード（9章）。照合はハッシュで行い、元の値はその組織の専門職だけに返す
  l.staff_join_code_hash,
  l.staff_join_code,
  (COALESCE(i.updated_at, l.created_at) < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)) AS is_stale
FROM latest AS l
LEFT JOIN latest_info AS i
  ON i.facility_id = l.facility_id
WHERE l.rn = 1;
