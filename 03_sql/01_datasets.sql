-- データセットを作る（要件定義書10-4、開発手順書 Step 2-1）
-- 何度流しても同じ状態になる（IF NOT EXISTS）。
-- {{PROJECT}} と {{LOCATION}} は 04_scripts/run_sql.py が .env の値に置き換える。

CREATE SCHEMA IF NOT EXISTS `{{PROJECT}}.gdgoc_raw`
OPTIONS (location = "{{LOCATION}}", description = "raw：発生したまま追記する生データ。上書きと削除はしない");

CREATE SCHEMA IF NOT EXISTS `{{PROJECT}}.gdgoc_staging`
OPTIONS (location = "{{LOCATION}}", description = "staging：stg_ で最新の版を取り出し、int_ で被災者ごとに中間集計する（すべてビュー）");

CREATE SCHEMA IF NOT EXISTS `{{PROJECT}}.gdgoc_mart`
OPTIONS (location = "{{LOCATION}}", description = "mart：スケジュールクエリで1時間ごとに作り直す集計の表");
