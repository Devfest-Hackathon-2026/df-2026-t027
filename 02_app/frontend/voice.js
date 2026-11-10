// 声で入力する（使いやすさの改修の計画.md 13章）
//
// - ブラウザの音声の文字起こし（Web Speech API）を使う。**Gemini は使わない**（無料枠の回数を使わない）。
// - 相談の文章の欄（`consultation-raw-text`）と、訪問・見守りの記録の欄（`visit-raw-text`）の下に
//   マイクのボタンを置く。押して話すと文字になって欄に入り、もう一度押すと止まる。
// - 使えないブラウザでは、ボタンを出さない。代わりに、キーボードのマイクで話せることを案内する。
// - ブラウザによっては、声がブラウザの会社のサーバーに送られて文字になる。健康の話が入るので、
//   **初めて押したときに了承をもらう**。了承はこの端末にだけ覚える（覚えられなくても動く）。
// - **声も文字もログに出さない**（要件定義書11-3）。コンソールにも出さない。
// - 文字になったあと、送る前に本人が読んで直せる（今の流れと同じ。送るのは各画面のボタン）。

import { icon } from "./icons.js";
import { confirmDialog, el, node, showNotice } from "./ui.js";

const CONSENT_KEY = "tsunagi.voiceConsent";
export const CONSENT_MESSAGE =
  "声は文字にするために、ブラウザの会社のサーバーに送られることがあります。" +
  "健康のことを話すときも同じです。了承して使いますか。";
export const UNSUPPORTED_NOTE = "キーボードのマイクのボタンでも話して入力できます";
const LISTENING_NOTE = "聞いています。話し終えたら「止める」を押してください。";

// 止まった理由ごとの知らせ（声の中身は入れない）
const ERROR_MESSAGES = {
  "not-allowed": "マイクを使えません。ブラウザの設定でマイクを許可してください",
  "service-not-allowed": "このブラウザでは声の入力を使えません。" + UNSUPPORTED_NOTE,
  "audio-capture": "マイクが見つかりません。" + UNSUPPORTED_NOTE,
  network: "通信ができず、声を文字にできませんでした。" + UNSUPPORTED_NOTE,
  "no-speech": "声が聞き取れませんでした。もう一度押して話してください",
};

// いま聞いている1回分（同時に聞くのは1つの欄だけ）
let active = null;

function recognitionClass() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

/** このブラウザで声の入力を使えるか。 */
export function isSupported() {
  return Boolean(recognitionClass());
}

function hasConsent() {
  try {
    return window.localStorage.getItem(CONSENT_KEY) === "1";
  } catch (err) {
    return false;
  }
}

function saveConsent() {
  try {
    window.localStorage.setItem(CONSENT_KEY, "1");
  } catch (err) {
    // 覚えられないときは、次に押したときにもう一度聞く
  }
}

/** 相談と訪問・見守りの記録の欄に、声の入力を付ける（app.js が1回だけ呼ぶ）。 */
export function installVoiceInput() {
  attach(el("consultation-raw-text"));
  attach(el("visit-raw-text"));
}

/** 文章の欄1つに、マイクのボタン（使えなければ案内）を付ける。 */
function attach(textarea) {
  if (!textarea) return;
  const field = textarea.closest(".field") || textarea;
  const row = node("div", "voice-row");
  if (!isSupported()) {
    row.append(node("p", "field-note", UNSUPPORTED_NOTE));
    field.after(row);
    return;
  }
  const button = node("button", "button voice-button");
  button.type = "button";
  const status = node("span", "field-note voice-status");
  status.setAttribute("aria-live", "polite");
  setButton(button, status, false);
  button.addEventListener("click", () =>
    toggle(textarea, button, status).catch((err) => showNotice(err.message)),
  );
  row.append(button, status);
  field.after(row);

  // 画面を移ったら止める（隠れた欄に聞き続けない）
  const screen = textarea.closest(".screen");
  if (screen) {
    new MutationObserver(() => {
      if (screen.hidden && active?.textarea === textarea) stopVoiceInput();
    }).observe(screen, { attributes: true, attributeFilter: ["hidden"] });
  }
  // 本人が手で直し始めたら止める（文字起こしで、直した文字を上書きしない）
  textarea.addEventListener("input", (event) => {
    if (event.isTrusted && active?.textarea === textarea) stopVoiceInput({ discard: true });
  });
  // 送るときも止める
  textarea.form?.addEventListener("submit", () => stopVoiceInput(), true);
}

function setButton(button, status, listening) {
  button.replaceChildren(
    icon(listening ? "mic-off" : "mic"),
    document.createTextNode(listening ? "止める" : "声で入力"),
  );
  button.setAttribute("aria-pressed", String(listening));
  button.setAttribute("aria-label", listening ? "声の入力を止める" : "声で入力する");
  button.classList.toggle("voice-on", listening);
  status.textContent = listening ? LISTENING_NOTE : "";
}

async function toggle(textarea, button, status) {
  if (active?.textarea === textarea) {
    stopVoiceInput();
    return;
  }
  stopVoiceInput();
  if (!hasConsent()) {
    const ok = await confirmDialog(CONSENT_MESSAGE, { ok: "了承して使う", cancel: "使わない" });
    if (!ok) return;
    saveConsent();
  }
  start(textarea, button, status);
}

function start(textarea, button, status) {
  const Recognition = recognitionClass();
  if (!Recognition) return;
  const recognition = new Recognition();
  recognition.lang = "ja-JP";
  recognition.interimResults = true;
  recognition.continuous = true;
  // 押したときの欄の中身を残し、その後ろに足していく
  const session = { recognition, textarea, button, status, base: textarea.value, closed: false };

  recognition.onresult = (event) => {
    if (session.closed) return;
    let finals = "";
    let interim = "";
    for (let i = 0; i < event.results.length; i += 1) {
      const result = event.results[i];
      const text = result[0]?.transcript || "";
      if (result.isFinal) finals += text;
      else interim += text;
    }
    session.finals = finals;
    write(session, finals + interim);
  };
  recognition.onerror = (event) => {
    const message = ERROR_MESSAGES[event.error];
    // 止めたとき（aborted）は知らせない。理由の名前だけを使い、声の中身は出さない
    if (message) showNotice(message, "warn");
  };
  recognition.onend = () => {
    if (!session.closed) {
      // 途中の文字（確定していない分）は落とし、確定した分だけを残す
      write(session, session.finals || "");
      session.closed = true;
    }
    if (active === session) active = null;
    setButton(button, status, false);
  };

  active = session;
  setButton(button, status, true);
  try {
    recognition.start();
  } catch (err) {
    session.closed = true;
    active = null;
    setButton(button, status, false);
    showNotice("声の入力を始められませんでした。もう一度お試しください");
  }
}

/** 欄に「押したときの中身」＋「文字になった分」を入れる。 */
function write(session, added) {
  const { textarea, base } = session;
  const separator = base && added && !/\s$/.test(base) ? "\n" : "";
  textarea.value = added ? `${base}${separator}${added}` : base;
  // 欄の誤りの表示を消すため、入力があったことを知らせる（isTrusted は偽になる）
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  textarea.scrollTop = textarea.scrollHeight;
}

/** 聞くのを止める。`discard` のときは、これから届く文字を欄に入れない。 */
export function stopVoiceInput({ discard = false } = {}) {
  const session = active;
  if (!session) return;
  active = null;
  if (discard) session.closed = true;
  setButton(session.button, session.status, false);
  try {
    session.recognition.stop();
  } catch (err) {
    // すでに止まっているときは何もしない
  }
}
