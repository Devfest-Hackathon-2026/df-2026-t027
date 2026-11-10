// F6 受けられる支援と申請の状況の画面（開発手順書 Step 10-1〜10-5）
//
// - **使えるのは本人と専門職だけ。** 一般の支援者には入口もカードも出さない
//   （要件定義書446行「支援制度の候補（F6）は扱わない」・601行の「見られる人」）。
// - 制度の中身（対象になる条件・受けられる内容・申請先・期限の決まり・必要な書類・出典）は
//   **APIが返す値をそのまま出す**。AIに書かせていない（要件定義書F6）。
// - 申請の状況のラベル・「次にすること」・期限の知らせ・断り書きも **APIが返すものだけ**を出す
//   （文言の正本は `services/supports.py`。画面に書き写すとずれる。Step 8・9 と同じ形）。
// - **本人向けの画面は文字を大きくし**（`.large-text`）、言い方を変え、分からないことを
//   相談できるように F8 への入口を出す（要件定義書F6）。
// - ホームと一覧の「期限の近い未申請」は、**一覧の応答（`support_alerts`）から作る**。
//   F6 のための読みを増やさない（Step 7 の「対応が必要な人」と同じ作り）。
// - 氏名・制度の名前をコンソールに出さない（要件定義書11-3）。

import { apiFetch, getConfig, readError } from "./auth.js";
import {
  clear,
  el,
  fillSelect,
  formatDate,
  formatDateTime,
  node,
  setTileCount,
  showFormError,
  showNotice,
  showScreen,
  withBusy,
} from "./ui.js";

const ROLE_PROFESSIONAL = "professional";
const ROLE_SURVIVOR = "survivor";

// 画面の見出しと説明。**本人向けは言い方を変える**（要件定義書F6「専門用語を言い換え」）。
// ここにあるのは画面の案内文だけで、制度の中身・状況・次にすること・断り書きは API が返す。
const WORDING = {
  professional: {
    title: "受けられる支援と申請の状況",
    formTitle: "1. 住宅の被害の程度・世帯の人数・再建の意向を入れる",
    damageLabel: "住宅の被害の程度",
    householdLabel: "世帯の人数（記録に無いため、調べるのに使うだけです）",
    householdNote: "記録の値です。違うときは、記録の「基本の情報」で直してください。",
    intentLabel: "再建の方法の意向",
    candidatesTitle: "2. 受けられそうな支援（AIが出した案）",
    programsNote: "候補に出なかった制度も、ご自分の判断で選べます。",
    recordedTitle: "記録した支援と申請の状況",
    recordedNote: "申請の状況を変えると、ご本人の画面にも反映されます。前の状況も記録に残ります。",
    recordedEmpty: "まだ記録した支援はありません。",
  },
  survivor: {
    title: "受けられる支援",
    formTitle: "1. いまの状況を入れてください",
    damageLabel: "お住まいの壊れ方",
    householdLabel: "世帯の人数（ご自分を含む）",
    householdNote: "登録の内容です。違うときは、プロフィールで直せます。",
    intentLabel: "これからのお住まいをどうしたいか",
    candidatesTitle: "2. 受けられそうな支援",
    programsNote: "上に出なかったものも、ここから選べます。",
    recordedTitle: "申請の進み具合",
    recordedNote: "申請の状況を選んで直せます。分からないことは「相談する」から聞けます。",
    recordedEmpty: "まだ支援の記録はありません。上の欄から調べてみてください。",
  },
};

let me = null;
let detail = null;
let openDetail = () => Promise.resolve();
let openConsultation = () => {};
let openProfile = () => Promise.resolve();

// いま開いている画面の状態（記録するときにそのまま送る）。
let view = {
  survivorId: "",
  candidates: [],
  programs: [],
  recorded: [],
  // 選べる行（チェック・理由・期限の入力欄を持つ。IDは付けない）
  choices: [],
};

/** 立場を受け取る（ホームから渡される）。 */
export function setMe(value) {
  me = value;
  if (!value) {
    detail = null;
    view = { survivorId: "", candidates: [], programs: [], recorded: [], choices: [] };
  }
}

function choices() {
  return getConfig()?.choices || {};
}

function isProfessional() {
  return me?.role === ROLE_PROFESSIONAL;
}

function isSurvivor() {
  return me?.role === ROLE_SURVIVOR;
}

/** 本人か専門職か（一般の支援者は F6 を扱わない）。 */
function canUse() {
  return isProfessional() || isSurvivor();
}

function wording() {
  return WORDING[isSurvivor() ? ROLE_SURVIVOR : ROLE_PROFESSIONAL];
}

/** いま開いている記録のID（本人は自分の記録）。 */
function survivorId() {
  return detail?.survivor?.survivor_id || me?.survivor_id || "";
}

// --- ホームと一覧の「期限の近い未申請」 ---

// 期日での絞り込み（使いやすさの改修の計画.md 11章）。日数は「期限までの日数」（期限切れは負）。
// 3日・7日・30日の絞り込みでも、期限切れは見落とさないよう上に出す。
export const DEADLINE_FILTERS = [
  { key: "overdue", label: "期限切れ", test: (days) => days < 0 },
  { key: "3", label: "3日以内", test: (days) => days <= 3 },
  { key: "7", label: "7日以内", test: (days) => days <= 7 },
  { key: "30", label: "30日以内", test: (days) => days <= 30 },
  { key: "all", label: "すべて", test: () => true },
];
const DEFAULT_DEADLINE_FILTER = "30";

/** 期限までの日数が、選んだ絞り込みに入るか。 */
export function matchesDeadline(days, key) {
  if (!key) return true;
  if (typeof days !== "number") return false;
  const filter = DEADLINE_FILTERS.find((item) => item.key === key);
  return filter ? filter.test(days) : true;
}

// カードごとに選んでいる絞り込み（ホームと一覧で別々に覚える）
const alertFilters = new Map();
let lastAlerts = null;

/** 絞り込みのボタンを並べる（押すと、そのカードの一覧だけを描き直す）。 */
export function renderDeadlineChips(container, { selected, counts, onSelect }) {
  clear(container);
  for (const filter of DEADLINE_FILTERS) {
    const count = counts ? counts[filter.key] : null;
    const chip = node(
      "button",
      "chip",
      count === null || count === undefined ? filter.label : `${filter.label}（${count}）`,
    );
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(filter.key === selected));
    chip.addEventListener("click", () => onSelect(filter.key));
    container.append(chip);
  }
}

function deadlineCounts(items) {
  const counts = {};
  for (const filter of DEADLINE_FILTERS) {
    counts[filter.key] = items.filter((item) => filter.test(item.days_to_nearest_deadline)).length;
  }
  return counts;
}

/** 一覧の応答（`support_alerts`）から、ホームと一覧の知らせを描く。
 *
 *  **読みは増えない**（`/api/survivors` の応答をそのまま使う）。
 *  一般の支援者には出さない（要件定義書446行。応答にも入らない）。 */
export function renderSupportAlerts(body) {
  const alerts = isProfessional() ? body?.support_alerts : null;
  lastAlerts = alerts;
  // ホームのタイルの数（期限まで30日以内の人）
  setTileCount("support-alerts", alerts?.count || 0);
  renderAlertCards();
}

function renderAlertCards() {
  fillAlerts("home", lastAlerts);
  fillAlerts("survivors", lastAlerts);
}

function fillAlerts(prefix, alerts) {
  const card = el(prefix === "home" ? "home-support-alerts-card" : "survivors-support-alerts-card");
  const countNode = el(prefix === "home" ? "home-support-alerts-count" : "survivors-support-alerts-count");
  const listNode = el(prefix === "home" ? "home-support-alerts" : "survivors-support-alerts");
  const noteNode = el(prefix === "home" ? "home-support-alerts-note" : "survivors-support-alerts-note");
  const chips = el(prefix === "home" ? "home-support-alerts-filters" : "survivors-support-alerts-filters");
  if (!alerts) {
    card.hidden = true;
    return;
  }
  // 「すべて」のために、30日より先の未申請も受け取っている（all_survivors）
  const all = alerts.all_survivors || alerts.survivors || [];
  const selected = alertFilters.get(prefix) || DEFAULT_DEADLINE_FILTER;
  renderDeadlineChips(chips, {
    selected,
    counts: deadlineCounts(all),
    onSelect: (key) => {
      alertFilters.set(prefix, key);
      fillAlerts(prefix, alerts);
    },
  });
  chips.hidden = all.length === 0;
  const items = all.filter((item) => matchesDeadline(item.days_to_nearest_deadline, selected));
  clear(listNode);
  countNode.textContent =
    all.length === 0
      ? "期限が近い未申請の支援はありません。"
      : items.length === 0
        ? "この絞り込みに当てはまる方はいません。"
        : `${items.length}人`;
  for (const item of items) {
    const li = node("li", "attention");
    const button = node("button", "link-button", `${item.full_name}（未申請${item.unapplied_count}件）`);
    button.type = "button";
    button.addEventListener("click", () => {
      openDetail(item.survivor_id).catch((err) => showNotice(err.message));
    });
    li.append(button);
    // 期限の知らせの文言は API が返すものを出す
    if (item.notice) li.append(node("span", "row-note", item.notice));
    if (item.nearest_unapplied_deadline) {
      li.append(
        node("span", "row-note", `一番近い期限：${formatDate(item.nearest_unapplied_deadline)}`),
      );
    }
    listNode.append(li);
  }
  noteNode.textContent = alerts.disclaimer || "";
  card.hidden = false;
}

// --- 詳細の画面の「受けられる支援」 ---

/** 詳細の画面に F6 のカードを描く（f1.js が詳細を描いたあとに呼ばれる）。
 *
 *  一般の支援者には**カードごと出さない**。応答の `supports` は空の配列で返るため、
 *  「中身が無い」ではなく**立場で隠す**（空の配列と、扱えないことを区別する）。 */
export function renderDetailSupports(detailBody) {
  detail = detailBody;
  const card = el("detail-supports-card");
  if (!detailBody?.survivor || !canUse()) {
    card.hidden = true;
    return;
  }
  const rows = detailBody.survivor.supports || [];
  const list = el("detail-supports");
  clear(list);
  if (rows.length === 0) {
    list.append(node("li", "note", wording().recordedEmpty));
  } else {
    for (const row of rows) list.append(renderRecorded(row, { withForm: false }));
  }
  el("detail-supports-note").textContent = isSurvivor()
    ? "受けられそうな支援と、申請の進み具合を見られます。"
    : "示した支援と申請の状況です。期限の近い未申請は、ホームと一覧にも出ます。";
  el("supports-open").hidden = false;
  card.hidden = false;
}

// --- F6 の画面 ---

/** 「受けられる支援を調べる」を押したとき（詳細からも、本人のホームからも開ける）。 */
export function openSupports() {
  showNotice("");
  if (!canUse()) {
    showNotice("受けられる支援を扱えるのは、ご本人と専門職だけです");
    return;
  }
  const id = survivorId();
  if (!id) {
    showNotice("記録が開かれていません。一覧から開き直してください");
    return;
  }
  const text = wording();
  const survivor = detail?.survivor;
  view = {
    survivorId: id,
    candidates: [],
    programs: [],
    recorded: survivor?.supports || [],
    choices: [],
  };

  el("supports-title").textContent = text.title;
  el("supports-subtitle").textContent =
    survivor && !isSurvivor() ? `${survivor.full_name}さんの支援を調べます` : "";
  // 断り書きは API が返す文言（選択肢のAPIにも入っている）
  el("supports-disclaimer").textContent = choices().support_disclaimer || "";
  el("supports-form-title").textContent = text.formTitle;
  el("supports-damage-label").textContent = text.damageLabel;
  el("supports-household-label").textContent = text.householdLabel;
  el("supports-intent-label").textContent = text.intentLabel;
  el("supports-candidates-title").textContent = text.candidatesTitle;
  el("supports-programs-note").textContent = text.programsNote;
  el("supports-recorded-title").textContent = text.recordedTitle;
  el("supports-recorded-note").textContent = text.recordedNote;

  // 本人向けは文字を大きくする（要件定義書F6）
  el("screen-supports").classList.toggle("large-text", isSurvivor());
  // 分からないことを相談できる入口は本人にだけ出す（F8）
  el("supports-to-consultation").hidden = !isSurvivor();

  fillSelect(el("support-housing-damage"), choices().housing_damages || [], {
    placeholder: "選んでください",
  });
  fillSelect(el("support-rebuild-intent"), choices().rebuild_intents || [], {
    placeholder: "選んでください",
  });
  // 世帯の人数は記録の値を出すだけにする（使いやすさの改修の計画.md 6-4）。
  // 記録に無いときだけ欄を出す。ここで入れた値は調べるのに使うだけで、記録は変えない
  const household = survivor?.household_size;
  const hasHousehold = household !== null && household !== undefined && household !== "";
  el("support-household-record").hidden = !hasHousehold;
  el("support-household-field").hidden = hasHousehold;
  el("support-household-value").textContent = hasHousehold ? `${household}人` : "";
  el("support-household-note").textContent = text.householdNote;
  el("support-household-edit").hidden = !(hasHousehold && isSurvivor());
  el("support-household").value = "";
  el("supports-uses-consultations").textContent =
    "住宅の被害と、これまでの相談の内容をもとに調べます。";
  showFormError(el("form-support-conditions"), "");
  el("supports-notice").hidden = true;
  el("support-candidates-card").hidden = true;
  showScreen("screen-supports");
  renderRecordedList();
}

function numberOrNull(value) {
  const text = (value || "").trim();
  if (!text) return null;
  const parsed = Number(text);
  return Number.isInteger(parsed) ? parsed : null;
}

async function submitConditions(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const id = view.survivorId || survivorId();
  const body = {
    housing_damage: el("support-housing-damage").value,
    rebuild_intent: el("support-rebuild-intent").value,
    // 記録に世帯の人数があるときは送らない（サーバーも記録の値を使う）
    household_size: el("support-household-field").hidden
      ? null
      : numberOrNull(el("support-household").value),
  };
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(
      `/api/survivors/${encodeURIComponent(id)}/support-candidates`,
      { method: "POST", body: JSON.stringify(body) },
    );
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    renderCandidates(await res.json());
  });
}

function renderCandidates(body) {
  view.survivorId = body.survivor_id || view.survivorId;
  view.candidates = body.candidates || [];
  view.programs = body.programs || [];
  view.recorded = body.supports || [];
  if (body.disclaimer) el("supports-disclaimer").textContent = body.disclaimer;

  // AI を呼べなかったとき・制度が無いときの知らせ（文言は API が返す）
  const notice = el("supports-notice");
  notice.textContent = body.notice || "";
  notice.hidden = !body.notice;
  // 調べるのに使った相談の数（使いやすさの改修の計画.md 6-3）
  el("supports-uses-consultations").textContent = body.consultation_count
    ? `住宅の被害と、これまでの相談${body.consultation_count}件の内容をもとに調べました。`
    : "住宅の被害と、これまでの相談の内容をもとに調べます（相談はまだありません）。";

  renderChoiceLists();
  renderRecordedList();
  el("support-candidates-card").hidden = false;
  el("support-candidates-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

/** 候補と、候補に出なかった制度を描く（どちらも同じ形で選べる）。 */
function renderChoiceLists() {
  view.choices = [];
  const recordedIds = new Set(view.recorded.map((row) => row.program_id));

  const list = el("support-candidates");
  clear(list);
  const candidates = view.candidates.filter((item) => !recordedIds.has(item.program_id));
  el("support-candidates-count").textContent =
    candidates.length === 0
      ? "選べる候補はありません。下の一覧から選べます。"
      : `${candidates.length}件`;
  for (const item of candidates) list.append(renderChoice(item));

  // 候補に出なかった制度（すでに記録したものと、候補に出たものは除く）
  const shown = new Set(candidates.map((item) => item.program_id));
  const others = view.programs.filter(
    (item) => !shown.has(item.program_id) && !recordedIds.has(item.program_id),
  );
  const programs = el("support-programs");
  clear(programs);
  if (others.length === 0) {
    programs.append(node("li", "note", "ほかに選べる制度はありません。"));
  } else {
    for (const item of others) programs.append(renderChoice(item));
  }
}

/** 選べる制度1件（チェック・理由・期限の入力欄を付ける。IDは付けない）。 */
function renderChoice(item) {
  const li = node("li", "card support");
  const head = node("label", "field field-check");
  const check = document.createElement("input");
  check.type = "checkbox";
  head.append(check);
  head.append(node("span", "row-title", item.program_name || "（名前なし）"));
  // 組織に関係なく使える国の制度（追加修正②）
  if (item.nationwide) head.append(node("span", "badge badge-note", "全国共通"));
  // 相談の内容から当てはまると考えた候補（使いやすさの改修の計画.md 6-3）
  if (item.consultation_ids?.length) head.append(node("span", "badge badge-note", "相談の内容から"));
  li.append(head);

  if (item.reason) li.append(node("p", null, `理由：${item.reason}`));
  li.append(programDetails(item));

  const reasonField = node("label", "field");
  reasonField.append(node("span", "field-label", "記録に残す理由"));
  const reason = document.createElement("textarea");
  reason.rows = 2;
  reason.value = item.reason || "";
  reasonField.append(reason);
  li.append(reasonField);

  const deadlineField = node("label", "field");
  deadlineField.append(
    node("span", "field-label", "申請の期限（分かるときだけ。決まりを見て入れ直せます）"),
  );
  const deadline = document.createElement("input");
  deadline.type = "date";
  deadline.value = typeof item.deadline === "string" ? item.deadline.slice(0, 10) : "";
  deadlineField.append(deadline);
  li.append(deadlineField);

  view.choices.push({ program_id: item.program_id, check, reason, deadline });
  return li;
}

/** 制度の中身（APIが返す値をそのまま出す。AIに書かせていない）。 */
function programDetails(item) {
  const box = node("dl", "pairs");
  const pairs = [
    ["対象になる条件", item.conditions],
    ["受けられる内容", item.benefit],
    ["申請先", item.apply_to],
    ["申請の期限の決まり", item.deadline_rule],
    ["必要な書類", item.documents],
  ];
  for (const [label, value] of pairs) {
    box.append(node("dt", null, label));
    box.append(node("dd", null, value || "—"));
  }
  if (item.source_url) {
    box.append(node("dt", null, "出典"));
    const dd = node("dd");
    const link = node("a", null, item.source_url);
    link.href = item.source_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    dd.append(link);
    box.append(dd);
  }
  return box;
}

async function recordSupports(button) {
  const chosen = view.choices.filter((row) => row.check.checked);
  const error = el("support-record-error");
  error.hidden = true;
  if (chosen.length === 0) {
    error.textContent = "記録する支援を1つ以上選んでください";
    error.hidden = false;
    return;
  }
  const body = {
    housing_damage: el("support-housing-damage").value,
    rebuild_intent: el("support-rebuild-intent").value,
    items: chosen.map((row) => ({
      program_id: row.program_id,
      reason: row.reason.value,
      deadline: row.deadline.value || null,
    })),
  };
  await withBusy(button, async () => {
    const res = await apiFetch(
      `/api/survivors/${encodeURIComponent(view.survivorId)}/supports`,
      { method: "POST", body: JSON.stringify(body) },
    );
    if (!res.ok) {
      error.textContent = await readError(res);
      error.hidden = false;
      return;
    }
    const saved = await res.json();
    mergeRecorded(saved.supports || []);
    renderChoiceLists();
    renderRecordedList();
    showNotice("支援を記録しました。申請の状況は未申請から始まります。", "ok");
  });
}

/** 記録した支援を、同じ記録は新しいものに置き換えて並べる。 */
function mergeRecorded(rows) {
  const merged = new Map(view.recorded.map((row) => [row.support_id, row]));
  for (const row of rows) merged.set(row.support_id, row);
  view.recorded = [...merged.values()];
}

function renderRecordedList() {
  const list = el("support-recorded");
  clear(list);
  if (view.recorded.length === 0) {
    list.append(node("li", "note", wording().recordedEmpty));
  } else {
    for (const row of view.recorded) list.append(renderRecorded(row, { withForm: true }));
  }
  el("support-recorded-card").hidden = false;
}

/** 記録した支援1件（状況・期限・次にすること。`withForm` のときは状況を変えられる）。 */
function renderRecorded(row, { withForm }) {
  const li = node("li", "card support");
  li.append(node("h3", "row-title", row.program_name || "（名前なし）"));

  const marks = node("div", "row-marks");
  marks.append(node("span", statusClass(row.status), row.status_label));
  if (row.due_soon) marks.append(node("span", "badge badge-high", "期限が近い"));
  li.append(marks);

  // 期限の知らせと「次にすること」は API が返す文言をそのまま出す
  if (row.deadline_notice) li.append(node("p", "note", row.deadline_notice));
  if (row.next_action) li.append(node("p", null, `次にすること：${row.next_action}`));
  if (row.reason) li.append(node("p", "row-note", `理由：${row.reason}`));
  li.append(
    node(
      "p",
      "row-note",
      `申請の期限：${row.deadline ? formatDate(row.deadline) : "まだ決まっていません"}`,
    ),
  );
  li.append(programDetails(row));
  li.append(node("p", "row-note", `最後の更新：${formatDateTime(row.updated_at)}`));

  if (withForm) li.append(renderStatusForm(row));
  return li;
}

function statusClass(status) {
  if (status === "not_applied") return "badge badge-medium";
  if (status === "not_eligible") return "badge badge-note";
  if (status === "received") return "badge badge-low";
  return "badge badge-note";
}

/** 申請の状況を変える欄（支援ごとに作る。IDは付けない）。 */
function renderStatusForm(row) {
  const box = node("div", "proposal");
  box.append(node("h4", "proposal-title", "申請の状況を変える"));

  const statusField = node("label", "field");
  statusField.append(node("span", "field-label", "いまの状況"));
  const select = document.createElement("select");
  for (const item of choices().support_statuses || []) {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    select.append(option);
  }
  select.value = row.status || "";
  statusField.append(select);
  box.append(statusField);

  const deadlineField = node("label", "field");
  deadlineField.append(node("span", "field-label", "申請の期限（分かるときだけ）"));
  const deadline = document.createElement("input");
  deadline.type = "date";
  deadline.value = typeof row.deadline === "string" ? row.deadline.slice(0, 10) : "";
  deadlineField.append(deadline);
  box.append(deadlineField);

  const error = node("p", "form-error");
  error.hidden = true;
  box.append(error);

  const actions = node("div", "actions");
  const save = node("button", "button", "この内容に更新する");
  save.type = "button";
  save.addEventListener("click", () =>
    withBusy(save, () =>
      updateStatus(row.support_id, {
        status: select.value,
        deadline: deadline.value || null,
      }).catch((err) => {
        error.textContent = err.message;
        error.hidden = false;
      }),
    ),
  );
  actions.append(save);
  box.append(actions);
  return box;
}

async function updateStatus(supportId, body) {
  const res = await apiFetch(
    `/api/survivors/${encodeURIComponent(view.survivorId)}/supports/${encodeURIComponent(supportId)}`,
    { method: "PATCH", body: JSON.stringify(body) },
  );
  if (!res.ok) throw new Error(await readError(res));
  const saved = await res.json();
  if (saved.support) mergeRecorded([saved.support]);
  renderRecordedList();
  showNotice("申請の状況を変えました。前の状況も記録として残ります。", "ok");
}

// --- イベントの登録 ---

export function bind({
  openDetail: openDetailHandler,
  openConsultation: consultationHandler,
  openProfile: profileHandler,
}) {
  if (openDetailHandler) openDetail = openDetailHandler;
  if (consultationHandler) openConsultation = consultationHandler;
  if (profileHandler) openProfile = profileHandler;
  el("support-household-edit").addEventListener("click", () =>
    openProfile().catch((err) => showNotice(err.message)),
  );

  el("supports-open").addEventListener("click", () => openSupports());
  el("form-support-conditions").addEventListener("submit", submitConditions);
  el("support-record").addEventListener("click", (event) =>
    recordSupports(event.currentTarget).catch((err) => showNotice(err.message)),
  );
  el("supports-to-consultation").addEventListener("click", () => openConsultation());
}
