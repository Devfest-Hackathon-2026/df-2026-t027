-- mart（集計）の5表（要件定義書7-3-4、開発手順書 Step 2-1）
--
-- スケジュールクエリで1時間ごとに作り直す（CREATE OR REPLACE TABLE）。登録は Step 11。
-- デモの前は手で流す（mart は1時間ごとにしか変わらないため）。
-- どの表にも、作り直した日時（as_of）と組織のID（org_id）を持たせる。
-- 日付の計算は日本時間（Asia/Tokyo）でそろえる。

-- 1. 組織全体の状況（F7）
CREATE OR REPLACE TABLE `{{PROJECT}}.gdgoc_mart.mart_org_status`
CLUSTER BY org_id
OPTIONS (description = "組織・段階・居場所の種類・悪化の兆しの度合いごとの人数（F7）")
AS
SELECT
  CURRENT_TIMESTAMP() AS as_of,
  org_id,
  stage,
  location_type,
  IFNULL(risk_level, "unknown") AS risk_level,
  COUNT(*) AS survivor_count,
  COUNTIF(open_consultations > 0) AS with_open_consultation,
  COUNTIF(unapplied_count > 0) AS with_unapplied_support,
  COUNTIF(pending_proposals > 0) AS with_pending_proposal
FROM `{{PROJECT}}.gdgoc_staging.int_survivor_list`
GROUP BY org_id, stage, location_type, risk_level;

-- 2. 見守りが行き届いているか（効果の確認）
--    not_visited_days は、この1か所で変える（要件定義書7-3-4の「日数は設定で変える」）。
CREATE OR REPLACE TABLE `{{PROJECT}}.gdgoc_mart.mart_visit_coverage`
CLUSTER BY org_id
OPTIONS (description = "組織・段階ごとの訪問の行き届き方と、一般の支援者の担当がいる割合")
AS
WITH config AS (
  SELECT 7 AS not_visited_days
),
base AS (
  SELECT
    s.org_id,
    s.stage,
    s.survivor_id,
    v.days_since_last_visit,
    IFNULL(g.assignee_count, 0) AS assignee_count,
    config.not_visited_days
  FROM `{{PROJECT}}.gdgoc_staging.stg_survivors` AS s
  CROSS JOIN config
  LEFT JOIN `{{PROJECT}}.gdgoc_staging.int_survivor_visits` AS v
    ON v.survivor_id = s.survivor_id AND v.org_id = s.org_id
  LEFT JOIN `{{PROJECT}}.gdgoc_staging.int_survivor_assignees` AS g
    ON g.survivor_id = s.survivor_id AND g.org_id = s.org_id
)
SELECT
  CURRENT_TIMESTAMP() AS as_of,
  org_id,
  stage,
  ANY_VALUE(not_visited_days) AS not_visited_days,
  COUNT(*) AS survivor_count,
  COUNTIF(days_since_last_visit IS NULL OR days_since_last_visit >= not_visited_days) AS not_visited_7d,
  SAFE_DIVIDE(
    COUNTIF(days_since_last_visit IS NULL OR days_since_last_visit >= not_visited_days),
    COUNT(*)
  ) AS not_visited_7d_rate,
  COUNTIF(assignee_count > 0) AS with_volunteer,
  SAFE_DIVIDE(COUNTIF(assignee_count > 0), COUNT(*)) AS with_volunteer_rate
FROM base
GROUP BY org_id, stage;

-- 3. 相談員に届くまでの遅れ（効果の確認）
CREATE OR REPLACE TABLE `{{PROJECT}}.gdgoc_mart.mart_consultation_response_daily`
CLUSTER BY org_id
OPTIONS (description = "組織・相談を受けた日・相談の種類ごとの、相談の数と最初の返事までの時間")
AS
SELECT
  CURRENT_TIMESTAMP() AS as_of,
  org_id,
  DATE(consulted_at, "Asia/Tokyo") AS consulted_on,
  IFNULL(category, "未分類") AS category,
  COUNT(*) AS consultation_count,
  COUNTIF(first_replied_at IS NOT NULL) AS replied_count,
  APPROX_QUANTILES(first_reply_minutes, 2 IGNORE NULLS)[SAFE_OFFSET(1)] AS median_first_reply_minutes,
  APPROX_QUANTILES(first_reply_minutes, 10 IGNORE NULLS)[SAFE_OFFSET(9)] AS p90_first_reply_minutes,
  COUNTIF(is_unanswered) AS open_count
FROM `{{PROJECT}}.gdgoc_staging.stg_consultations`
GROUP BY org_id, consulted_on, category;

-- 4. 支援の漏れ（効果の確認）
CREATE OR REPLACE TABLE `{{PROJECT}}.gdgoc_mart.mart_support_uptake`
CLUSTER BY org_id
OPTIONS (description = "組織・制度ごとの、示した数・申請した数・期限を過ぎて未申請の数")
AS
SELECT
  CURRENT_TIMESTAMP() AS as_of,
  s.org_id,
  s.program_id,
  ANY_VALUE(p.program_name) AS program_name,
  COUNT(*) AS suggested_count,
  COUNTIF(s.status IN ("applying", "received")) AS applied_count,
  COUNTIF(s.status = "received") AS received_count,
  COUNTIF(
    s.status = "not_applied"
    AND s.deadline IS NOT NULL
    AND s.deadline < CURRENT_DATE("Asia/Tokyo")
  ) AS unapplied_past_deadline,
  SAFE_DIVIDE(COUNTIF(s.status IN ("applying", "received")), COUNT(*)) AS uptake_rate
FROM `{{PROJECT}}.gdgoc_staging.stg_supports_latest` AS s
LEFT JOIN `{{PROJECT}}.gdgoc_staging.stg_programs` AS p
  ON p.program_id = s.program_id
  -- 全国共通の制度（org_id が空。追加修正②）は、どの組織の記録ともつなぐ
  AND (p.org_id = s.org_id OR IFNULL(p.org_id, "") = "")
GROUP BY s.org_id, s.program_id;

-- 5. 記録の量と、人がAIの判断を見直している度合い（効果の確認）
CREATE OR REPLACE TABLE `{{PROJECT}}.gdgoc_mart.mart_activity_daily`
CLUSTER BY org_id
OPTIONS (description = "組織・日・立場ごとの記録の数、Gemini で整えた割合、AIの判断を専門職が変えた数")
AS
WITH events AS (
  SELECT
    org_id,
    DATE(recorded_at, "Asia/Tokyo") AS activity_date,
    recorded_role AS role,
    "visit" AS kind,
    ai_used,
    FALSE AS is_override
  FROM `{{PROJECT}}.gdgoc_staging.stg_visits`
  UNION ALL
  SELECT
    org_id,
    DATE(consulted_at, "Asia/Tokyo") AS activity_date,
    consulted_role AS role,
    "consultation" AS kind,
    ai_used,
    FALSE AS is_override
  FROM `{{PROJECT}}.gdgoc_staging.stg_consultations`
  UNION ALL
  SELECT
    org_id,
    DATE(created_at, "Asia/Tokyo") AS activity_date,
    created_role AS role,
    "handover" AS kind,
    ai_used,
    FALSE AS is_override
  FROM `{{PROJECT}}.gdgoc_staging.stg_handovers`
  UNION ALL
  SELECT
    org_id,
    DATE(assessed_at, "Asia/Tokyo") AS activity_date,
    assessed_role AS role,
    "assessment" AS kind,
    CAST(NULL AS BOOL) AS ai_used,
    (source = "professional") AS is_override
  FROM `{{PROJECT}}.gdgoc_staging.stg_assessments`
)
SELECT
  CURRENT_TIMESTAMP() AS as_of,
  org_id,
  activity_date,
  IFNULL(role, "unknown") AS role,
  COUNTIF(kind = "visit") AS visit_count,
  COUNTIF(kind = "consultation") AS consultation_count,
  COUNTIF(kind = "handover") AS handover_count,
  SAFE_DIVIDE(COUNTIF(ai_used), COUNTIF(ai_used IS NOT NULL)) AS ai_used_rate,
  COUNTIF(is_override) AS ai_override_count
FROM events
GROUP BY org_id, activity_date, role;
