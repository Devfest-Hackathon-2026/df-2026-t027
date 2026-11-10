// 施設の職員の画面（使いやすさの改修の計画.md 9章）
//
// - 施設の職員は、**自分の施設だけ**を見て直す。施設は /api/my-facility が
//   stg_users から決める（画面は施設のIDを送らない。要件定義書10-3）。
// - 空きの数は「−」「＋」の大きなボタンで変える。断水・停電・対応できる介護と医療は
//   押すと切り替わるボタンにする。押すたびには送らず、「この内容で更新する」で1回だけ送る
//   （押すたびに送ると、版が何十も増え、通信が悪いときに途中の値が残る）。
// - 対応できる介護・医療の選択肢は **API が返すものだけ**を使う（正本は services/facilities.py）。
// - 被災者の記録・引き継ぎシートは見せない（健康・介護の情報が入るため。9章）。
// - 施設の名前をコンソールに出さない。

import { apiFetch, getConfig, readError } from "./auth.js";
import { icon } from "./icons.js";
import {
  clear,
  confirmDialog,
  doneSkeleton,
  el,
  formatDateTime,
  node,
  showFormError,
  showNotice,
  showScreen,
  showSkeleton,
  withBusy,
} from "./ui.js";

const ROLE_FACILITY_STAFF = "facility_staff";

// 空きの数の上限（services/facilities.py の VACANCIES_MAX と同じ値）
const VACANCIES_MAX = 9999;

let me = null;
// 読んだときの施設（送る前に、変えたかどうかを比べる）
let loaded = null;
// 画面で直している値
let draft = null;
// 対応できる介護・医療の切り替えボタン（IDを組み立てないため要素を持つ）
const capabilityButtons = new Map();

/** 立場を受け取る（ホームから渡される）。 */
export function setMe(value) {
  me = value;
}

/** 施設の職員か（画面の出し分けだけに使う。決めるのはサーバー）。 */
export function isFacilityStaff() {
  return me?.role === ROLE_FACILITY_STAFF;
}

function capabilityChoices() {
  return getConfig()?.choices?.facility_capabilities || [];
}

/** 「自分の施設」を開く（ホームのタイル・下のタブ・ログインの直後から）。 */
export async function openMyFacility() {
  showNotice("");
  if (!isFacilityStaff()) {
    showNotice("自分の施設を直せるのは、施設の職員だけです");
    return;
  }
  showFormError(el("form-my-facility"), "");
  showScreen("screen-my-facility");
  await loadMyFacility();
}

async function loadMyFacility() {
  el("my-facility-status").textContent = "読み込んでいます…";
  el("form-my-facility").hidden = true;
  showSkeleton(el("my-facility-loading"), 2);
  const res = await apiFetch("/api/my-facility");
  doneSkeleton(el("my-facility-loading"));
  if (!res.ok) {
    el("my-facility-status").textContent =
      res.status === 404
        ? "施設が見つかりません。施設を登録した組織の専門職にお知らせください。"
        : await readError(res);
    return;
  }
  render((await res.json()).facility);
}

function render(facility) {
  loaded = facility;
  draft = {
    vacancies: Number.isInteger(facility.vacancies) ? facility.vacancies : 0,
    water_outage: Boolean(facility.water_outage),
    power_outage: Boolean(facility.power_outage),
    capabilities: new Set(facility.capabilities || []),
  };

  el("my-facility-name").textContent = facility.facility_name || "自分の施設";
  el("my-facility-type").textContent = facility.facility_type_label || "";
  const who = facility.updated_role_label ? `・${facility.updated_role_label}が更新` : "";
  el("my-facility-status").textContent = `最終更新：${formatDateTime(facility.updated_at)}${who}`;
  const stale = el("my-facility-stale");
  stale.hidden = !facility.is_stale;
  stale.textContent = facility.is_stale
    ? "最終更新から24時間以上たっています。今の空きの数を入れ直してください。"
    : "";

  buildCapabilityButtons();
  renderDraft();
  el("form-my-facility").hidden = false;
}

/** 対応できる介護・医療の切り替えボタンを作る（選択肢は API が返すものだけ）。 */
function buildCapabilityButtons() {
  const container = el("my-facility-capabilities");
  clear(container);
  capabilityButtons.clear();
  for (const item of capabilityChoices()) {
    const button = toggleButton(item.label);
    button.addEventListener("click", () => {
      if (draft.capabilities.has(item.value)) draft.capabilities.delete(item.value);
      else draft.capabilities.add(item.value);
      renderDraft();
    });
    container.append(button);
    capabilityButtons.set(item.value, button);
  }
}

function toggleButton(text) {
  const button = node("button", "toggle");
  button.type = "button";
  button.append(icon("check", "icon toggle-mark"));
  button.append(node("span", null, text));
  return button;
}

function setPressed(button, pressed) {
  button.setAttribute("aria-pressed", pressed ? "true" : "false");
}

/** 画面で直している値を、ボタンと数に映す。 */
function renderDraft() {
  if (!draft) return;
  el("my-facility-vacancies").textContent = String(draft.vacancies);
  el("my-facility-minus").disabled = draft.vacancies <= 0;
  el("my-facility-plus").disabled = draft.vacancies >= VACANCIES_MAX;
  setPressed(el("my-facility-water"), draft.water_outage);
  setPressed(el("my-facility-power"), draft.power_outage);
  for (const [code, button] of capabilityButtons) setPressed(button, draft.capabilities.has(code));
  const changed = isChanged();
  el("my-facility-changed").hidden = !changed;
}

function chosenCapabilities() {
  // 並びは選択肢の順にそろえる（押した順にすると、同じ内容でも版ごとに並びが変わる）
  return capabilityChoices()
    .map((item) => item.value)
    .filter((code) => draft.capabilities.has(code));
}

function isChanged() {
  if (!loaded || !draft) return false;
  const before = [...(loaded.capabilities || [])].sort().join(",");
  const after = [...draft.capabilities].sort().join(",");
  return (
    draft.vacancies !== loaded.vacancies ||
    draft.water_outage !== Boolean(loaded.water_outage) ||
    draft.power_outage !== Boolean(loaded.power_outage) ||
    before !== after
  );
}

function stepVacancies(delta) {
  if (!draft) return;
  draft.vacancies = Math.min(VACANCIES_MAX, Math.max(0, draft.vacancies + delta));
  renderDraft();
}

async function submitMyFacility(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  if (!draft) return;
  // 変えていなくても送れる（「今も同じ」と伝えると、24時間の知らせが消える）。
  // そのときは、送る前に確かめる
  if (
    !isChanged() &&
    !(await confirmDialog("内容は変わっていません。今もこのとおりとして、更新の時刻だけ新しくしますか？", {
      ok: "このとおりで更新する",
    }))
  ) {
    return;
  }
  const body = {
    vacancies: draft.vacancies,
    water_outage: draft.water_outage,
    power_outage: draft.power_outage,
    capabilities: chosenCapabilities(),
  };
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/my-facility", {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    render((await res.json()).facility);
    showNotice("施設の情報を更新しました（前の内容も消えずに残ります）。", "ok");
  });
}

// --- イベントの登録 ---

export function bind() {
  el("my-facility-minus").addEventListener("click", () => stepVacancies(-1));
  el("my-facility-plus").addEventListener("click", () => stepVacancies(1));
  el("my-facility-water").addEventListener("click", () => {
    if (!draft) return;
    draft.water_outage = !draft.water_outage;
    renderDraft();
  });
  el("my-facility-power").addEventListener("click", () => {
    if (!draft) return;
    draft.power_outage = !draft.power_outage;
    renderDraft();
  });
  el("form-my-facility").addEventListener("submit", submitMyFacility);
  el("my-facility-reload").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () => loadMyFacility().catch((err) => showNotice(err.message))),
  );
}
