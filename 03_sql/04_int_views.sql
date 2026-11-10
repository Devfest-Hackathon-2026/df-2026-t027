-- staging（中間集計）の5ビュー（要件定義書7-3-3、開発手順書 Step 2-1）
--
-- stg_ のビューを被災者ごとにまとめた中間集計。一覧・ホームの表示と mart の両方で使う。
-- 日付の計算は日本時間（Asia/Tokyo）でそろえる。
-- 何度流しても同じ状態になる（CREATE OR REPLACE VIEW）。

-- 1. 被災者ごとの訪問・見守り
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.int_survivor_visits` AS
SELECT
  org_id,
  survivor_id,
  MAX(visited_at) AS last_visited_at,
  DATE_DIFF(
    CURRENT_DATE("Asia/Tokyo"),
    DATE(MAX(visited_at), "Asia/Tokyo"),
    DAY
  ) AS days_since_last_visit,
  COUNTIF(visited_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)) AS visits_30d,
  COUNTIF(is_pending) AS pending_proposals
FROM `{{PROJECT}}.gdgoc_staging.stg_visits`
GROUP BY org_id, survivor_id;

-- 2. 被災者ごとの相談（未対応の数と、未対応の中で一番高い急ぎの度合い）
--    被災者のIDだけでまとめる（追加修正②⑦）。組織がないときに送った相談も、本人が組織を
--    変える前に送った相談も、本人の記録のいまの組織の相談として数える（raw は書き換えない）。
--    org_id は、送った時点の組織のうち一番大きい値（並べ替えには使わない。参考の列）。
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.int_survivor_consultations` AS
SELECT
  MAX(org_id) AS org_id,
  survivor_id,
  COUNTIF(is_unanswered) AS open_consultations,
  CASE
    MAX(
      IF(
        is_unanswered,
        CASE urgency WHEN "high" THEN 3 WHEN "medium" THEN 2 WHEN "low" THEN 1 ELSE 0 END,
        NULL
      )
    )
    WHEN 3 THEN "high"
    WHEN 2 THEN "medium"
    WHEN 1 THEN "low"
    ELSE NULL
  END AS max_open_urgency,
  MAX(consulted_at) AS last_consulted_at
FROM `{{PROJECT}}.gdgoc_staging.stg_consultations`
GROUP BY survivor_id;

-- 3. 被災者ごとの支援（示した数・未申請の数・一番近い期限）
--    相談と同じく、被災者のIDだけでまとめる（追加修正②⑦。組織がないときに示した
--    全国共通の制度も、組織につないだあとの知らせに入る）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.int_survivor_supports` AS
SELECT
  MAX(org_id) AS org_id,
  survivor_id,
  COUNT(*) AS suggested_count,
  COUNTIF(status = "not_applied") AS unapplied_count,
  MIN(IF(status = "not_applied", deadline, NULL)) AS nearest_unapplied_deadline,
  DATE_DIFF(
    MIN(IF(status = "not_applied", deadline, NULL)),
    CURRENT_DATE("Asia/Tokyo"),
    DAY
  ) AS days_to_nearest_deadline
FROM `{{PROJECT}}.gdgoc_staging.stg_supports_latest`
GROUP BY survivor_id;

-- 4. 被災者ごとの有効な担当
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.int_survivor_assignees` AS
SELECT
  org_id,
  survivor_id,
  ARRAY_AGG(DISTINCT supporter_user_id IGNORE NULLS ORDER BY supporter_user_id) AS assignee_user_ids,
  COUNT(DISTINCT supporter_user_id) AS assignee_count
FROM `{{PROJECT}}.gdgoc_staging.stg_active_assignments`
GROUP BY org_id, survivor_id;

-- 5. 一覧・ホームに使う被災者の一覧
--    並べ替えの順：悪化の兆しの度合い（高→低）、未対応の相談の急ぎの度合い（高→低）、
--    最後の訪問からの日数（長い順。1度も訪問がない人を先に出す）
CREATE OR REPLACE VIEW `{{PROJECT}}.gdgoc_staging.int_survivor_list` AS
SELECT
  s.survivor_id,
  s.org_id,
  -- 版の番号。居場所・段階を変えるときに「次の版」を決めるのに使う（F1）
  s.version,
  s.full_name,
  s.age,
  s.household_size,
  s.contact,
  s.stage,
  s.location_type,
  s.location_detail,
  -- 住んでいる市区町村と住所（要望8）。ここに無いと、版を足すときに引き継がれず消える
  s.municipality_code,
  s.address,
  s.self_user_id,
  s.registered_via,
  -- 代わりの登録での本人の同意（使いやすさの改修の計画.md 10章）。ここに無いと版を足すときに消える
  s.proxy_consent,
  s.registered_at,
  s.updated_at,
  a.level AS risk_level,
  a.evidence AS risk_evidence,
  a.next_action AS risk_next_action,
  a.source AS risk_source,
  a.assessed_at AS risk_assessed_at,
  v.last_visited_at,
  v.days_since_last_visit,
  IFNULL(v.visits_30d, 0) AS visits_30d,
  IFNULL(v.pending_proposals, 0) AS pending_proposals,
  IFNULL(c.open_consultations, 0) AS open_consultations,
  c.max_open_urgency,
  c.last_consulted_at,
  IFNULL(p.suggested_count, 0) AS suggested_count,
  IFNULL(p.unapplied_count, 0) AS unapplied_count,
  p.nearest_unapplied_deadline,
  p.days_to_nearest_deadline,
  IFNULL(g.assignee_user_ids, ARRAY<STRING>[]) AS assignee_user_ids,
  IFNULL(g.assignee_count, 0) AS assignee_count,
  -- 家族としてつながっている利用者（使いやすさの改修の計画.md 10章）。見てよいかの判定に使う
  IFNULL(f.family_user_ids, ARRAY<STRING>[]) AS family_user_ids,
  (v.last_visited_at IS NULL) AS never_visited,
  -- 並べ替え用の順位（画面側でも同じ順に並べられるようにする）
  CASE a.level WHEN "high" THEN 3 WHEN "medium" THEN 2 WHEN "low" THEN 1 ELSE 0 END AS risk_rank,
  CASE c.max_open_urgency WHEN "high" THEN 3 WHEN "medium" THEN 2 WHEN "low" THEN 1 ELSE 0 END AS open_urgency_rank,
  IFNULL(v.days_since_last_visit, 99999) AS days_since_last_visit_sort
FROM `{{PROJECT}}.gdgoc_staging.stg_survivors` AS s
LEFT JOIN `{{PROJECT}}.gdgoc_staging.stg_assessment_latest` AS a
  ON a.survivor_id = s.survivor_id AND a.org_id = s.org_id
LEFT JOIN `{{PROJECT}}.gdgoc_staging.int_survivor_visits` AS v
  ON v.survivor_id = s.survivor_id AND v.org_id = s.org_id
-- 相談と支援は被災者のIDだけでつなぐ（追加修正②⑦。本人の記録のいまの組織のものとして数える）
LEFT JOIN `{{PROJECT}}.gdgoc_staging.int_survivor_consultations` AS c
  ON c.survivor_id = s.survivor_id
LEFT JOIN `{{PROJECT}}.gdgoc_staging.int_survivor_supports` AS p
  ON p.survivor_id = s.survivor_id
LEFT JOIN `{{PROJECT}}.gdgoc_staging.int_survivor_assignees` AS g
  ON g.survivor_id = s.survivor_id AND g.org_id = s.org_id
-- 家族は組織がなくてもつながるので、被災者のIDだけでつなぐ（使いやすさの改修の計画.md 10章）
LEFT JOIN (
  SELECT
    survivor_id,
    ARRAY_AGG(DISTINCT user_id ORDER BY user_id) AS family_user_ids
  FROM `{{PROJECT}}.gdgoc_staging.stg_family_links`
  GROUP BY survivor_id
) AS f
  ON f.survivor_id = s.survivor_id
ORDER BY risk_rank DESC, open_urgency_rank DESC, days_since_last_visit_sort DESC;
