-- デプロイ済みのデータセットに、周りの地域の状況の表を足す（使いやすさの改修の計画.md 8章）
--
-- 06・07_migrations.sql と同じ考え方。02_raw_tables.sql にも同じ表を書いてあるので、
-- 新しく作る環境では 02 が表を作り、このファイルは何もしない（IF NOT EXISTS）。
-- すでにある環境（本番・各自のローカル）では、**このファイルを先に流してから 03_stg_views.sql を
-- 流し直す**（stg_disaster_status のビューが、ここで作る表を読むため）。
-- 08・09 は別の章（施設の職員・家族の登録）で使うため、10 にした。
-- 上書き・削除はしない。直すときも、終わったときも、版を1つ足す。
-- {{PROJECT}} は run_sql.py が .env の値に置き換える。

-- 2つのファイルの定義は同じにする（05_tests/test_migrations_sql.py が確かめる）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_disaster_status` (
  event_id STRING NOT NULL OPTIONS (description = "出来事のID（UUID）"),
  org_id STRING NOT NULL OPTIONS (description = "入れた人の組織のID（直せるのは同じ組織の専門職だけ）"),
  created_at TIMESTAMP NOT NULL OPTIONS (description = "発生日時（この版を入れた日時＝更新した日）"),
  created_by STRING NOT NULL OPTIONS (description = "入れた利用者のID"),
  created_role STRING NOT NULL OPTIONS (description = "入れた人の立場（professional）"),
  status_id STRING NOT NULL OPTIONS (description = "状況の1件のID（版をまたいで同じ）"),
  version INT64 NOT NULL OPTIONS (description = "版の番号"),
  municipality_code STRING NOT NULL OPTIONS (description = "市区町村の団体コード（6桁）"),
  prefecture_code STRING NOT NULL OPTIONS (description = "都道府県の番号（団体コードの上2桁。同じ県で絞るのに使う）"),
  disaster_name STRING OPTIONS (description = "災害の名前"),
  item_kind STRING OPTIONS (description = "項目（relief_act・support_act・support_open・shelter・temporary_housing・other）"),
  state STRING OPTIONS (description = "状態（例：適用中・3か所で開設中・入居の申し込みを受付中）"),
  ended BOOL OPTIONS (description = "終わった・取り下げたか（消さずに、この印の版を足す）"),
  deadline DATE OPTIONS (description = "申請・受付の期限（ないときは空）"),
  source_url STRING OPTIONS (description = "出典のURL（市区町村・内閣府などのページ。必須）"),
  note STRING OPTIONS (description = "補足")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, municipality_code, status_id
OPTIONS (description = "周りの地域の状況（使いやすさの改修の計画.md 8章）。公開の情報だけを入れ、個人の情報は入れない");
