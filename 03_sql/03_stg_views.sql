-- staging（整形）の18ビュー（16ビューは要件定義書7-3-2、開発手順書 Step 2-1）
-- 17番目の stg_disaster_status は使いやすさの改修の計画.md 8章、18番目の stg_family_links は10章で足したビュー
--
-- 作り方（要件定義書10-4）
--   - すべてビューにする。raw に書いた内容がすぐに反映される。
--   - 最新の版は、IDごとに version（版のない表は created_at）の大きい1行を ROW_NUMBER() で取り出す。
--     同じ版・同じ時刻が並んだときは event_id で順番を決め、結果が毎回同じになるようにする。
--   - 日付の計算は日本時間（Asia/Tokyo）でそろえる。
-- 何度流しても同じ状態になる（CREATE OR REPLACE VIEW）。

-- 1. 組織（org_id ごとの最新の1行。F0の参加コードの確認に使う）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_organizations` AS
SELECT
  org_id,
  org_name,
  prefecture,
  municipality_code,
  pro_join_code_hash,
  vol_join_code_hash,
  -- 参加コードの元の値（追加修正⑥。オーナーの API だけが読む）
  pro_join_code,
  vol_join_code,
  created_at AS updated_at
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY org_id ORDER BY created_at DESC, event_id DESC) AS rn
  FROM `{{PROJECT}}.gdgoc_raw.raw_organizations`
)
WHERE rn = 1;

-- 2. 利用者（user_id ごとの最新の1行）
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

-- 3. 被災者（survivor_id ごとの最新の版。最初の版の created_at を registered_at にする）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_survivors` AS
SELECT
  survivor_id,
  org_id,
  version,
  full_name,
  age,
  household_size,
  contact,
  stage,
  location_type,
  location_detail,
  municipality_code,
  address,
  self_user_id,
  registered_via,
  -- 代わりの登録での本人の同意（使いやすさの改修の計画.md 10章）
  proxy_consent,
  registered_at,
  created_at AS updated_at,
  created_by AS updated_by,
  created_role AS updated_role
FROM (
  SELECT
    *,
    -- 書いた時刻の新しい版を最新にする（使いやすさの改修の計画.md 5章）。版の番号を先にすると、
    -- 同じ記録のIDで登録し直したとき、新しい版1が古い版2以上に負けて古い値が出る
    ROW_NUMBER() OVER (PARTITION BY survivor_id ORDER BY created_at DESC, version DESC, event_id DESC) AS rn,
    MIN(created_at) OVER (PARTITION BY survivor_id) AS registered_at
  FROM `{{PROJECT}}.gdgoc_raw.raw_survivors`
)
WHERE rn = 1;

-- 4. 居場所・段階の変更（前の版と stage か location_type が違う版だけを残す）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_location_history` AS
SELECT
  survivor_id,
  org_id,
  version,
  created_at AS changed_at,
  stage,
  location_type,
  location_detail,
  prev_stage,
  prev_location_type,
  prev_location_detail,
  created_by AS changed_by,
  created_role AS changed_role
FROM (
  SELECT
    *,
    LAG(stage) OVER (PARTITION BY survivor_id ORDER BY created_at, version, event_id) AS prev_stage,
    LAG(location_type) OVER (PARTITION BY survivor_id ORDER BY created_at, version, event_id) AS prev_location_type,
    LAG(location_detail) OVER (PARTITION BY survivor_id ORDER BY created_at, version, event_id) AS prev_location_detail,
    ROW_NUMBER() OVER (PARTITION BY survivor_id ORDER BY created_at, version, event_id) AS seq
  FROM `{{PROJECT}}.gdgoc_raw.raw_survivors`
)
WHERE
  seq = 1
  OR IFNULL(stage, "") != IFNULL(prev_stage, "")
  OR IFNULL(location_type, "") != IFNULL(prev_location_type, "");

-- 5. 確認コード（code_hash ごとに発行・使用・誤りをまとめる。F0・F1の照合に使う）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_link_codes` AS
SELECT
  code_hash,
  MAX(org_id) AS org_id,
  MAX(survivor_id) AS survivor_id,
  MAX(IF(action = "issued", created_at, NULL)) AS issued_at,
  MAX(IF(action = "issued", expires_at, NULL)) AS expires_at,
  MAX(IF(action = "used", created_at, NULL)) AS used_at,
  COUNTIF(action = "failed") AS failed_count_total,
  COUNTIF(
    action = "failed"
    AND created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
  ) AS failed_count_1h,
  (
    COUNTIF(action = "used") = 0
    AND MAX(IF(action = "issued", expires_at, NULL)) > CURRENT_TIMESTAMP()
  ) AS is_valid
FROM `{{PROJECT}}.gdgoc_raw.raw_link_codes`
GROUP BY code_hash;

-- 6. 有効な担当（承認があり、その後に removed がないもの。F1）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_active_assignments` AS
WITH agg AS (
  SELECT
    assignment_id,
    MAX(org_id) AS org_id,
    MAX(survivor_id) AS survivor_id,
    MAX(supporter_user_id) AS supporter_user_id,
    MIN(IF(action = "assigned", created_at, NULL)) AS assigned_at,
    MAX(IF(action = "removed", created_at, NULL)) AS removed_at
  FROM `{{PROJECT}}.gdgoc_raw.raw_assignment_events`
  GROUP BY assignment_id
),
consent AS (
  SELECT
    assignment_id,
    action AS consent_type,
    created_at AS consented_at
  FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (PARTITION BY assignment_id ORDER BY created_at DESC, event_id DESC) AS rn
    FROM `{{PROJECT}}.gdgoc_raw.raw_assignment_events`
    WHERE action IN ("approved", "verbal_consent")
  )
  WHERE rn = 1
)
SELECT
  agg.assignment_id,
  agg.org_id,
  agg.survivor_id,
  agg.supporter_user_id,
  agg.assigned_at,
  consent.consented_at,
  consent.consent_type
FROM agg
JOIN consent ON consent.assignment_id = agg.assignment_id
WHERE agg.removed_at IS NULL OR agg.removed_at < consent.consented_at;

-- 7. 訪問・見守りの記録（visit_id ごとの最新の版。反映の記録がない pending を is_pending にする。F2）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_visits` AS
WITH latest AS (
  SELECT * FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (PARTITION BY visit_id ORDER BY version DESC, created_at DESC, event_id DESC) AS rn
    FROM `{{PROJECT}}.gdgoc_raw.raw_visits`
  )
  WHERE rn = 1
),
applies AS (
  SELECT
    visit_id,
    MAX(created_at) AS applied_at,
    COUNT(*) AS apply_count,
    MAX(result) AS apply_result
  FROM `{{PROJECT}}.gdgoc_raw.raw_update_applies`
  GROUP BY visit_id
)
SELECT
  latest.visit_id,
  latest.org_id,
  latest.survivor_id,
  latest.version,
  latest.visited_at,
  latest.raw_text,
  latest.location,
  latest.condition,
  latest.meal,
  latest.sleep,
  latest.medication,
  latest.environment,
  latest.concerns,
  latest.change,
  latest.next_action,
  latest.location_proposal,
  latest.health_proposal,
  latest.proposal_status,
  latest.ai_used,
  latest.created_at AS recorded_at,
  latest.created_by AS recorded_by,
  latest.created_role AS recorded_role,
  applies.applied_at,
  applies.apply_result,
  (latest.proposal_status = "pending" AND applies.visit_id IS NULL) AS is_pending
FROM latest
LEFT JOIN applies ON applies.visit_id = latest.visit_id;

-- 8. 健康・介護の情報（survivor_id ごとの最新の版。F3）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_health_latest` AS
SELECT
  survivor_id,
  org_id,
  version,
  chronic_diseases,
  medications,
  allergies,
  primary_clinic,
  medical_needs,
  care_level,
  assistance_mobility,
  assistance_meal,
  assistance_toilet,
  dementia_notes,
  family_contact,
  source,
  created_at AS updated_at,
  created_by AS updated_by,
  created_role AS updated_role
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY survivor_id ORDER BY version DESC, created_at DESC, event_id DESC) AS rn
  FROM `{{PROJECT}}.gdgoc_raw.raw_health_versions`
)
WHERE rn = 1;

-- 9. 引き継ぎシート（すべて。F3）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_handovers` AS
SELECT
  handover_id,
  org_id,
  survivor_id,
  destination,
  health_version,
  summary,
  cautions,
  ai_used,
  created_at,
  created_by,
  created_role
FROM `{{PROJECT}}.gdgoc_raw.raw_handovers`;

-- 10. 悪化の兆しの判断（すべて。F4）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_assessments` AS
SELECT
  assessment_id,
  org_id,
  survivor_id,
  level,
  evidence,
  next_action,
  source,
  override_reason,
  trigger_id,
  created_at AS assessed_at,
  created_by AS assessed_by,
  created_role AS assessed_role
FROM `{{PROJECT}}.gdgoc_raw.raw_assessments`;

-- 11. 悪化の兆しの最新の判断（survivor_id ごと。F4）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_assessment_latest` AS
SELECT
  survivor_id,
  org_id,
  assessment_id,
  level,
  evidence,
  next_action,
  source,
  override_reason,
  trigger_id,
  assessed_at
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY survivor_id ORDER BY assessed_at DESC, assessment_id DESC) AS rn
  FROM `{{PROJECT}}.gdgoc_staging.stg_assessments`
)
WHERE rn = 1;

-- 12. 施設（facility_id ごとの最新の版。24時間以上たった更新は is_stale にする。F5）
--   最終更新（updated_at・updated_by・updated_role）は、空きなどを変えた版だけで決める。
--   施設の参加コードを発行しただけの版（change_kind が join_code）は数えない
--   （発行しただけで新しい情報に見えると、古い空きの数を信じてしまう。使いやすさの改修の計画.md 9章）。
--   施設の参加コードの列は、照合と版の引き継ぎのために出す。画面に返す読み
--   （services/facilities.py の _COLUMNS）には入れない。
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

-- 13. 移る先の決定（すべて。F5）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_placements` AS
SELECT
  placement_id,
  org_id,
  survivor_id,
  candidates,
  chosen_facility_id,
  chosen_from_candidates,
  chosen_reason,
  created_at AS decided_at,
  created_by AS decided_by,
  created_role AS decided_role
FROM `{{PROJECT}}.gdgoc_raw.raw_placements`;

-- 14. 支援制度（program_id ごとの最新の版）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_programs` AS
SELECT
  program_id,
  org_id,
  version,
  program_name,
  conditions,
  benefit,
  apply_to,
  deadline_rule,
  documents,
  source_url,
  created_at AS updated_at
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY program_id ORDER BY version DESC, created_at DESC, event_id DESC) AS rn
  FROM `{{PROJECT}}.gdgoc_raw.raw_programs`
)
WHERE rn = 1;

-- 15. 支援と申請の状況（support_id ごとの最新の状況＋最初に示したときの理由と日時。F6）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_supports_latest` AS
WITH latest AS (
  SELECT * FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (PARTITION BY support_id ORDER BY created_at DESC, event_id DESC) AS rn
    FROM `{{PROJECT}}.gdgoc_raw.raw_support_events`
  )
  WHERE rn = 1
),
suggested AS (
  SELECT * FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (PARTITION BY support_id ORDER BY created_at ASC, event_id ASC) AS rn
    FROM `{{PROJECT}}.gdgoc_raw.raw_support_events`
    WHERE action = "suggested"
  )
  WHERE rn = 1
),
known AS (
  SELECT
    support_id,
    MAX(deadline) AS known_deadline,
    MAX(program_id) AS known_program_id
  FROM `{{PROJECT}}.gdgoc_raw.raw_support_events`
  GROUP BY support_id
)
SELECT
  latest.support_id,
  latest.org_id,
  latest.survivor_id,
  COALESCE(latest.program_id, known.known_program_id) AS program_id,
  suggested.reason AS suggested_reason,
  suggested.created_at AS suggested_at,
  COALESCE(latest.status, "not_applied") AS status,
  COALESCE(latest.deadline, known.known_deadline) AS deadline,
  DATE_DIFF(
    COALESCE(latest.deadline, known.known_deadline),
    CURRENT_DATE("Asia/Tokyo"),
    DAY
  ) AS days_to_deadline,
  COALESCE(latest.housing_damage, suggested.housing_damage) AS housing_damage,
  COALESCE(latest.rebuild_intent, suggested.rebuild_intent) AS rebuild_intent,
  latest.created_at AS updated_at,
  latest.created_by AS updated_by
FROM latest
LEFT JOIN suggested ON suggested.support_id = latest.support_id
LEFT JOIN known ON known.support_id = latest.support_id;

-- 16. 相談（本体＋最新の状況＋最初の返事までの時間＋最新の返事。F8）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_consultations` AS
WITH body AS (
  SELECT * FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (PARTITION BY consultation_id ORDER BY created_at DESC, event_id DESC) AS rn
    FROM `{{PROJECT}}.gdgoc_raw.raw_consultations`
  )
  WHERE rn = 1
),
replies AS (
  SELECT
    consultation_id,
    MIN(created_at) AS first_replied_at,
    COUNT(*) AS reply_count
  FROM `{{PROJECT}}.gdgoc_raw.raw_consultation_events`
  WHERE action = "replied"
  GROUP BY consultation_id
),
last_reply AS (
  SELECT
    consultation_id,
    reply_text AS last_reply_text,
    next_action AS last_next_action,
    created_at AS last_replied_at
  FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (PARTITION BY consultation_id ORDER BY created_at DESC, event_id DESC) AS rn
    FROM `{{PROJECT}}.gdgoc_raw.raw_consultation_events`
    WHERE action = "replied"
  )
  WHERE rn = 1
),
latest_status AS (
  SELECT
    consultation_id,
    status
  FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (PARTITION BY consultation_id ORDER BY created_at DESC, event_id DESC) AS rn
    FROM `{{PROJECT}}.gdgoc_raw.raw_consultation_events`
    WHERE action = "status_changed" AND status IS NOT NULL
  )
  WHERE rn = 1
)
SELECT
  body.consultation_id,
  body.org_id,
  body.survivor_id,
  body.raw_text,
  body.category,
  body.structured,
  body.emergency_flag,
  body.urgency,
  body.urgency_evidence,
  body.ai_used,
  body.created_at AS consulted_at,
  body.created_by AS consulted_by,
  body.created_role AS consulted_role,
  COALESCE(latest_status.status, "open") AS status,
  replies.first_replied_at,
  IFNULL(replies.reply_count, 0) AS reply_count,
  TIMESTAMP_DIFF(replies.first_replied_at, body.created_at, MINUTE) AS first_reply_minutes,
  last_reply.last_reply_text,
  last_reply.last_next_action,
  last_reply.last_replied_at,
  (
    IFNULL(replies.reply_count, 0) = 0
    AND COALESCE(latest_status.status, "open") = "open"
  ) AS is_unanswered
FROM body
LEFT JOIN replies ON replies.consultation_id = body.consultation_id
LEFT JOIN last_reply ON last_reply.consultation_id = body.consultation_id
LEFT JOIN latest_status ON latest_status.consultation_id = body.consultation_id;

-- 17. 周りの地域の状況（status_id ごとの最新の版。使いやすさの改修の計画.md 8章）
-- 終わった・取り下げたものも残す（ended = true の版）。画面に出すかは services/disaster_status.py が決める。
-- 「古い情報かもしれません」（更新から30日）の判断も services/disaster_status.py が正本（ここでは持たない）。
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_disaster_status` AS
SELECT
  status_id,
  org_id,
  version,
  municipality_code,
  prefecture_code,
  disaster_name,
  item_kind,
  state,
  IFNULL(ended, FALSE) AS ended,
  deadline,
  source_url,
  note,
  updated_at,
  updated_by,
  updated_role
FROM (
  SELECT
    * EXCEPT (created_at, created_by, created_role),
    created_at AS updated_at,
    created_by AS updated_by,
    created_role AS updated_role,
    ROW_NUMBER() OVER (PARTITION BY status_id ORDER BY version DESC, created_at DESC, event_id DESC) AS rn
  FROM `{{PROJECT}}.gdgoc_raw.raw_disaster_status`
)
WHERE rn = 1;

-- 18. いまつながっている家族（記録と利用者の組ごとの最新の行が link のもの。使いやすさの改修の計画.md 10章）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.stg_family_links` AS
SELECT
  survivor_id,
  user_id,
  via,
  created_at AS linked_at,
  created_by AS linked_by
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY survivor_id, user_id ORDER BY created_at DESC, event_id DESC) AS rn
  FROM `{{PROJECT}}.gdgoc_raw.raw_family_links`
)
WHERE rn = 1 AND action = "link";
