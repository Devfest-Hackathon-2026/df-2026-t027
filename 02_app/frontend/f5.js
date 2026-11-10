// F5 受け入れ先の施設と移る先の決定の画面（開発手順書 Step 9-1・9-2）
//
// - **専門職だけが使う。** 一般の支援者と本人には入口を出さない（要件定義書F5・446行）。
// - 施設の種類と「対応できる介護・医療」の選択肢は **API が返すものだけ**を使う
//   （正本は `services/facilities.py`。画面に書き写すとずれる。Step 8 と同じ考え方）。
// - **空きがない施設と、必要な医療に対応できない施設は候補に出ない。** なぜ出ないかを
//   「候補に出なかった施設」に理由つきで出す（画面で理由が分からないと、入力の誤りと
//   区別できない）。
// - **候補にない施設も選べる**（要件定義書F5）。選べる施設は API の `selectable` を使う。
// - 最終更新から24時間以上たった施設は、その知らせを出す（文言は API が返す）。
// - 施設の名前・被災者の氏名をコンソールに出さない。
// - 施設ごとに「施設の職員の参加コード」を見る・発行し直す（使いやすさの改修の計画.md 9章）。
//   コードは押したときだけ読む（一覧の読みには入れない）。最終更新には、施設の職員と
//   専門職のどちらが更新したかを出す。

import { apiFetch, getConfig, readError } from "./auth.js";
import {
  clear,
  confirmDialog,
  el,
  fillSelect,
  formatDateTime,
  node,
  showFormError,
  showNotice,
  showScreen,
  withBusy,
} from "./ui.js";

const ROLE_PROFESSIONAL = "professional";

let me = null;
let detail = null;
let openDetail = async () => {};
let openHandover = () => {};

// 施設の登録の「対応できる介護・医療」のチェック欄（IDを組み立てないため要素を持つ）
const capabilityInputs = new Map();

// いま開いている候補の画面の状態（決めるときにそのまま送る）
let placement = { survivorId: "", candidates: [], selectable: [] };

/** 立場を受け取る（ホームから渡される）。 */
export function setMe(value) {
  me = value;
}

function choices() {
  return getConfig()?.choices || {};
}

function facilityTypes() {
  return choices().facility_types || [];
}

function capabilities() {
  return choices().facility_capabilities || [];
}

function isProfessional() {
  return me?.role === ROLE_PROFESSIONAL;
}

// --- 施設の一覧と登録 ---

/** 「受け入れ先の施設」を開く（ホームから）。 */
export async function openFacilities() {
  showNotice("");
  if (!isProfessional()) {
    showNotice("受け入れ先の施設を扱えるのは専門職だけです");
    return;
  }
  buildFacilityForm();
  showFormError(el("form-facility"), "");
  showScreen("screen-facilities");
  await loadFacilities();
}

/** 施設の一覧を読み直す。 */
export async function loadFacilities() {
  el("facilities-count").textContent = "読み込んでいます…";
  const res = await apiFetch("/api/facilities");
  if (!res.ok) {
    el("facilities-count").textContent = "読み込めませんでした。読み直してください";
    clear(el("facilities-list"));
    return;
  }
  renderFacilities(await res.json());
}

function renderFacilities(body) {
  const list = el("facilities-list");
  clear(list);
  const items = body?.facilities || [];
  el("facilities-count").textContent =
    items.length === 0 ? "まだ施設が登録されていません。" : `${items.length}件`;

  const stale = el("facilities-stale-note");
  if (body.stale_count > 0) {
    stale.textContent = `${body.stale_count}件の施設が、最終更新から24時間以上たっています。空きの数を確かめてください。`;
    stale.hidden = false;
  } else {
    stale.hidden = true;
  }

  for (const item of items) list.append(renderFacility(item));
}

function renderFacility(item) {
  const li = node("li", "card");
  li.append(node("h3", "row-title", item.facility_name));

  const marks = node("div", "row-marks");
  marks.append(node("span", "badge badge-note", item.facility_type_label));
  marks.append(
    node("span", item.vacancies > 0 ? "badge badge-low" : "badge badge-high", `空き ${item.vacancies}`),
  );
  if (item.water_outage) marks.append(node("span", "badge badge-high", "断水"));
  if (item.power_outage) marks.append(node("span", "badge badge-high", "停電"));
  if (item.is_stale) marks.append(node("span", "badge badge-medium", "情報が古い"));
  li.append(marks);

  if (item.address) li.append(node("p", "row-note", item.address));
  li.append(
    node(
      "p",
      "row-note",
      `対応できる介護・医療：${item.capability_labels.join("・") || "記載なし"}`,
    ),
  );
  // 最後に更新したのが施設の職員か専門職か（9章）
  const who = item.updated_role_label ? `・${item.updated_role_label}が更新` : "";
  li.append(
    node("p", "row-note", `最終更新：${formatDateTime(item.updated_at)}${who}（版${item.version}）`),
  );
  if (item.stale_notice) li.append(node("p", "note", item.stale_notice));

  li.append(renderFacilityUpdateForm(item));
  li.append(renderStaffJoinCode(item));
  return li;
}

/** 施設の職員の参加コードを見る・発行し直す欄（9章。施設ごとに作る。IDは付けない）。 */
function renderStaffJoinCode(item) {
  const box = node("div", "proposal");
  box.append(node("h4", "proposal-title", "施設の職員の参加コード"));
  box.append(
    node(
      "p",
      "row-note",
      "施設の職員に渡すと、その人がこの施設の空きなどを直せるようになります（被災者の記録は見えません）。",
    ),
  );
  const code = node("p", "code-display");
  code.hidden = true;
  box.append(code);
  const note = node("p", "row-note");
  note.hidden = true;
  box.append(note);

  const actions = node("div", "actions");
  const show = node("button", "button", "参加コードを見る");
  show.type = "button";
  const issue = node("button", "button", "発行し直す");
  issue.type = "button";
  issue.hidden = true;

  const showCode = (body) => {
    if (body.available) {
      code.textContent = body.staff_join_code;
      code.hidden = false;
      note.textContent = "発行し直すと、このコードでは入れなくなります（もう入っている職員はそのまま使えます）。";
      issue.textContent = "発行し直す";
    } else {
      code.hidden = true;
      note.textContent = "まだ発行していません。";
      issue.textContent = "発行する";
    }
    note.hidden = false;
    issue.hidden = false;
    show.hidden = true;
  };

  show.addEventListener("click", () =>
    withBusy(show, async () => {
      const res = await apiFetch(`/api/facilities/${encodeURIComponent(item.facility_id)}/staff-join-code`);
      if (!res.ok) {
        showNotice(await readError(res));
        return;
      }
      showCode(await res.json());
    }),
  );
  issue.addEventListener("click", async () => {
    // 発行し直すと前のコードは使えなくなるので、先に確かめる
    if (
      !code.hidden &&
      !(await confirmDialog("前のコードは使えなくなります。発行し直しますか？", { ok: "発行し直す" }))
    ) {
      return;
    }
    await withBusy(issue, async () => {
      const res = await apiFetch(`/api/facilities/${encodeURIComponent(item.facility_id)}/staff-join-code`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        showNotice(await readError(res));
        return;
      }
      showCode(await res.json());
      showNotice("施設の職員の参加コードを発行しました。施設の職員に渡してください。", "ok");
    });
  });
  actions.append(show, issue);
  box.append(actions);
  return box;
}

/** 空き・断水・停電を更新する欄（施設ごとに作る。IDは付けない）。 */
function renderFacilityUpdateForm(item) {
  const box = node("div", "proposal");
  box.append(node("h4", "proposal-title", "空き・断水・停電を更新する"));

  const vacancyLabel = node("label", "field");
  vacancyLabel.append(node("span", "field-label", "空きの数"));
  const vacancies = document.createElement("input");
  vacancies.type = "number";
  vacancies.inputMode = "numeric";
  vacancies.min = "0";
  vacancies.max = "9999";
  vacancies.value = String(item.vacancies ?? "");
  vacancyLabel.append(vacancies);
  box.append(vacancyLabel);

  const water = checkbox("断水している", item.water_outage);
  const power = checkbox("停電している", item.power_outage);
  box.append(water.label);
  box.append(power.label);

  const error = node("p", "form-error");
  error.hidden = true;
  box.append(error);

  const actions = node("div", "actions");
  const save = node("button", "button", "この内容に更新する");
  save.type = "button";
  save.addEventListener("click", () =>
    withBusy(save, () =>
      updateFacility(item.facility_id, {
        vacancies: numberOrNull(vacancies.value),
        water_outage: water.input.checked,
        power_outage: power.input.checked,
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

function checkbox(text, checked) {
  const label = node("label", "field field-check");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(checked);
  label.append(input);
  label.append(node("span", null, text));
  return { label, input };
}

function numberOrNull(value) {
  const text = (value || "").trim();
  if (!text) return null;
  const parsed = Number(text);
  return Number.isInteger(parsed) ? parsed : null;
}

async function updateFacility(facilityId, body) {
  const res = await apiFetch(`/api/facilities/${encodeURIComponent(facilityId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await readError(res));
  await loadFacilities();
  showNotice("施設の情報を更新しました（前の内容も消えずに残ります）。", "ok");
}

/** 登録の欄を作る（選択肢は API が返すものだけを使う）。 */
function buildFacilityForm() {
  fillSelect(el("facility-type"), facilityTypes(), { placeholder: "選んでください" });
  const container = el("facility-capabilities");
  clear(container);
  capabilityInputs.clear();
  for (const item of capabilities()) {
    const { label, input } = checkbox(item.label, false);
    container.append(label);
    capabilityInputs.set(item.value, input);
  }
}

async function submitFacility(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const chosen = [];
  for (const [code, input] of capabilityInputs) {
    if (input.checked) chosen.push(code);
  }
  const body = {
    facility_name: el("facility-name").value,
    facility_type: el("facility-type").value,
    address: el("facility-address").value,
    vacancies: numberOrNull(el("facility-vacancies").value),
    capabilities: chosen,
    water_outage: el("facility-water").checked,
    power_outage: el("facility-power").checked,
  };
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/facilities", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    form.reset();
    buildFacilityForm();
    await loadFacilities();
    showNotice("施設を登録しました。", "ok");
  });
}

// --- 詳細の画面の「受け入れ先と引き継ぎ」 ---

/** 詳細の画面に、受け入れ先の入口を出す（f1.js が詳細を描いたあとに呼ばれる）。
 *
 *  **本人にもカードは出す**（要件定義書7-1 の「引き継ぎシート：本人も見られる」）。
 *  本人には施設の候補の入口（`placement-open`）を出さない（施設の情報は専門職だけ。
 *  要件定義書446行）。引き継ぎシートを作るボタンは handovers.js が立場で決める。 */
export function renderDetailPlacement(detailBody) {
  detail = detailBody;
  const card = el("detail-placement-card");
  const survivor = detailBody?.survivor;
  if (!survivor) {
    card.hidden = true;
    return;
  }
  const isSelf = survivor.role_view === "survivor";
  el("placement-open").hidden = isSelf || !isProfessional();
  if (isSelf) {
    el("detail-placement-note").textContent =
      "支援者が作った引き継ぎシートを見られます。移る先に渡す内容の確認にお使いください。";
  } else if (isProfessional()) {
    el("detail-placement-note").textContent =
      "空きと必要な医療から受け入れ先の候補を出し、決めた理由を残せます。決めたあとに居場所を変え、引き継ぎシートを作れます。";
  } else {
    el("detail-placement-note").textContent =
      "引き継ぎシートを作って、移る先に渡せます。施設の候補は専門職が出します。";
  }
  card.hidden = false;
}

function survivorId() {
  return detail?.survivor?.survivor_id || "";
}

// --- 候補と決定の画面 ---

/** 「受け入れ先の候補を見る」を押したとき。 */
export async function openPlacement() {
  showNotice("");
  const id = survivorId();
  if (!id) {
    showNotice("記録が開かれていません。一覧から開き直してください");
    return;
  }
  const survivor = detail?.survivor;
  el("placement-subtitle").textContent = survivor
    ? `${survivor.full_name}さんの受け入れ先を決めます`
    : "受け入れ先を決めます";
  el("placement-disclaimer").textContent =
    // 候補は AI を使わずルールで並べる（画面の改修の計画.md 7章）
    "候補は、対応できる介護・医療と空きの数などで並べた案です。空きの数は施設に確かめてから決めてください。決めるのは専門職です。";
  el("placement-candidates-note").textContent = "読み込んでいます…";
  clear(el("placement-candidates"));
  clear(el("placement-excluded"));
  el("placement-notice").hidden = true;
  el("placement-unmatched").hidden = true;
  el("placement-next-card").hidden = true;
  el("placement-reason").value = "";
  showFormError(el("form-placement"), "");
  showScreen("screen-placement");

  const res = await apiFetch(`/api/survivors/${encodeURIComponent(id)}/facility-candidates`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  if (!res.ok) {
    el("placement-candidates-note").textContent = "読み込めませんでした";
    showNotice(await readError(res));
    return;
  }
  renderCandidates(await res.json());
}

function renderCandidates(body) {
  placement = {
    survivorId: body.survivor_id || survivorId(),
    candidates: (body.candidates || []).map((item) => ({
      facility_id: item.facility_id,
      reason: item.reason,
    })),
    selectable: body.selectable || [],
  };

  const list = el("placement-candidates");
  clear(list);
  const candidates = body.candidates || [];
  el("placement-candidates-note").textContent =
    candidates.length === 0
      ? "候補はありません。下の一覧から選んでください。"
      : `${candidates.length}件（多くても3件）`;
  for (const item of candidates) list.append(renderCandidate(item));

  const excluded = el("placement-excluded");
  clear(excluded);
  const rows = body.excluded || [];
  if (rows.length === 0) {
    excluded.append(node("li", "note", "ありません。"));
  } else {
    for (const row of rows) {
      const li = node("li", null);
      li.append(node("span", "row-title", row.facility_name || "（名前なし）"));
      li.append(node("span", "badge badge-note", row.reason));
      if ((row.missing_capability_labels || []).length > 0) {
        li.append(node("p", "row-note", `足りない項目：${row.missing_capability_labels.join("・")}`));
      }
      excluded.append(li);
    }
  }

  const notice = el("placement-notice");
  notice.textContent = body.notice || "";
  notice.hidden = !body.notice;

  const unmatched = el("placement-unmatched");
  if ((body.unmatched_needs || []).length > 0) {
    unmatched.textContent = `${body.unmatched_notice}（${body.unmatched_needs.join("・")}）`;
    unmatched.hidden = false;
  } else {
    unmatched.hidden = true;
  }

  fillSelect(
    el("placement-facility"),
    placement.selectable.map((item) => ({
      value: item.facility_id,
      label: `${item.facility_name}（空き ${item.vacancies}・${item.facility_type_label}）`,
    })),
    { placeholder: "選んでください" },
  );
  if (candidates.length > 0) el("placement-facility").value = candidates[0].facility_id;
}

function renderCandidate(item) {
  const li = node("li", null);
  li.append(node("span", "row-title", item.facility_name));
  const marks = node("div", "row-marks");
  marks.append(node("span", "badge badge-note", item.facility_type_label));
  marks.append(node("span", "badge badge-low", `空き ${item.vacancies}`));
  if (item.water_outage) marks.append(node("span", "badge badge-high", "断水"));
  if (item.power_outage) marks.append(node("span", "badge badge-high", "停電"));
  if (item.is_stale) marks.append(node("span", "badge badge-medium", "情報が古い"));
  li.append(marks);
  if (item.reason) li.append(node("p", null, `理由：${item.reason}`));
  if (item.address) li.append(node("p", "row-note", item.address));
  li.append(
    node("p", "row-note", `対応できる介護・医療：${item.capability_labels.join("・") || "記載なし"}`),
  );
  if (item.stale_notice) li.append(node("p", "note", item.stale_notice));
  // 候補の中では、これが次に押すボタン（要件定義書11-2の2-b）
  const choose = node("button", "button button-primary", "この施設を選ぶ");
  choose.type = "button";
  choose.addEventListener("click", () => {
    el("placement-facility").value = item.facility_id;
    el("placement-reason").focus();
  });
  li.append(choose);
  return li;
}

async function submitPlacement(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const id = placement.survivorId || survivorId();
  const body = {
    chosen_facility_id: el("placement-facility").value,
    chosen_reason: el("placement-reason").value,
    candidates: placement.candidates,
  };
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(id)}/placements`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    showDecided(await res.json());
  });
}

// 決めた移る先（引き継ぎシートの初期値にする）
let decidedDestination = "";

// 決めた施設から入れる居場所の初期値（記録の「居場所を変える」の初期値にする）。
// **1回だけ**使う。開き直すたびに入れると、専門職が直した値が戻ってしまう
let suggestedLocationValue = null;

function showDecided(body) {
  decidedDestination = body?.next_steps?.handover_destination || "";
  suggestedLocationValue = {
    location_type: body?.next_steps?.location_type || "",
    location_detail: body?.next_steps?.location_detail || "",
  };
  el("placement-next-note").textContent =
    `移る先を「${decidedDestination}」に決めました。` +
    "記録の「居場所を変える」で居場所を更新し、引き継ぎシートを作って渡してください。";
  el("placement-next-card").hidden = false;
  el("placement-next-card").scrollIntoView({ behavior: "smooth", block: "start" });
  showNotice("移る先を記録しました。", "ok");
}

/** 決めた移る先（引き継ぎシートの画面が初期値に使う）。 */
export function destination() {
  return decidedDestination;
}

/** 施設の種類から決めた居場所の初期値（記録の画面が使う）。**1回返したら捨てる**。 */
export function suggestedLocation() {
  const value = suggestedLocationValue;
  suggestedLocationValue = null;
  return value;
}

// --- イベントの登録 ---

export function bind({ openDetail: openDetailHandler, openHandover: openHandoverHandler }) {
  if (openDetailHandler) openDetail = openDetailHandler;
  if (openHandoverHandler) openHandover = openHandoverHandler;

  el("facilities-reload").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () => loadFacilities().catch((err) => showNotice(err.message))),
  );
  el("form-facility").addEventListener("submit", submitFacility);

  el("placement-open").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () => openPlacement().catch((err) => showNotice(err.message))),
  );
  el("form-placement").addEventListener("submit", submitPlacement);

  el("placement-to-handover").addEventListener("click", () => openHandover());
  el("placement-to-detail").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () =>
      openDetail(placement.survivorId || survivorId()).catch((err) => showNotice(err.message)),
    ),
  );
}
