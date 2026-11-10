// F3 引き継ぎシートの画面（開発手順書 Step 9-3）
//
// - **保存するまでシートにならない。** AIの案は画面の中だけに置き、作った人が確かめて直す
//   （要件定義書F3。F2・F8 と同じ形）。
// - **AIの案であることを必ず画面に出す。** 文言は API が返すもの（正本は
//   `services/handovers.py` の AI_DISCLAIMER）。画面に書き写すとずれる。
// - **AIが使えなくても作れる。** 「自分で書く」から空の欄に書いて保存できる（`ai_used: false`）。
// - 印刷の画面は、ブラウザの印刷でそのまま紙やPDFにできるようにする（要件定義書F3）。
//   ボタンと戻るリンクは `.no-print` で印刷に出さない。
// - 保存したあとに健康・介護の情報が更新されていたときは、そのことを画面に出す
//   （`raw_handovers` は健康の項目そのものを持たないため、出るのは**いまの**情報）。
// - 氏名・健康の情報をコンソールに出さない。
// - ご本人のホームの「引き継ぎシート」から、記録の詳細を開かずにシートの一覧を開ける
//   （追加修正⑪。`GET /api/survivors/{id}/handovers`）。見られる範囲は記録と同じで、
//   組織を変えても前に作ったシートが見える。印刷のボタンは今のまま。

import { apiFetch, getConfig, readError } from "./auth.js";
import {
  addPair,
  clear,
  el,
  formatDateTime,
  node,
  showFormError,
  showNotice,
  showScreen,
  withBusy,
} from "./ui.js";

const ROLE_PROFESSIONAL = "professional";
const ROLE_VOLUNTEER = "volunteer";

let me = null;
let detail = null;
let openDetail = async () => {};

// 案の状態（保存するときにそのまま送る）
let draft = { aiUsed: true, healthVersion: null, destination: "" };

/** 立場を受け取る（ホームから渡される）。 */
export function setMe(value) {
  me = value;
}

function choices() {
  return getConfig()?.choices || {};
}

function healthFields() {
  return choices().health_fields || [];
}

function canCreate() {
  return me?.role === ROLE_PROFESSIONAL || me?.role === ROLE_VOLUNTEER;
}

function survivorId() {
  return detail?.survivor?.survivor_id || "";
}

function survivorName() {
  return detail?.survivor?.full_name || "";
}

function disclaimerText() {
  return choices().handover_ai_disclaimer || "";
}

// --- 詳細の画面の「作った引き継ぎシート」 ---

/** 詳細の画面に、保存した引き継ぎシートの一覧を出す（f1.js のあとに呼ばれる）。 */
export function renderDetailHandovers(detailBody) {
  detail = detailBody;
  const survivor = detailBody?.survivor;
  el("handover-new").hidden = !canCreate();

  const list = el("detail-handovers");
  clear(list);
  const rows = survivor?.handovers || [];
  if (rows.length === 0) {
    list.append(node("li", "note", "まだ作られていません。"));
    return;
  }
  for (const row of rows) {
    const li = node("li", null);
    const open = node(
      "button",
      "link-button",
      `${row.destination} 向け（${formatDateTime(row.created_at)}）`,
    );
    open.type = "button";
    open.addEventListener("click", () => showSheet(row, survivor?.health));
    li.append(open);
    if (!row.ai_used) li.append(node("span", "badge badge-note", "手で入力"));
    if (row.health_version_changed) {
      li.append(node("span", "badge badge-medium", "健康の情報が更新されています"));
    }
    list.append(li);
  }
}

// --- 引き継ぎシートの一覧（ご本人のホームから。追加修正⑪） ---

/** その人の引き継ぎシートの一覧を開く（記録の詳細は開かない）。 */
export async function openHandoverList(survivorIdValue) {
  showNotice("");
  if (!survivorIdValue) {
    showNotice("ご自分の記録が見つかりません。ログアウトしてお試しください");
    return;
  }
  const res = await apiFetch(`/api/survivors/${encodeURIComponent(survivorIdValue)}/handovers`);
  if (!res.ok) {
    showNotice(await readError(res));
    return;
  }
  const body = await res.json();
  // 印刷の画面は `detail` からお名前・年齢を出すので、一覧の応答で同じ形を作っておく
  detail = { survivor: { ...(body.survivor || {}), health: body.health || null } };

  const list = el("handover-list");
  clear(list);
  const rows = body.handovers || [];
  if (rows.length === 0) {
    list.append(node("li", "note", "まだ作られていません。"));
  }
  for (const row of rows) {
    const li = node("li", null);
    const open = node(
      "button",
      "link-button",
      `${row.destination} 向け（${formatDateTime(row.created_at)}）`,
    );
    open.type = "button";
    open.addEventListener("click", () => {
      showSheet(row, body.health);
      // 一覧から開いたときだけ「一覧に戻る」を出す
      el("handover-back-to-list").hidden = false;
    });
    li.append(open);
    if (row.health_version_changed) {
      li.append(node("span", "badge badge-medium", "健康の情報が更新されています"));
    }
    list.append(li);
  }
  showScreen("screen-handover-list");
}

// --- 引き継ぎシートを作る画面 ---

/** 「引き継ぎシートを作る」を押したとき（移る先が決まっていれば初期値に入れる）。 */
export function openHandoverNew(destinationValue = "") {
  showNotice("");
  if (!survivorId()) {
    showNotice("記録が開かれていません。一覧から開き直してください");
    return;
  }
  el("handover-subtitle").textContent = survivorName()
    ? `${survivorName()}さんの引き継ぎシートを作ります`
    : "引き継ぎシートを作ります";
  el("handover-destination").value = destinationValue || "";
  el("handover-confirm-card").hidden = true;
  el("handover-ai-note").hidden = true;
  el("handover-disclaimer").textContent = disclaimerText();
  showFormError(el("form-handover-destination"), "");
  showFormError(el("form-handover"), "");
  showScreen("screen-handover-new");
}

async function submitDraft(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const id = survivorId();
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(id)}/handovers/draft`, {
      method: "POST",
      body: JSON.stringify({ destination: el("handover-destination").value }),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    showConfirm(await res.json());
  });
}

/** AIを使わずに自分で書く（AIが使えないときの道。要件定義書F3）。 */
function startManual() {
  const destination = (el("handover-destination").value || "").trim();
  if (!destination) {
    showFormError(el("form-handover-destination"), "移る先を入れてください");
    return;
  }
  showConfirm({
    destination,
    draft: {
      summary: "",
      cautions: "",
      ai_used: false,
      notice: "AIを使わずに、ご自身で書いた内容を保存します。",
    },
    health_version: detail?.survivor?.health?.version ?? null,
    ai_disclaimer: disclaimerText(),
  });
}

function showConfirm(body) {
  const item = body.draft || {};
  draft = {
    aiUsed: Boolean(item.ai_used),
    healthVersion: body.health_version ?? null,
    destination: body.destination || "",
  };
  el("handover-summary").value = item.summary || "";
  el("handover-cautions").value = item.cautions || "";
  el("handover-disclaimer").textContent = body.ai_disclaimer || disclaimerText();

  const note = el("handover-ai-note");
  note.textContent = item.notice || "";
  note.hidden = !item.notice;

  showFormError(el("form-handover"), "");
  el("handover-confirm-card").hidden = false;
  el("handover-confirm-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function submitHandover(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const id = survivorId();
  const body = {
    destination: draft.destination || el("handover-destination").value,
    summary: el("handover-summary").value,
    cautions: el("handover-cautions").value,
    health_version: draft.healthVersion,
    ai_used: draft.aiUsed,
  };
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(id)}/handovers`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    const saved = await res.json();
    // 詳細を読み直して一覧に出したあと、印刷の画面を出す
    await openDetail(id);
    showSheet(saved.handover, saved.health);
    showNotice("引き継ぎシートを保存しました。印刷して移る先にお渡しください。", "ok");
  });
}

// --- 印刷の画面 ---

/** 保存した引き継ぎシートを、印刷できる形で出す。 */
export function showSheet(handover, healthPayload) {
  const meta = el("handover-print-meta");
  clear(meta);
  addPair(meta, "お名前", survivorName());
  addPair(meta, "年齢", detail?.survivor?.age ?? "");
  addPair(meta, "移る先", handover.destination);
  addPair(meta, "作った日時", formatDateTime(handover.created_at));
  addPair(meta, "作った人", handover.created_by_name || "");
  addPair(meta, "使った健康・介護の情報（何回目に書いたものか）", handover.health_version ?? "");

  const health = el("handover-print-health");
  clear(health);
  const fields = healthPayload?.fields || {};
  if (!healthPayload) {
    health.append(node("dt", null, "登録"));
    health.append(node("dd", null, "まだありません"));
  } else {
    for (const field of healthFields()) {
      const value = fields[field.value];
      const text = field.multi ? (value || []).join("・") : value || "";
      addPair(health, field.label, text);
    }
  }

  el("handover-print-summary").textContent = handover.summary || "";
  el("handover-print-cautions").textContent = handover.cautions || "（記載なし）";
  el("handover-print-disclaimer").textContent = handover.ai_disclaimer || disclaimerText();

  const notice = el("handover-print-notice");
  notice.textContent = handover.health_version_notice || "";
  notice.hidden = !handover.health_version_notice;

  // 「一覧に戻る」は一覧から開いたときだけ出す（呼ぶ側が出し直す。追加修正⑪）
  el("handover-back-to-list").hidden = true;
  showScreen("screen-handover");
}

// --- イベントの登録 ---

export function bind({ openDetail: openDetailHandler }) {
  if (openDetailHandler) openDetail = openDetailHandler;

  el("handover-new").addEventListener("click", () => openHandoverNew());
  el("handover-manual").addEventListener("click", startManual);
  el("form-handover-destination").addEventListener("submit", submitDraft);
  el("form-handover").addEventListener("submit", submitHandover);
  el("handover-print").addEventListener("click", () => window.print());
  el("handover-back-to-list").addEventListener("click", () => {
    showNotice("");
    showScreen("screen-handover-list");
  });
}
