-- raw（生データ）の18表（16表は要件定義書7-3-1、開発手順書 Step 2-1）
-- 17番目の raw_disaster_status は使いやすさの改修の計画.md 8章、18番目の raw_family_links は10章で足した表
--
-- 作り方（要件定義書10-4）
--   - 1つの出来事を1行として追記するだけ。上書きと削除はしない。
--   - 共通の列：event_id・org_id・created_at・created_by・created_role。
--     被災者に関わる表には survivor_id も持たせる。
--   - 版を持つ表には version を持たせる（raw_survivors・raw_visits・raw_health_versions・
--     raw_facilities・raw_programs・raw_disaster_status）。
--   - 発生日時の日付で分割し、組織のID（と被災者のID）でクラスタ化する。読む量を減らし費用を抑えるため。
-- 何度流しても同じ状態になる（CREATE TABLE IF NOT EXISTS）。
-- {{PROJECT}} は 04_scripts/run_sql.py が .env の値に置き換える。場所はデータセットに従う（01_datasets.sql）。

-- 1. 組織（初期設定で登録。参加コードはハッシュで持つ）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_organizations` (
  event_id STRING NOT NULL OPTIONS (description = "出来事のID（UUID）"),
  org_id STRING NOT NULL OPTIONS (description = "組織のID"),
  created_at TIMESTAMP NOT NULL OPTIONS (description = "発生日時"),
  created_by STRING NOT NULL OPTIONS (description = "操作した利用者のID（初期設定は system）"),
  created_role STRING NOT NULL OPTIONS (description = "操作した人の立場（survivor・professional・volunteer・system）"),
  org_name STRING OPTIONS (description = "組織の名前（例：市町の名前）"),
  prefecture STRING OPTIONS (description = "都道府県"),
  -- 組織が受け持つ市区町村の団体コード（6桁。要望8）。本人の登録で、住んでいる
  -- 市区町村から組織を決めるのに使う（services/organizations.py の fetch_by_municipality）。
  municipality_code STRING OPTIONS (description = "受け持つ市区町村の団体コード（6桁。要望8）"),
  pro_join_code_hash STRING OPTIONS (description = "専門職用の参加コードのハッシュ"),
  vol_join_code_hash STRING OPTIONS (description = "一般の支援者用の参加コードのハッシュ"),
  -- 参加コードの元の値（追加修正⑥）。組織のオーナーだけに返す。照合はハッシュで行う
  pro_join_code STRING OPTIONS (description = "専門職用の参加コード（元の値。オーナーだけに返す。追加修正⑥）"),
  vol_join_code STRING OPTIONS (description = "一般の支援者用の参加コード（元の値。オーナーだけに返す。追加修正⑥）")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id
OPTIONS (description = "組織。F0の参加コードの確認に使う");

-- 2. 利用者の登録と変更（F0）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_users` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  user_id STRING NOT NULL OPTIONS (description = "利用者のID（Firebase Authentication の uid）"),
  role STRING OPTIONS (description = "立場（survivor・professional・volunteer・facility_staff）"),
  profession STRING OPTIONS (description = "職種（保健師・ケアマネジャー・相談員・その他。専門職だけ）"),
  display_name STRING OPTIONS (description = "表示名"),
  login_method STRING OPTIONS (description = "ログインの方法（google・phone）。電話番号そのものは保存しない"),
  linked_survivor_id STRING OPTIONS (description = "本人の場合、自分の被災者の記録のID"),
  -- 支援者の連絡先・活動する市区町村・住所（追加修正④。すでにある表には 07_migrations.sql で足す）
  contact STRING OPTIONS (description = "支援者の連絡先（電話番号など。追加修正④）"),
  municipality_code STRING OPTIONS (description = "支援者が活動する市区町村の団体コード（6桁。追加修正④）"),
  address STRING OPTIONS (description = "支援者の住所（町名・番地など。任意。追加修正④）"),
  -- 組織の中の権限（追加修正⑥）。立場とは別に持つ
  org_permission STRING OPTIONS (description = "組織の中の権限（owner か空。追加修正⑥）"),
  -- 施設の職員の、自分の施設（使いやすさの改修の計画.md 9章。すでにある表には 08_migrations.sql で足す）
  facility_id STRING OPTIONS (description = "施設の職員の場合、自分の施設のID（9章）")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, user_id
OPTIONS (description = "利用者の登録と変更（F0）");

-- 3. 被災者の記録の版（基本の情報・段階・居場所が変わるたびに1行。F1）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_survivors` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL OPTIONS (description = "被災者のID"),
  version INT64 NOT NULL OPTIONS (description = "版の番号（1から増える）"),
  full_name STRING OPTIONS (description = "氏名"),
  age INT64 OPTIONS (description = "年齢"),
  household_size INT64 OPTIONS (description = "世帯の人数"),
  contact STRING OPTIONS (description = "連絡先（任意）"),
  stage STRING OPTIONS (description = "段階（evacuation＝③・temporary＝④・rebuilding＝⑤）"),
  location_type STRING OPTIONS (description = "居場所の種類（避難所・福祉避難所・自宅ほか）"),
  location_detail STRING OPTIONS (description = "具体的な場所（避難所名・施設名・地区など）"),
  self_user_id STRING OPTIONS (description = "本人の利用者ID（本人とつながるまでは空）"),
  registered_via STRING OPTIONS (description = "誰が作ったか（professional・self・proxy＝家族などが代わりに登録）"),
  -- 住んでいる市区町村と住所（要望8。今の居場所＝location_typeとは別。住所は任意）
  municipality_code STRING OPTIONS (description = "住んでいる市区町村の団体コード（6桁。要望8）"),
  address STRING OPTIONS (description = "住所（町名・番地など。任意。要望8）"),
  -- 代わりの登録での本人の同意（使いやすさの改修の計画.md 10章。すでにある表には 09_migrations.sql で足す）
  proxy_consent STRING OPTIONS (description = "代わりの登録での本人の同意（obtained＝同意を得た・unable＝同意できない状態。proxy のときだけ）")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id
OPTIONS (description = "被災者の記録の版（F1）。上書きせず版を足す");

-- 4. 確認コードの発行と使用（F0・F1）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_link_codes` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  code_hash STRING NOT NULL OPTIONS (description = "確認コードのハッシュ。コードそのものは保存しない"),
  action STRING NOT NULL OPTIONS (description = "issued（発行）・used（使用）・failed（入力の誤り）"),
  expires_at TIMESTAMP OPTIONS (description = "有効期限（発行のときだけ）")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, code_hash
OPTIONS (description = "確認コードの発行と使用（F0・F1）");

-- 5. 担当の割り当て（F1）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_assignment_events` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  assignment_id STRING NOT NULL OPTIONS (description = "割り当てのID"),
  supporter_user_id STRING OPTIONS (description = "担当になる一般の支援者のID"),
  action STRING NOT NULL OPTIONS (description = "assigned・approved・verbal_consent・removed")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, assignment_id
OPTIONS (description = "担当の割り当てと本人の承認（F1）");

-- 6. 訪問・見守りの記録（直すときは同じ visit_id で版を足す。F2）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_visits` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  visit_id STRING NOT NULL OPTIONS (description = "記録のID"),
  version INT64 NOT NULL OPTIONS (description = "版の番号"),
  visited_at TIMESTAMP OPTIONS (description = "訪問した日時"),
  raw_text STRING OPTIONS (description = "入力した文章"),
  location STRING OPTIONS (description = "整えた項目：今の居場所。書かれていない項目は「記載なし」"),
  condition STRING OPTIONS (description = "整えた項目：体調"),
  meal STRING OPTIONS (description = "整えた項目：食事"),
  sleep STRING OPTIONS (description = "整えた項目：睡眠"),
  medication STRING OPTIONS (description = "整えた項目：服薬"),
  environment STRING OPTIONS (description = "整えた項目：生活の環境"),
  concerns STRING OPTIONS (description = "整えた項目：困りごと"),
  change STRING OPTIONS (description = "整えた項目：前回からの変化"),
  next_action STRING OPTIONS (description = "整えた項目：次に必要な対応"),
  location_proposal JSON OPTIONS (description = "居場所の更新の案"),
  health_proposal JSON OPTIONS (description = "健康・介護の更新の案"),
  proposal_status STRING OPTIONS (description = "更新の案の状況（none・applied・pending）"),
  ai_used BOOL OPTIONS (description = "Gemini で整えたか（false は手入力）")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, visit_id
OPTIONS (description = "訪問・見守りの記録の版（F2）");

-- 7. 確認待ちの更新の案を専門職が反映した記録（F2）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_update_applies` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  visit_id STRING NOT NULL OPTIONS (description = "元の記録のID"),
  applied_items ARRAY<STRING> OPTIONS (description = "反映した内容（location・health）"),
  result STRING NOT NULL OPTIONS (description = "applied（反映した）・rejected（反映しない）")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, visit_id
OPTIONS (description = "更新の案の反映の記録（F2）");

-- 8. 健康・介護の情報の版（F3）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_health_versions` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  version INT64 NOT NULL OPTIONS (description = "版の番号"),
  chronic_diseases ARRAY<STRING> OPTIONS (description = "持病"),
  medications ARRAY<STRING> OPTIONS (description = "飲んでいる薬"),
  allergies STRING OPTIONS (description = "アレルギー"),
  primary_clinic STRING OPTIONS (description = "かかりつけの医療機関"),
  medical_needs ARRAY<STRING> OPTIONS (description = "必要な医療（透析・在宅酸素・たんの吸引など）"),
  care_level STRING OPTIONS (description = "要介護度"),
  assistance_mobility STRING OPTIONS (description = "移動で必要な介助"),
  assistance_meal STRING OPTIONS (description = "食事で必要な介助"),
  assistance_toilet STRING OPTIONS (description = "排せつで必要な介助"),
  dementia_notes STRING OPTIONS (description = "認知症の有無と配慮すること"),
  family_contact STRING OPTIONS (description = "連絡してほしい家族など"),
  source STRING OPTIONS (description = "誰の情報か（professional・self・visit_proposal）")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id
OPTIONS (description = "健康・介護の情報の版（F3）。要配慮個人情報を含む");

-- 9. 引き継ぎシート（F3）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_handovers` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  handover_id STRING NOT NULL OPTIONS (description = "引き継ぎシートのID"),
  destination STRING OPTIONS (description = "移る先"),
  health_version INT64 OPTIONS (description = "載せた健康・介護の情報の版"),
  summary STRING OPTIONS (description = "直近の記録と相談の要約"),
  cautions STRING OPTIONS (description = "気をつけること"),
  ai_used BOOL OPTIONS (description = "Gemini で要約を作ったか")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, handover_id
OPTIONS (description = "引き継ぎシート（F3）");

-- 10. 悪化の兆しの判断（F4）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_assessments` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  assessment_id STRING NOT NULL OPTIONS (description = "判断のID"),
  level STRING OPTIONS (description = "度合い（high・medium・low）"),
  evidence JSON OPTIONS (description = "根拠（記録や相談の記述と、その記録・相談のID）"),
  next_action STRING OPTIONS (description = "次に必要な対応"),
  source STRING OPTIONS (description = "ai（Gemini の判断）・professional（専門職が変えた）"),
  override_reason STRING OPTIONS (description = "専門職が変えた理由"),
  trigger_id STRING OPTIONS (description = "判断のきっかけになった記録か相談のID")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, assessment_id
OPTIONS (description = "悪化の兆しの判断（F4）。本人には返さない");

-- 11. 施設の登録と更新（F5）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_facilities` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  facility_id STRING NOT NULL OPTIONS (description = "施設のID"),
  version INT64 NOT NULL OPTIONS (description = "版の番号"),
  facility_name STRING OPTIONS (description = "施設の名前"),
  facility_type STRING OPTIONS (description = "種類（福祉避難所・介護施設・病院など）"),
  address STRING OPTIONS (description = "場所"),
  vacancies INT64 OPTIONS (description = "空きの数"),
  capabilities ARRAY<STRING> OPTIONS (description = "対応できる介護・医療（透析・たんの吸引・認知症・車いすなど）"),
  water_outage BOOL OPTIONS (description = "断水しているか"),
  power_outage BOOL OPTIONS (description = "停電しているか"),
  -- 施設の参加コード（使いやすさの改修の計画.md 9章。すでにある表には 08_migrations.sql で足す）。
  -- 施設の職員が入るためのコード。照合はハッシュで行い、元の値はその組織の専門職だけに返す
  staff_join_code_hash STRING OPTIONS (description = "施設の職員用の参加コードのハッシュ（9章）"),
  staff_join_code STRING OPTIONS (description = "施設の職員用の参加コード（元の値。その組織の専門職だけに返す。9章）"),
  -- 版を足した理由。参加コードの発行だけの版は、最終更新の時刻に数えない
  change_kind STRING OPTIONS (description = "版を足した理由（info＝空きなど・join_code＝参加コードの発行。空は info。9章）")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, facility_id
OPTIONS (description = "施設の登録と更新の版（F5）。実在の施設の空き情報は入れない");

-- 12. 移る先の決定（F5）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_placements` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  placement_id STRING NOT NULL OPTIONS (description = "決定のID"),
  candidates JSON OPTIONS (description = "Gemini が出した候補の施設のIDと理由（最大3件）"),
  chosen_facility_id STRING OPTIONS (description = "選んだ施設のID"),
  chosen_from_candidates BOOL OPTIONS (description = "候補から選んだか"),
  chosen_reason STRING OPTIONS (description = "選んだ理由")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, placement_id
OPTIONS (description = "移る先の決定（F5）。決めるのは人");

-- 13. 支援制度の情報（初期設定で登録）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_programs` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  program_id STRING NOT NULL OPTIONS (description = "制度のID"),
  version INT64 NOT NULL OPTIONS (description = "版の番号"),
  program_name STRING OPTIONS (description = "制度の名前"),
  conditions STRING OPTIONS (description = "対象になる条件"),
  benefit STRING OPTIONS (description = "受けられる内容（金額など）"),
  apply_to STRING OPTIONS (description = "申請先"),
  deadline_rule STRING OPTIONS (description = "申請の期限の決まり"),
  documents STRING OPTIONS (description = "必要な書類"),
  source_url STRING OPTIONS (description = "出典")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, program_id
OPTIONS (description = "支援制度の情報の版（出典のある公的な情報だけ）");

-- 14. 示した支援と、申請の状況の変更（F6）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_support_events` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  support_id STRING NOT NULL OPTIONS (description = "支援の記録のID（被災者と制度の組み合わせごと）"),
  program_id STRING OPTIONS (description = "制度のID"),
  action STRING NOT NULL OPTIONS (description = "suggested（Gemini が示した）・status_changed（状況を変えた）"),
  reason STRING OPTIONS (description = "受けられそうな理由（示したとき）"),
  status STRING OPTIONS (description = "申請の状況（not_applied・applying・received・not_eligible）"),
  deadline DATE OPTIONS (description = "申請の期限"),
  housing_damage STRING OPTIONS (description = "示したときの住宅の被害の程度"),
  rebuild_intent STRING OPTIONS (description = "示したときの再建の方法の意向")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, support_id
OPTIONS (description = "示した支援と申請の状況の変更（F6）");

-- 15. 本人からの相談（F8）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_consultations` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  consultation_id STRING NOT NULL OPTIONS (description = "相談のID"),
  raw_text STRING OPTIONS (description = "本人が書いた文章"),
  category STRING OPTIONS (description = "相談の種類（体調・介護・住まい・お金と制度・生活・その他）"),
  structured JSON OPTIONS (description = "F2と同じ9つの項目に整えた内容"),
  emergency_flag BOOL OPTIONS (description = "急を要するとAIが判断したか（119番などの案内を出したか）"),
  urgency STRING OPTIONS (description = "急ぎの度合い（high・medium・low）"),
  urgency_evidence STRING OPTIONS (description = "急ぎの度合いの根拠"),
  ai_used BOOL OPTIONS (description = "Gemini で整えたか")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, consultation_id
OPTIONS (description = "本人からの相談（F8）");

-- 16. 相談への返事と状況の変更（F8）
CREATE TABLE IF NOT EXISTS `{{PROJECT}}.gdgoc_raw.raw_consultation_events` (
  event_id STRING NOT NULL,
  org_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  created_by STRING NOT NULL,
  created_role STRING NOT NULL,
  survivor_id STRING NOT NULL,
  consultation_id STRING NOT NULL OPTIONS (description = "相談のID"),
  action STRING NOT NULL OPTIONS (description = "replied（返事）・status_changed（状況を変えた）"),
  reply_text STRING OPTIONS (description = "返事"),
  next_action STRING OPTIONS (description = "次の対応"),
  status STRING OPTIONS (description = "相談の状況（open・in_progress・closed）")
)
PARTITION BY DATE(created_at)
CLUSTER BY org_id, survivor_id, consultation_id
OPTIONS (description = "相談への返事と状況の変更（F8）");

-- 17. 周りの地域の状況（使いやすさの改修の計画.md 8章。要件定義書7-3-1には無い追加の表）
-- すでにある環境には 10_migrations.sql で作る。上書き・削除はせず、直すときも終わったときも版を足す。
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

-- 18. 家族と記録のつながり（使いやすさの改修の計画.md 10章。すでにある環境には 09_migrations.sql で作る）
--     担当（raw_assignment_events）とは分ける。担当は組織の中の割り当てで、家族は組織がなくてもつながるため。
--     つなぐ・外すのたびに1行足す。いまつながっているかは stg_family_links が最新の行で決める。
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
