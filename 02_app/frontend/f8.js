// F8 被災者からの相談の画面（開発手順書 Step 8-1〜8-4）
//
// - **ご本人は書いた文章をそのまま送る**（画面の改修の計画.md 6章）。AIが9つの項目に分けた案は、
//   送ったあとにサーバーで作り、相談員が読むときにだけ出す。ご本人には見せない。
// - **命に関わるときの案内（119番）は、AIの判断にかかわらず必ず出す。** 文言はAPIが返すもの
//   （正本は `services/consultations.py`）。画面に書き写すとずれる。
// - AIが「急を要する」と見たときは、送ったあとの画面にすぐ出す（要件定義書F8）。
// - **急ぎの度合いは本人の画面に出ない。** 本人向けの応答には `urgency` が入らないので、
//   入っていなければ出さない（画面側で立場を見て隠すのではなく、無いものを出さない）。
// - 専門職のホームには、未対応から・急ぎの順に並んだ相談を出す（並べ替えはAPIの順のまま）。
// - 氏名・相談の本文をコンソールに出さない。
// - 組織につながっていないご本人の相談は**公開**になる（追加修正②）。送る前に、組織を問わず
//   支援者が見られることを知らせて確かめてもらう（文言はAPIが返す）。支援者のホームには
//   「組織につながっていない方からの相談」を出す（お名前は出さず、市区町村まで）。
//   返事を書けるのは専門職だけ（APIが `can_reply` を返す）。

import { apiFetch, getConfig, readError } from "./auth.js";
import { describeMunicipality } from "./municipalities.js";
import {
  clear,
  confirmDialog,
  el,
  fillSelect,
  formatDateTime,
  node,
  setTileCount,
  showFormError,
  showNotice,
  showScreen,
  showSkeleton,
  withBusy,
} from "./ui.js";

const ROLE_PROFESSIONAL = "professional";
const ROLE_VOLUNTEER = "volunteer";
const ROLE_SURVIVOR = "survivor";

const NOT_RECORDED = "記載なし";

let me = null;
let detail = null;
let openDetail = async () => {};

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

function categories() {
  return choices().consultation_categories || [];
}

function statuses() {
  return choices().consultation_statuses || [];
}

function isProfessional() {
  return me?.role === ROLE_PROFESSIONAL;
}

function isSurvivor() {
  return me?.role === ROLE_SURVIVOR;
}

function isSupporter() {
  return me?.role === ROLE_PROFESSIONAL || me?.role === ROLE_VOLUNTEER;
}

/** 組織につながっていないご本人か（その相談は公開になる。追加修正②）。 */
function isPublicSender() {
  return isSurvivor() && !me?.org_id;
}

function publicNoticeText() {
  return choices().consultation_public_notice || "";
}

/** いま開いている記録のID（本人は自分の記録）。 */
function survivorId() {
  return detail?.survivor?.survivor_id || me?.survivor_id || "";
}

// --- 専門職のホームの「ご本人からの相談」 ---

/** ホームに相談の一覧を出す（未対応から・急ぎの順。要件定義書F8）。
 *
 *  ホームの表示を止めないように、呼ぶ側は待たずに呼んでよい。 */
export async function loadConsultations() {
  const card = el("home-consultations-card");
  if (!isProfessional()) {
    card.hidden = true;
    return;
  }
  el("home-consultations-count").textContent = "読み込んでいます…";
  showSkeleton(el("home-consultations"), 2);
  card.hidden = false;
  const res = await apiFetch("/api/consultations");
  if (!res.ok) {
    el("home-consultations-count").textContent = "読み込めませんでした。読み直してください";
    clear(el("home-consultations"));
    return;
  }
  renderConsultations(await res.json());
}

function renderConsultations(body) {
  const list = el("home-consultations");
  clear(list);
  const items = body?.consultations || [];
  // ホームの「ご本人からの相談」のタイルに、未対応の数を出す（使いやすさの改修の計画.md 4-2の5）
  setTileCount("consultations", body?.unanswered_count || 0);
  el("home-consultations-count").textContent =
    items.length === 0
      ? "まだ相談は届いていません。"
      : `${items.length}件（未対応 ${body.unanswered_count}件）`;

  for (const item of items) {
    const li = node("li", "attention");
    const label = `${item.full_name || "ご本人"}：${item.category_label}（${item.status_label}）`;
    const button = node("button", "link-button", label);
    button.type = "button";
    button.addEventListener("click", () => {
      openDetail(item.survivor_id).catch((err) => showNotice(err.message));
    });
    li.append(button);
    if (item.urgency_label) {
      li.append(node("span", urgencyClass(item.urgency), `急ぎ：${item.urgency_label}`));
    }
    if (item.emergency_flag) {
      li.append(node("span", "badge badge-high", "急を要する様子あり"));
    }
    li.append(node("span", "row-note", formatDateTime(item.consulted_at)));
    list.append(li);
  }
}

function urgencyClass(urgency) {
  if (urgency === "high") return "badge badge-high";
  if (urgency === "low") return "badge badge-low";
  return "badge badge-note";
}

// --- 支援者のホームの「組織につながっていない方からの相談」（追加修正②） ---

/** 公開の相談を読む（支援者全員。組織に入っていない支援者も）。
 *
 *  ホームの表示を止めないように、呼ぶ側は待たずに呼んでよい。 */
export async function loadPublicConsultations() {
  const card = el("home-public-consultations-card");
  if (!isSupporter()) {
    card.hidden = true;
    return;
  }
  el("home-public-consultations-count").textContent = "読み込んでいます…";
  showSkeleton(el("home-public-consultations"), 2);
  card.hidden = false;
  const res = await apiFetch("/api/consultations/public");
  if (!res.ok) {
    el("home-public-consultations-count").textContent = "読み込めませんでした。読み直してください";
    clear(el("home-public-consultations"));
    return;
  }
  renderPublicConsultations(await res.json());
}

function renderPublicConsultations(body) {
  const list = el("home-public-consultations");
  clear(list);
  const items = body?.consultations || [];
  el("home-public-consultations-count").textContent =
    items.length === 0
      ? "いまはありません。"
      : `${items.length}件（未対応 ${body.unanswered_count}件）`;
  for (const item of items) {
    list.append(renderPublicConsultation(item, Boolean(body.can_reply)));
  }
}

/** 公開の相談1件（お名前は出さない。出すのは市区町村まで）。 */
function renderPublicConsultation(row, canReply) {
  const li = node("li", "visit");

  const head = node("div", "visit-head");
  head.append(node("span", "visit-when", formatDateTime(row.consulted_at)));
  head.append(node("span", "badge badge-note", row.category_label));
  head.append(node("span", statusClass(row.status), row.status_label));
  if (row.urgency_label) {
    head.append(node("span", urgencyClass(row.urgency), `急ぎ：${row.urgency_label}`));
  }
  if (row.emergency_flag) head.append(node("span", "badge badge-high", "急を要する様子あり"));
  li.append(head);

  const place = node("p", "note", "お住まい：読み込んでいます…");
  li.append(place);
  describeMunicipality(row.municipality_code)
    .then((label) => {
      place.textContent = `お住まい：${label || "（分かりません）"}`;
    })
    .catch(() => {
      place.textContent = "お住まい：（分かりません）";
    });

  li.append(renderBody(row, { showDraft: true }));

  const messagesUrl = `/api/consultations/public/${encodeURIComponent(
    row.consultation_id,
  )}/messages`;
  li.append(renderThreadPanel(row, { messagesUrl, canWrite: false }));
  if (canReply) li.append(renderReplyForm(row, { isPublic: true }));
  return li;
}

async function sendPublicReply(consultationId, body) {
  const res = await apiFetch(
    `/api/consultations/public/${encodeURIComponent(consultationId)}/replies`,
    { method: "POST", body: JSON.stringify(body) },
  );
  if (!res.ok) throw new Error(await readError(res));
  await loadPublicConsultations();
  showNotice("返事を送りました。ご本人の画面に出ます。", "ok");
}

/** 整えた9つの項目（「記載なし」は出さない）。 */
function renderItems(row) {
  const items = node("dl", "pairs");
  for (const field of visitFields()) {
    const value = row.items?.[field.value];
    if (!value || value === NOT_RECORDED) continue;
    items.append(node("dt", null, field.label));
    items.append(node("dd", null, value));
  }
  return items;
}

/**
 * 相談の中身（画面の改修の計画.md 6-2）。ご本人の文章をいちばん上に出す（正本）。
 * 支援者には、その下に「AIが整えた案」（9つの項目と急ぎの根拠）を出す。ご本人には案を見せない。
 */
function renderBody(row, { showDraft }) {
  const wrap = node("div", "consultation-body");
  if (row.raw_text) {
    wrap.append(node("p", "consultation-raw", row.raw_text));
  } else {
    // 前の作りで、文章を書かずに項目だけで送った相談
    wrap.append(renderItems(row));
    return wrap;
  }
  if (!showDraft) return wrap;

  const draft = node("details", "visit-raw");
  draft.open = true;
  draft.append(node("summary", null, "AIが整えた案（読む助け。ご本人の文章が正本です）"));
  if (row.ai_used) {
    draft.append(renderItems(row));
  } else {
    draft.append(node("p", "note", "AIで整えられませんでした。上の文章を読んでください。"));
  }
  if (row.urgency_evidence) {
    draft.append(node("p", "note", `急ぎと判断した根拠：${row.urgency_evidence}`));
  }
  wrap.append(draft);
  return wrap;
}

// --- 詳細の画面の「相談」 ---

/** 詳細の画面の相談を描く（f1.js が詳細を描いたあとに呼ばれる）。 */
export function renderDetailConsultations(detailBody) {
  detail = detailBody;
  const card = el("detail-consultations-card");
  // 相談を見られるのは本人と同じ組織の専門職だけ（要件定義書7-1）
  if (!isSurvivor() && !isProfessional()) {
    card.hidden = true;
    return;
  }

  const rows = detailBody?.survivor?.consultations || [];
  const list = el("detail-consultations");
  clear(list);
  if (rows.length === 0) {
    list.append(
      node(
        "li",
        "note",
        isSurvivor()
          ? "まだ相談はありません。困っていることがあれば「相談する」から送れます。"
          : "まだ相談は届いていません。",
      ),
    );
  } else {
    for (const row of rows) list.append(renderConsultation(row));
  }

  el("consultation-new").hidden = !isSurvivor();
  el("detail-consultations-note").textContent = isSurvivor()
    ? "送った相談と、相談員からの返事を見られます。続けて伝えたいときは「やり取りを見る・書き足す」から送れます。" +
      (isPublicSender()
        ? "支援の組織につながっていないあいだの相談は、組織を問わず支援者が見られます（お名前は出ません）。"
        : "")
    : "ご本人から届いた相談です。返事を書くと、ご本人の画面に出ます。";
  card.hidden = false;
}

function renderConsultation(row) {
  const li = node("li", "visit");

  const head = node("div", "visit-head");
  head.append(node("span", "visit-when", formatDateTime(row.consulted_at)));
  head.append(node("span", "badge badge-note", row.category_label));
  head.append(node("span", statusClass(row.status), row.status_label));
  // 急ぎの度合いは本人向けの応答に入らない（要件定義書10-3）
  if (row.urgency_label) {
    head.append(node("span", urgencyClass(row.urgency), `急ぎ：${row.urgency_label}`));
  }
  if (row.emergency_flag) head.append(node("span", "badge badge-high", "急を要する様子あり"));
  // 組織につながっていないときに送った相談（追加修正②。組織を問わず支援者が見られる）
  if (row.is_public) head.append(node("span", "badge badge-medium", "公開の相談"));
  li.append(head);

  // ご本人には、書いた文章だけを出す（AIの案は相談員が読むためのもの）
  li.append(renderBody(row, { showDraft: !isSurvivor() }));

  if (row.last_reply_text) {
    const reply = node("div", "proposal");
    reply.append(node("h3", "proposal-title", "相談員からの最新の返事"));
    reply.append(node("p", null, row.last_reply_text));
    if (row.last_next_action) {
      reply.append(node("p", "note", `次の対応：${row.last_next_action}`));
    }
    reply.append(node("p", "note", formatDateTime(row.last_replied_at)));
    li.append(reply);
  }

  // やり取りを全部見る・本人が書き足す（要望7。要件定義書には無い追加の機能）
  li.append(renderThreadPanel(row));

  if (isProfessional()) li.append(renderReplyForm(row));
  return li;
}

// --- やり取り（返事・本人の書き足し。要望7） ---

/** その相談のやり取りの道（公開の相談は相談のIDだけで読む。追加修正②）。 */
function threadUrl(consultationId) {
  return (
    `/api/survivors/${encodeURIComponent(survivorId())}/consultations/` +
    `${encodeURIComponent(consultationId)}/messages`
  );
}

/** その相談のやり取りを古い順に読む。 */
async function fetchThread(url) {
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(await readError(res));
  const body = await res.json();
  return body.messages || [];
}

/** 本人が、同じ相談に書き足す。 */
async function sendMessage(consultationId, messageText) {
  const res = await apiFetch(
    `/api/survivors/${encodeURIComponent(survivorId())}/consultations/` +
      `${encodeURIComponent(consultationId)}/messages`,
    { method: "POST", body: JSON.stringify({ message: messageText }) },
  );
  if (!res.ok) throw new Error(await readError(res));
}

function renderMessage(row) {
  const p = node("p", null);
  const who = row.from_survivor ? "ご本人" : "相談員";
  p.append(node("strong", null, `${who}（${formatDateTime(row.created_at)}）`));
  p.append(document.createElement("br"));
  p.append(document.createTextNode(row.text || ""));
  return p;
}

/** 「やり取りを見る・書き足す」の折りたたみ（相談ごとに作る。IDは付けない）。
 *
 *  開いたときに読み込む（相談ごとにAPIを1回増やすため、閉じたままなら読まない）。
 *  公開の相談（追加修正②）は `messagesUrl` を渡し、書き足しの欄は出さない。 */
function renderThreadPanel(row, { messagesUrl = "", canWrite = isSurvivor() } = {}) {
  const details = node("details", "visit-raw");
  details.append(
    node("summary", null, canWrite ? "やり取りを見る・書き足す" : "やり取りを見る"),
  );

  const list = node("div", null);
  details.append(list);

  let loaded = false;
  const reload = () =>
    fetchThread(messagesUrl || threadUrl(row.consultation_id))
      .then((items) => {
        clear(list);
        if (items.length === 0) {
          list.append(node("p", "note", "まだやり取りはありません。"));
        }
        for (const item of items) list.append(renderMessage(item));
      })
      .catch((err) => {
        clear(list);
        list.append(node("p", "note", err.message));
      });

  details.addEventListener("toggle", () => {
    if (details.open && !loaded) {
      loaded = true;
      reload();
    }
  });

  if (canWrite) {
    const form = node("form", "form");
    const label = node("label", "field");
    label.append(node("span", "field-label", "書き足す"));
    const textarea = document.createElement("textarea");
    textarea.rows = 2;
    textarea.required = true;
    label.append(textarea);
    form.append(label);
    const error = node("p", "form-error");
    error.hidden = true;
    form.append(error);
    const send = node("button", "button button-primary", "送る");
    send.type = "submit";
    form.append(send);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      error.hidden = true;
      withBusy(send, () =>
        sendMessage(row.consultation_id, textarea.value)
          .then(() => {
            textarea.value = "";
            loaded = true;
            return reload();
          })
          .catch((err) => {
            error.textContent = err.message;
            error.hidden = false;
          }),
      );
    });
    details.append(form);
  }

  return details;
}

function statusClass(status) {
  if (status === "closed") return "badge badge-low";
  if (status === "in_progress") return "badge badge-note";
  return "badge badge-high";
}

/** 専門職が返事と状況を書く欄（相談ごとに作る。IDは付けない）。
 *
 *  公開の相談（`isPublic`。追加修正②）は公開の経路で返事を書く。状況は返事と一緒にだけ
 *  変えられる（「対応済みにする」だけのボタンは出さない）。 */
function renderReplyForm(row, { isPublic = false } = {}) {
  const box = node("div", "proposal");
  box.append(node("h3", "proposal-title", "返事を書く"));

  const replyLabel = node("label", "field");
  replyLabel.append(node("span", "field-label", "返事"));
  const reply = document.createElement("textarea");
  reply.rows = 3;
  reply.required = true;
  replyLabel.append(reply);
  box.append(replyLabel);

  const nextLabel = node("label", "field");
  nextLabel.append(node("span", "field-label", "次の対応"));
  const next = document.createElement("textarea");
  next.rows = 2;
  nextLabel.append(next);
  box.append(nextLabel);

  const statusLabel = node("label", "field");
  statusLabel.append(node("span", "field-label", "相談の状況"));
  const select = document.createElement("select");
  statusLabel.append(select);
  fillSelect(select, statuses(), { placeholder: "変えない" });
  box.append(statusLabel);

  const error = node("p", "form-error");
  error.hidden = true;
  box.append(error);

  const actions = node("div", "actions");
  const send = node("button", "button button-primary", "返事を送る");
  send.type = "button";
  send.addEventListener("click", () => {
    const body = {
      reply_text: reply.value,
      next_action: next.value,
      status: select.value || null,
    };
    return withBusy(send, () =>
      (isPublic
        ? sendPublicReply(row.consultation_id, body)
        : sendReply(row.consultation_id, body)
      ).catch((err) => {
        error.textContent = err.message;
        error.hidden = false;
      }),
    );
  });
  actions.append(send);
  box.append(actions);
  if (isPublic) return box;

  const close = node("button", "button", "対応済みにする");
  close.type = "button";
  close.addEventListener("click", () =>
    withBusy(close, () =>
      changeStatus(row.consultation_id, "closed").catch((err) => {
        error.textContent = err.message;
        error.hidden = false;
      }),
    ),
  );
  actions.append(close);
  return box;
}

async function sendReply(consultationId, body) {
  const id = survivorId();
  const res = await apiFetch(
    `/api/survivors/${encodeURIComponent(id)}/consultations/${encodeURIComponent(
      consultationId,
    )}/replies`,
    { method: "POST", body: JSON.stringify(body) },
  );
  if (!res.ok) throw new Error(await readError(res));
  // 知らせは読み直したあとに出す（詳細の読み直しが知らせを消すため）
  await openDetail(id);
  showNotice("返事を送りました。ご本人の画面に出ます。", "ok");
}

async function changeStatus(consultationId, status) {
  const id = survivorId();
  const res = await apiFetch(
    `/api/survivors/${encodeURIComponent(id)}/consultations/${encodeURIComponent(
      consultationId,
    )}`,
    { method: "PATCH", body: JSON.stringify({ status }) },
  );
  if (!res.ok) throw new Error(await readError(res));
  await openDetail(id);
  showNotice("相談の状況を変えました。", "ok");
}

// --- 相談を送る画面（ご本人） ---

/** 「相談する」を押したとき（ホームからも詳細からも開ける）。 */
export function openConsultationNew() {
  showNotice("");
  if (!survivorId()) {
    showNotice("ご自分の記録が見つかりません。ホームから開き直してください");
    return;
  }
  const survivor = detail?.survivor;
  el("consultation-subtitle").textContent = survivor
    ? `${survivor.full_name}さんとして送ります`
    : "ご自分の相談として送ります";
  // 命に関わるときの案内は、書く前から出しておく（要件定義書F8）
  el("consultation-emergency-guide").textContent = guideText();
  // 組織につながっていないときは、公開になることを書く前から知らせる（追加修正②）
  const publicNotice = el("consultation-public-notice");
  publicNotice.textContent = isPublicSender() ? publicNoticeText() : "";
  publicNotice.hidden = !isPublicSender();
  el("consultation-raw-text").value = "";
  // 種類は選ばなくても送れる（選ばなければ AI の案を使う。画面の改修の計画.md 6-2）
  fillSelect(el("consultation-category"), categories(), { placeholder: "選ばない" });
  el("consultation-category").value = "";
  showFormError(el("form-consultation"), "");
  showScreen("screen-consultation-new");
}

// 案内の文言はAPIが返すものだけを使う（画面に書き写すとずれる。
// 正本は `services/consultations.py` の EMERGENCY_GUIDE）。
let emergencyGuide = "";

function guideText() {
  return emergencyGuide || choices().consultation_emergency_guide || "";
}

async function submitConsultation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const rawText = el("consultation-raw-text").value.trim();
  if (!rawText) {
    showFormError(form, "いまの状態や困っていることを書いてください");
    el("consultation-raw-text").focus();
    return;
  }

  const body = {
    raw_text: rawText,
    // 空なら送らない（サーバーが AI の案を使う）
    category: el("consultation-category").value || null,
  };

  // 公開になる相談は、送る前にもう一度確かめてもらう（追加修正②。文言はAPIが返す）
  if (
    isPublicSender() &&
    !(await confirmDialog(`${publicNoticeText()}\n\nこの内容で送りますか？`, { ok: "送る" }))
  ) {
    return;
  }

  const id = survivorId();
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(id)}/consultations`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    const sent = await res.json();
    if (sent.emergency_guide) emergencyGuide = sent.emergency_guide;
    await openDetail(id);
    // AIが「急を要する」と見たときは、送れた知らせの代わりに案内を出す（要件定義書F8）
    if (sent.emergency_notice) {
      showNotice(`相談を送りました。\n${sent.emergency_notice}`, "ng");
    } else {
      showNotice("相談を送りました。相談員からの返事は、この画面に出ます。", "ok");
    }
  });
}

// --- イベントの登録 ---

export function bind({ openDetail: openDetailHandler }) {
  if (openDetailHandler) openDetail = openDetailHandler;

  el("consultation-new").addEventListener("click", openConsultationNew);
  el("form-consultation").addEventListener("submit", submitConsultation);
  el("home-consultations-reload").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () =>
      loadConsultations().catch((err) => showNotice(err.message)),
    ),
  );
  el("home-public-consultations-reload").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () =>
      loadPublicConsultations().catch((err) => showNotice(err.message)),
    ),
  );
}
