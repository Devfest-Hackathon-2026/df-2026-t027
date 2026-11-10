// 画面の部品（F0・F1で共通に使う。開発手順書 Step 5-7）
//
// - 画面の切り替えは `.screen` を全部隠して1つだけ出す（画面が増えても直す場所を増やさない）。
// - `hidden` は CSS で `display: none !important` にしてある。
//   `[hidden]` のブラウザ既定は `.form { display: flex }` に負けるため（Step 4 で踏んだ）。
// - 日時は日本時間で出す。

export const el = (id) => document.getElementById(id);

// --- 画面の切り替えと「戻る」（使いやすさの改修の計画.md 4-2の1） ---
//
// 画面を切り替えるたびに、ブラウザの履歴を1つ足す。スマートフォンの「戻る」で、
// アプリから出ずに前の画面に戻れるようにする（前は履歴を残さず、アプリそのものから出ていた）。
// - ログイン・選択・ホームなどの「入口の画面」は履歴を足さず、そこまでの履歴を戻して置き換える
//   （ホームで戻るを押したら、アプリの前のページに戻る。何度もホームが重ならない）。
// - 履歴に入れるのは画面のIDだけ（記録のIDや氏名は入れない）。

const ENTRY_SCREENS = new Set(["screen-loading", "screen-login", "screen-choice", "screen-home"]);
let currentScreen = "";
let depth = 0;
let skipNextPop = false;
let historyGuard = (id) => id;
let onHistoryShown = () => {};

/** 戻るで出してよい画面を決める関数と、戻ったあとに呼ぶ関数を受け取る（app.js から）。 */
export function setHistoryHandlers({ guard, onShown } = {}) {
  if (guard) historyGuard = guard;
  if (onShown) onHistoryShown = onShown;
}

function writeHistory(id) {
  try {
    if (ENTRY_SCREENS.has(id) || !currentScreen) {
      if (depth > 0) {
        // 入口の画面に戻るときは、足した履歴を戻してから置き換える（popstate は1回読み飛ばす）
        skipNextPop = true;
        const steps = depth;
        depth = 0;
        history.go(-steps);
        return;
      }
      history.replaceState({ screen: id, depth: 0 }, "");
      return;
    }
    depth += 1;
    history.pushState({ screen: id, depth }, "");
  } catch (err) {
    // 履歴を使えない環境（埋め込みの表示など）でも、画面の切り替えは止めない
  }
}

/** `.screen` のうち1つだけを出す。 */
export function showScreen(id, { fromHistory = false } = {}) {
  for (const node of document.querySelectorAll(".screen")) {
    node.hidden = node.id !== id;
  }
  window.scrollTo({ top: 0 });
  if (!fromHistory && id !== currentScreen) writeHistory(id);
  currentScreen = id;
}

export function currentScreenId() {
  return currentScreen;
}

window.addEventListener("popstate", (event) => {
  if (skipNextPop) {
    skipNextPop = false;
    try {
      history.replaceState({ screen: currentScreen, depth: 0 }, "");
    } catch (err) {
      // 上と同じ
    }
    return;
  }
  const target = event.state?.screen;
  depth = Number(event.state?.depth) || 0;
  if (!target || !document.getElementById(target)) return;
  const allowed = historyGuard(target);
  if (!allowed) return;
  showNotice("");
  showScreen(allowed, { fromHistory: true });
  onHistoryShown(allowed);
});

// --- 知らせ（使いやすさの改修の計画.md 4-2の4） ---
//
// 画面の下に浮かべて出す（前は画面の一番上に出て、スクロールしていると見えなかった）。
// 「保存しました」（ok）は少しで消す。誤りと注意は、閉じるまで残す。

const NOTICE_OK_MS = 3500;
let noticeTimer = null;

/** 画面の下に出す知らせ（kind は ok・ng・warn）。 */
export function showNotice(text, kind = "ng") {
  const box = el("notice");
  const label = el("notice-text") || box;
  window.clearTimeout(noticeTimer);
  if (!text) {
    box.hidden = true;
    label.textContent = "";
    return;
  }
  label.textContent = text;
  box.className = `toast notice-${kind}`;
  box.setAttribute("role", kind === "ng" ? "alert" : "status");
  box.hidden = false;
  if (kind === "ok") {
    noticeTimer = window.setTimeout(() => {
      box.hidden = true;
    }, NOTICE_OK_MS);
  }
}

// --- 入力の誤りを欄ごとに出す（使いやすさの改修の計画.md 4-2の2） ---
//
// 送る前に、必須の欄が空でないか・数の範囲を画面で確かめる。誤りは欄のすぐ下に出し、
// 最初の誤りの欄へ動かす。サーバーの確かめはそのまま残す（画面の確かめは手助けだけ）。
// 判定は HTML の `required`・`min`・`max` だけを使う（サーバーの必須と合っていることは
// 05_tests/test_frontend_required.py が確かめている）。

function fieldOf(control) {
  return control?.closest(".field") || null;
}

function labelOf(field) {
  return (field?.querySelector(".field-label")?.textContent || "").trim();
}

function isShown(control) {
  return !control.disabled && !control.closest("[hidden]");
}

function clearFieldError(control) {
  const field = fieldOf(control);
  if (!field) return;
  field.classList.remove("field-invalid");
  field.querySelector(":scope > .field-error")?.remove();
  control.removeAttribute("aria-invalid");
}

/** フォームの中の、欄ごとの誤りを消す。 */
export function clearFieldErrors(form) {
  for (const field of form.querySelectorAll(".field-invalid")) {
    field.classList.remove("field-invalid");
    field.querySelector(":scope > .field-error")?.remove();
  }
  for (const control of form.querySelectorAll("[aria-invalid]")) {
    control.removeAttribute("aria-invalid");
  }
}

function markFieldError(control, message) {
  const field = fieldOf(control);
  if (!field) return;
  field.classList.add("field-invalid");
  control.setAttribute("aria-invalid", "true");
  let box = field.querySelector(":scope > .field-error");
  if (!box) {
    box = node("span", "field-error");
    field.append(box);
  }
  box.textContent = message;
}

function problemOf(control) {
  const value = (control.value || "").trim();
  const label = labelOf(fieldOf(control)) || "この欄";
  if (control.required && !value) {
    return control.tagName === "SELECT" ? `${label}を選んでください` : `${label}を入れてください`;
  }
  if (value && control.type === "number") {
    const number = Number(value);
    const min = control.min === "" ? null : Number(control.min);
    const max = control.max === "" ? null : Number(control.max);
    if (!Number.isInteger(number)) return `${label}は数字（半角）で入れてください`;
    if ((min !== null && number < min) || (max !== null && number > max)) {
      return `${label}は${min ?? ""}から${max ?? ""}の間で入れてください`;
    }
  }
  return "";
}

const SKIPPED_TYPES = new Set(["checkbox", "radio", "hidden", "button", "submit"]);

/** フォームを送る前に確かめる。誤りがあれば欄の下に出し、最初の欄へ動いて false を返す。
 *  `section` を渡すと、その中の欄だけを確かめる（登録の1段目など）。 */
export function validateForm(form, section = form) {
  clearFieldErrors(section);
  const problems = [];
  for (const control of section.querySelectorAll("input, select, textarea")) {
    if (!isShown(control) || SKIPPED_TYPES.has(control.type)) continue;
    const message = problemOf(control);
    if (message) problems.push([control, message]);
  }
  for (const [control, message] of problems) markFieldError(control, message);
  if (problems.length === 0) return true;
  const [first] = problems[0];
  const errorBox = section.querySelector(".form-error") ? section : form;
  showFormError(errorBox, `入力を確かめてください（${problems.length}か所）`, { markField: false });
  fieldOf(first)?.scrollIntoView({ behavior: "smooth", block: "center" });
  first.focus({ preventScroll: true });
  return false;
}

/** すべてのフォームで、送る前の確かめを行う（app.js が1回だけ呼ぶ）。
 *  先に（capture で）確かめ、誤りがあれば各画面の送る処理まで届けない。 */
export function installFormValidation() {
  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || "skipValidation" in form.dataset) return;
      if (!validateForm(form)) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    },
    true,
  );
  const clearOnEdit = (event) => {
    const control = event.target;
    if (control instanceof Element && control.matches("input, select, textarea")) {
      clearFieldError(control);
    }
  };
  document.addEventListener("input", clearOnEdit, true);
  document.addEventListener("change", clearOnEdit, true);
}

/** サーバーの誤りの文に、欄の名前が入っていれば、その欄にも印を付ける。 */
function markServerError(form, text) {
  let best = null;
  let bestLength = 0;
  for (const field of form.querySelectorAll(".field")) {
    if (field.closest("[hidden]")) continue;
    const label = labelOf(field).replace(/（.*?）/g, "");
    if (label.length >= 2 && text.includes(label) && label.length > bestLength) {
      best = field;
      bestLength = label.length;
    }
  }
  const control = best?.querySelector("input, select, textarea");
  if (!control) return;
  markFieldError(control, text);
  best.scrollIntoView({ behavior: "smooth", block: "center" });
}

export function showFormError(form, text, { markField = true } = {}) {
  if (!text) clearFieldErrors(form);
  const box = form.querySelector(".form-error");
  if (!box) return;
  box.textContent = text || "";
  box.hidden = !text;
  if (text && markField) {
    markServerError(form, text);
    if (!form.querySelector(".field-invalid")) {
      box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}

// --- 確かめの枠（使いやすさの改修の計画.md 4-2の6） ---
//
// ブラウザの標準の枠（window.confirm）は文字が小さく、スマートフォンで読みにくい。
// 画面の中に大きな文字の枠を出し、「はい」「やめる」を大きなボタンにする。

/** 確かめの枠を出し、「はい」なら true を返す。 */
export function confirmDialog(message, { ok = "はい", cancel = "やめる", danger = false } = {}) {
  const dialog = el("confirm-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    return Promise.resolve(window.confirm(message));
  }
  el("confirm-message").textContent = message;
  const okButton = el("confirm-ok");
  const cancelButton = el("confirm-cancel");
  okButton.textContent = ok;
  cancelButton.textContent = cancel;
  okButton.classList.toggle("button-danger", danger);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      okButton.removeEventListener("click", onOk);
      cancelButton.removeEventListener("click", onCancel);
      dialog.removeEventListener("close", onClose);
      if (dialog.open) dialog.close();
      resolve(value);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onClose = () => finish(false);
    okButton.addEventListener("click", onOk);
    cancelButton.addEventListener("click", onCancel);
    dialog.addEventListener("close", onClose);
    dialog.showModal();
    cancelButton.focus();
  });
}

// --- 読み込み中の形（使いやすさの改修の計画.md 4-2の7） ---

/** 一覧を読むあいだ、灰色の形（中身の入る場所）を出す。 */
export function showSkeleton(container, rows = 3) {
  if (!container) return;
  clear(container);
  const tag = container.tagName === "UL" || container.tagName === "OL" ? "li" : "div";
  for (let i = 0; i < rows; i += 1) {
    const row = node(tag, "skeleton");
    row.setAttribute("aria-hidden", "true");
    row.append(node("span", "skeleton-line skeleton-line-wide"), node("span", "skeleton-line"));
    container.append(row);
  }
  container.setAttribute("aria-busy", "true");
}

/** 読み終えたら呼ぶ（`showSkeleton` の印を外す）。 */
export function doneSkeleton(container) {
  if (!container) return;
  container.removeAttribute("aria-busy");
  for (const row of container.querySelectorAll(":scope > .skeleton")) row.remove();
}

// --- ホームのタイルの数（使いやすさの改修の計画.md 4-2の5） ---

/** ホームのタイルの右上に、対応が要る数を出す（0なら隠す）。 */
export function setTileCount(key, count) {
  for (const badge of document.querySelectorAll(`[data-count-key="${key}"]`)) {
    const value = Number(count) || 0;
    badge.textContent = value > 99 ? "99+" : String(value);
    badge.hidden = value === 0;
  }
}

// --- 文字の大きさ（使いやすさの改修の計画.md 4-2の8） ---
//
// この端末にだけ覚える（ほかの端末やほかの人には効かない）。覚えられない環境でも画面は動く。

const TEXT_SIZE_KEY = "tsunagi.textSize";

export function savedTextSize() {
  try {
    return window.localStorage.getItem(TEXT_SIZE_KEY) || "";
  } catch (err) {
    return "";
  }
}

export function applyTextSize(size) {
  document.documentElement.classList.toggle("text-large", size === "large");
}

export function saveTextSize(size) {
  try {
    window.localStorage.setItem(TEXT_SIZE_KEY, size);
  } catch (err) {
    // 覚えられなくても、いまの画面には効かせる
  }
  applyTextSize(size);
}

/** 送信中はボタンを押せなくする（二重押しの防止）。 */
export async function withBusy(button, task) {
  if (!button) return task();
  // 中身（アイコンの SVG を含む）をそのまま取っておき、終わったら戻す。
  // 文字だけを戻すと、アイコンのボタンからアイコンが消える（使いやすさの改修の計画.md 2章）
  const saved = Array.from(button.childNodes);
  const iconOnly = button.classList.contains("icon-button");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  // アイコンだけのボタンは幅が狭いので、文字にせず、くるくる回る印にする
  if (iconOnly) button.classList.add("is-busy");
  else button.textContent = "処理中…";
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.classList.remove("is-busy");
    if (!iconOnly) button.replaceChildren(...saved);
  }
}

export function fillSelect(select, items, { placeholder } = {}) {
  select.innerHTML = "";
  if (placeholder) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = placeholder;
    select.append(option);
  }
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    select.append(option);
  }
}

export function clear(node) {
  node.innerHTML = "";
}

/** 要素を1つ作る（class と文字列をまとめて指定できるようにする）。 */
export function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

/** 「お名前（年齢）」の形にする。年齢が空なら「年齢未入力」と出す（本人の登録で後で入れてよいため）。 */
export function nameWithAge(name, age) {
  const shown = name || "（名前なし）";
  return age === null || age === undefined || age === "" ? `${shown}（年齢未入力）` : `${shown}（${age}歳）`;
}

/** 「お名前：値」の組を dl に足す。 */
export function addPair(list, label, value) {
  list.append(node("dt", null, label));
  list.append(node("dd", null, value === null || value === undefined || value === "" ? "—" : value));
}

/** 日時を日本時間で「11/8 21:30」の形にする。 */
export function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("ja-JP", {
    timeZone: "Asia/Tokyo",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 日付を日本時間で「2026/11/8」の形にする。 */
export function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString("ja-JP", { timeZone: "Asia/Tokyo" });
}
