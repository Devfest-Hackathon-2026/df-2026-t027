// F2 訪問・見守りの記録と F3 健康・介護の情報の画面（開発手順書 Step 6-1・6-2・6-3）
//
// - **保存するまで記録にならない。** 整えた案は画面の中だけに置き、確かめて保存する（要件定義書F2）。
// - 文章に書かれていない項目は「記載なし」のままにする。空にして保存しても「記載なし」になる。
// - AIを呼べなかったときは、9つの項目の手入力の欄を出して保存できるようにする（要件定義書F2）。
// - 立場は /api/me が返すものだけを使う。反映できるのは専門職だけで、画面でも隠す
//   （隠すだけでは足りないので、バックエンドでも断っている。要件定義書10-3）。
// - **スマートフォンで入れる**ことを想定し、項目は縦1列・文字は16px以上にする（style.css）。
// - 氏名・健康の情報・記録の本文をコンソールに出さない。

import { apiFetch, getConfig, readError } from "./auth.js";
import {
  addPair,
  clear,
  el,
  fillSelect,
  formatDateTime,
  nameWithAge,
  node,
  showFormError,
  showNotice,
  showScreen,
  withBusy,
} from "./ui.js";

const ROLE_PROFESSIONAL = "professional";
const ROLE_SURVIVOR = "survivor";

const NOT_RECORDED = "記載なし";

const RECORDED_ROLE_LABELS = {
  professional: "専門職",
  volunteer: "支援者（ボランティア・ご家族）",
  survivor: "ご本人",
};

let me = null;
let detail = null;
let openDetail = async () => {};

// 画面で作った入力欄（IDを組み立てないため、要素そのものを持っておく）
const itemInputs = new Map();
const healthInputs = new Map();

// 整えた案（保存のときにそのまま送る。人が直した項目は入力欄の値が正本）
let proposals = { location: null, health: null, aiUsed: true };

/** 立場を受け取る（ホームから渡される）。 */
export function setMe(value) {
  me = value;
}

function choices() {
  return getConfig()?.choices || {};
}

function visitFields() {
  return choices().visit_fields || [];
}

function healthFields() {
  return choices().health_fields || [];
}

function careLevels() {
  return choices().care_levels || [];
}

function survivorId() {
  return detail?.survivor?.survivor_id || "";
}

function isProfessional() {
  return me?.role === ROLE_PROFESSIONAL;
}

// --- 詳細の画面に出すカード（f1.js の詳細から呼ばれる） ---

/** 詳細の画面の「健康・介護の情報」と「訪問・見守りの記録」を描く。 */
export function renderDetailSections(detailBody) {
  detail = detailBody;
  renderHealthCard();
  renderVisitsCard();
}

function renderHealthCard() {
  const card = el("detail-health-card");
  const health = detail?.survivor?.health || null;
  const canEdit = isProfessional() || me?.role === ROLE_SURVIVOR;

  const list = el("detail-health");
  clear(list);
  if (!health) {
    list.append(node("dt", null, "まだ登録がありません"));
    list.append(node("dd", null, "—"));
  } else {
    for (const field of healthFields()) {
      const value = health.fields?.[field.value];
      addPair(list, field.label, Array.isArray(value) ? value.join("、") : value);
    }
  }

  el("detail-health-source").textContent = health
    ? `${health.source_label}／更新：${formatDateTime(health.updated_at)}（版${health.version}）`
    : "持病・薬・必要な介護を同じ項目で残します。";
  el("health-edit").hidden = !canEdit;
  el("health-edit").textContent = health
    ? "健康・介護の情報を更新する"
    : "健康・介護の情報を入れる";
  el("detail-health-note").textContent = canEdit
    ? ""
    : "更新は、訪問・見守りの記録の「更新の案」を通して専門職が行います。";
  card.hidden = false;
}

function renderVisitsCard() {
  const card = el("detail-visits-card");
  const rows = detail?.survivor?.visits;
  // 本人には記録を返していない（要件定義書F4）。カードそのものを出さない
  if (!Array.isArray(rows)) {
    card.hidden = true;
    return;
  }
  const list = el("detail-visits");
  clear(list);
  if (rows.length === 0) {
    list.append(node("li", "note", "まだ記録がありません。"));
  } else {
    for (const row of rows) {
      list.append(renderVisit(row));
    }
  }
  card.hidden = false;
}

function renderVisit(row) {
  const li = node("li", "visit");

  const head = node("div", "visit-head");
  head.append(node("span", "visit-when", formatDateTime(row.visited_at)));
  head.append(
    node(
      "span",
      "badge badge-note",
      row.recorded_by_name
        ? `${row.recorded_by_name}（${RECORDED_ROLE_LABELS[row.recorded_role] || ""}）`
        : RECORDED_ROLE_LABELS[row.recorded_role] || "",
    ),
  );
  if (!row.ai_used) head.append(node("span", "badge badge-note", "手入力"));
  if (row.is_pending) head.append(node("span", "badge badge-high", "更新の案が確認待ち"));
  if (row.apply_result === "applied") head.append(node("span", "badge badge-low", "案を反映済み"));
  if (row.apply_result === "rejected") {
    head.append(node("span", "badge badge-note", "案は反映しないと判断"));
  }
  li.append(head);

  const items = node("dl", "pairs");
  for (const field of visitFields()) {
    addPair(items, field.label, row.items?.[field.value]);
  }
  li.append(items);

  if (row.raw_text) {
    const details = node("details", "visit-raw");
    details.append(node("summary", null, "入力した文章を見る"));
    details.append(node("p", null, row.raw_text));
    li.append(details);
  }

  if (row.is_pending) li.append(renderPending(row));
  return li;
}

function renderPending(row) {
  const box = node("div", "proposal");
  box.append(node("h3", "proposal-title", "確認待ちの更新の案"));

  const available = [];
  if (row.location_proposal) {
    available.push("location");
    box.append(
      node(
        "p",
        null,
        `居場所：${row.location_proposal.location_type_label || ""}${
          row.location_proposal.location_detail
            ? `（${row.location_proposal.location_detail}）`
            : ""
        }`,
      ),
    );
    if (row.location_proposal.reason) {
      box.append(node("p", "note", `理由：${row.location_proposal.reason}`));
    }
  }
  if (row.health_proposal) {
    available.push("health");
    box.append(node("p", null, "健康・介護の情報："));
    box.append(healthProposalList(row.health_proposal));
    if (row.health_proposal.reason) {
      box.append(node("p", "note", `理由：${row.health_proposal.reason}`));
    }
  }

  if (!isProfessional()) {
    box.append(node("p", "note", "専門職が確かめてから反映されます。"));
    return box;
  }

  const actions = node("div", "actions");
  const apply = node("button", "button button-primary", "この案を反映する");
  apply.type = "button";
  apply.addEventListener("click", () =>
    withBusy(apply, () => applyProposal(row.visit_id, available, "applied")),
  );
  const reject = node("button", "button", "反映しない");
  reject.type = "button";
  reject.addEventListener("click", () =>
    withBusy(reject, () => applyProposal(row.visit_id, [], "rejected")),
  );
  actions.append(apply);
  actions.append(reject);
  box.append(actions);
  return box;
}

function healthProposalList(proposal) {
  const list = node("ul", "list");
  const fields = proposal.fields || {};
  for (const field of healthFields()) {
    const value = fields[field.value];
    if (!value || (Array.isArray(value) && value.length === 0)) continue;
    list.append(
      node("li", null, `${field.label}：${Array.isArray(value) ? value.join("、") : value}`),
    );
  }
  return list;
}

async function applyProposal(visitId, items, result) {
  const id = survivorId();
  const res = await apiFetch(
    `/api/survivors/${encodeURIComponent(id)}/visits/${encodeURIComponent(visitId)}/apply`,
    { method: "POST", body: JSON.stringify({ items, result }) },
  );
  if (!res.ok) {
    showNotice(await readError(res));
    return;
  }
  // 知らせは読み直したあとに出す（詳細の読み直しが知らせを消すため）
  await openDetail(id);
  showNotice(
    result === "applied" ? "更新の案を記録に反映しました。" : "反映しないと記録しました。",
    "ok",
  );
}

// --- F2 の記録を作る画面 ---

/** 「訪問・見守りを記録する」を押したとき。 */
export function openVisitNew() {
  showNotice("");
  const survivor = detail?.survivor;
  if (!survivor) {
    showNotice("記録が読み込めていません。一覧から開き直してください");
    return;
  }
  el("visit-subtitle").textContent = `${nameWithAge(survivor.full_name, survivor.age)}／${
    survivor.location_type_label || ""
  }`;
  el("visit-raw-text").value = "";
  el("visit-confirm-card").hidden = true;
  el("visit-ai-note").hidden = true;
  showFormError(el("form-visit-text"), "");
  showFormError(el("form-visit"), "");
  buildItemInputs();
  showScreen("screen-visit-new");
}

/** 9つの項目の入力欄を作る（見出しは /api/public-config の visit_fields が正本）。 */
function buildItemInputs() {
  const container = el("visit-items");
  clear(container);
  itemInputs.clear();
  for (const field of visitFields()) {
    const label = node("label", "field");
    label.append(node("span", "field-label", field.label));
    const input = document.createElement("textarea");
    input.rows = 2;
    input.value = NOT_RECORDED;
    label.append(input);
    container.append(label);
    itemInputs.set(field.value, input);
  }
}

function localNow() {
  // datetime-local は「その端末の時間」をそのまま表すので、時差を引いてから切り出す
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 16);
}

async function submitStructure(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(
      `/api/survivors/${encodeURIComponent(survivorId())}/visits/structure`,
      { method: "POST", body: JSON.stringify({ raw_text: el("visit-raw-text").value }) },
    );
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    showConfirm(await res.json());
  });
}

/** AIを使わずに手で入れる（AIが使えないときの道。要件定義書F2）。 */
function startManual() {
  const items = {};
  for (const field of visitFields()) items[field.value] = NOT_RECORDED;
  showConfirm({
    items,
    location_proposal: null,
    health_proposal: null,
    ai_used: false,
    notice: "AIを使わずに、手で入れた内容を保存します。",
  });
}

function showConfirm(body) {
  proposals = {
    location: body.location_proposal || null,
    health: body.health_proposal || null,
    aiUsed: Boolean(body.ai_used),
  };
  if (itemInputs.size === 0) buildItemInputs();
  for (const [key, input] of itemInputs) {
    input.value = body.items?.[key] || NOT_RECORDED;
  }
  el("visit-visited-at").value = localNow();

  const note = el("visit-ai-note");
  note.textContent = body.notice || "";
  note.hidden = !body.notice;

  showLocationProposal(body.location_proposal);
  showHealthProposal(body.health_proposal);

  showFormError(el("form-visit"), "");
  el("visit-confirm-card").hidden = false;
  el("visit-confirm-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

function showLocationProposal(proposal) {
  const box = el("visit-location-proposal");
  if (!proposal) {
    box.hidden = true;
    return;
  }
  el("visit-location-proposal-text").textContent = `${
    proposal.location_type_label || ""
  }${proposal.location_detail ? `（${proposal.location_detail}）` : ""}${
    proposal.reason ? `／理由：${proposal.reason}` : ""
  }`;
  el("visit-apply-location").checked = false;
  el("visit-apply-location-field").hidden = !isProfessional();
  el("visit-location-pending-note").hidden = isProfessional();
  box.hidden = false;
}

function showHealthProposal(proposal) {
  const box = el("visit-health-proposal");
  if (!proposal) {
    box.hidden = true;
    return;
  }
  el("visit-health-proposal-reason").textContent = proposal.reason
    ? `理由：${proposal.reason}`
    : "";
  const list = el("visit-health-proposal-list");
  clear(list);
  list.append(...healthProposalList(proposal).childNodes);
  el("visit-apply-health").checked = false;
  el("visit-apply-health-field").hidden = !isProfessional();
  el("visit-health-pending-note").hidden = isProfessional();
  box.hidden = false;
}

async function submitVisit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const items = {};
  for (const [key, input] of itemInputs) items[key] = input.value;

  const visitedAtValue = el("visit-visited-at").value;
  const body = {
    // datetime-local はその端末の時間。UTC に直してから送る（9時間ずれるのを防ぐ）
    visited_at: visitedAtValue ? new Date(visitedAtValue).toISOString() : null,
    raw_text: el("visit-raw-text").value,
    items,
    location_proposal: proposals.location,
    health_proposal: proposals.health,
    ai_used: proposals.aiUsed,
    apply_location: isProfessional() && el("visit-apply-location").checked,
    apply_health: isProfessional() && el("visit-apply-health").checked,
  };

  const id = survivorId();
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(id)}/visits`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    const saved = await res.json();
    await openDetail(id);
    if (saved.apply_error) {
      showNotice(saved.apply_error, "warn");
      return;
    }
    const message = saved.applied?.length
      ? "記録を保存し、更新の案を反映しました。"
      : saved.visit?.is_pending
        ? "記録を保存しました。更新の案は確認待ちです。"
        : "記録を保存しました。";
    showNotice(message, "ok");
  });
}

// --- F3 の健康・介護の情報の画面 ---

/** 「健康・介護の情報を入れる」を押したとき。 */
export function openHealth() {
  showNotice("");
  const health = detail?.survivor?.health || null;
  const note = el("health-self-note");
  if (me?.role === ROLE_SURVIVOR) {
    note.textContent =
      "ご本人が入れた内容は「ご本人の申告」として残ります。専門職が確かめたうえで使います。";
    note.className = "notice-box notice-warn";
    note.hidden = false;
  } else {
    note.hidden = true;
  }
  buildHealthInputs(health);
  showFormError(el("form-health"), "");
  showScreen("screen-health");
}

function buildHealthInputs(health) {
  const container = el("health-fields");
  clear(container);
  healthInputs.clear();
  const values = health?.fields || {};

  for (const field of healthFields()) {
    const label = node("label", "field");
    label.append(node("span", "field-label", field.label));
    let input;
    if (field.multi) {
      input = document.createElement("textarea");
      input.rows = 2;
      input.placeholder = "1行に1つ書いてください";
      input.value = (values[field.value] || []).join("\n");
      label.append(input);
      label.append(node("span", "field-note", "複数あるときは、改行で分けてください。"));
    } else if (field.value === "care_level") {
      input = document.createElement("select");
      label.append(input);
      fillSelect(input, careLevels(), { placeholder: "選んでください（任意）" });
      input.value = values[field.value] || "";
    } else {
      input = document.createElement("textarea");
      input.rows = 2;
      input.value = values[field.value] || "";
      label.append(input);
    }
    container.append(label);
    healthInputs.set(field.value, input);
  }
}

async function submitHealth(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const body = {};
  for (const [key, input] of healthInputs) body[key] = input.value;

  const id = survivorId();
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(id)}/health`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    await openDetail(id);
    showNotice("健康・介護の情報を保存しました。前の内容も消えずに残ります。", "ok");
  });
}

// --- イベントの登録 ---

export function bind({ openDetail: openDetailHandler }) {
  openDetail = openDetailHandler;

  el("visit-new").addEventListener("click", openVisitNew);
  el("health-edit").addEventListener("click", openHealth);
  el("visit-manual").addEventListener("click", startManual);
  el("form-visit-text").addEventListener("submit", submitStructure);
  el("form-visit").addEventListener("submit", submitVisit);
  el("form-health").addEventListener("submit", submitHealth);

  for (const button of document.querySelectorAll(".back-to-detail")) {
    button.addEventListener("click", () => {
      const id = survivorId();
      if (!id) return;
      openDetail(id).catch((err) => showNotice(err.message));
    });
  }
}
