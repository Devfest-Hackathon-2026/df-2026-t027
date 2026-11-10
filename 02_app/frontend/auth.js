// ログイン（F0。開発手順書 Step 4-1）
//
// - Firebase の JavaScript SDK 13.0.0 を ES Modules で読む（練習と同じ版）。
// - Google ログイン：ローカルは signInWithPopup、本番は signInWithRedirect。
//   どちらを使うかは /api/public-config の login_mode で決める（要件定義書10-2の5）。
// - 電話番号ログイン：見えない reCAPTCHA ＋ signInWithPhoneNumber。
// - apiFetch：IDトークンを Authorization: Bearer に付ける。以降のAPIはすべてこれで呼ぶ。
// - トークン・電話番号・参加コードは、画面にもコンソールにも残さない。
// - ログインの方法をつなぐ（使いやすさの改修の計画.md 1章）：Google と電話番号は、同じ人でも
//   別の利用者になる。プロフィールから、もう一方の方法を今の利用者に足せるようにする
//   （linkWithPopup・linkWithRedirect・linkWithPhoneNumber）。
// - 足したい電話番号が、別の利用者としてもう Firebase にあるとき（前にその電話番号でログインした）は、
//   その利用者に登録が無ければ、画面の中で消してつなぎ直せるようにする（画面の改修の計画.md 5章の案C）。
//   サーバーに Firebase の利用者を消す権限は持たせない。消すのは、SMS で本人と確かめた利用者だけ。
// - 前回のログインの方法（google・phone の名前だけ）をこの端末に覚える。電話番号は覚えない。

import { initializeApp } from "https://www.gstatic.com/firebasejs/13.0.0/firebase-app.js";
import {
  GoogleAuthProvider,
  PhoneAuthProvider,
  RecaptchaVerifier,
  browserLocalPersistence,
  deleteUser,
  getAuth,
  getRedirectResult,
  linkWithPhoneNumber,
  linkWithPopup,
  linkWithRedirect,
  onAuthStateChanged,
  setPersistence,
  signInWithCredential,
  signInWithPhoneNumber,
  signInWithPopup,
  signInWithRedirect,
  signOut,
} from "https://www.gstatic.com/firebasejs/13.0.0/firebase-auth.js";

let config = null;
let auth = null;
// reCAPTCHA は入れ物ごとに1つ（ログインの画面と、プロフィールの「電話番号をつなぐ」）
const recaptchaVerifiers = new Map();
let phoneConfirmation = null;
let linkConfirmation = null;
// つなごうとした電話番号が別の利用者のものだったときの、その電話番号の資格（5章。画面の中だけに持つ）
let conflictCredential = null;
// 利用者を入れ替えている間は、ログインの状態の知らせを画面に流さない（登録の画面が一瞬出るのを防ぐ）
let switchingUser = false;

const LOGIN_CONTAINER = "recaptcha-container";
const LINK_CONTAINER = "recaptcha-link-container";

/** /api/public-config を読む（ログイン前に呼べる唯一のAPI）。 */
export async function loadConfig() {
  if (config) return config;
  const res = await fetch("/api/public-config");
  if (!res.ok) throw new Error("アプリの設定を読み込めませんでした");
  config = await res.json();
  return config;
}

export function getConfig() {
  return config;
}

/** Firebase を用意し、ログインの状態が変わるたびに呼び出し先へ知らせる。 */
export async function startAuth({ onUser, onSignedOut, onError }) {
  const loaded = await loadConfig();
  const app = initializeApp(loaded.firebase);
  auth = getAuth(app);
  // 再読み込みしてもログインを保つ
  await setPersistence(auth, browserLocalPersistence);

  if (loaded.login_mode === "redirect") {
    // リダイレクトから戻ってきたときに、ここでログインが終わる
    try {
      await getRedirectResult(auth);
    } catch (err) {
      onError(describeAuthError(err));
    }
  }

  onAuthStateChanged(auth, (user) => {
    if (switchingUser) return;
    if (user) {
      onUser(user);
    } else {
      onSignedOut();
    }
  });
}

/** Google でログインする。 */
export async function loginWithGoogle() {
  const provider = new GoogleAuthProvider();
  if (getConfig()?.login_mode === "redirect") {
    await signInWithRedirect(auth, provider);
    return; // この後ページが切り替わる
  }
  await signInWithPopup(auth, provider);
}

/** 入力された電話番号を +81 の形にそろえる。 */
export function normalizePhoneNumber(input) {
  const digits = (input || "")
    .replace(/[０-９＋－]/g, (ch) =>
      ch === "＋" ? "+" : ch === "－" ? "-" : String.fromCharCode(ch.charCodeAt(0) - 0xfee0),
    )
    .replace(/[\s()\-]/g, "");
  if (!digits) return "";
  if (digits.startsWith("+")) return digits;
  if (digits.startsWith("0")) return `+81${digits.slice(1)}`;
  return `+${digits}`;
}

function ensureRecaptcha(containerId = LOGIN_CONTAINER) {
  const existing = recaptchaVerifiers.get(containerId);
  if (existing) return existing;
  // **毎回まっさらな入れ物を作る。** 同じ要素に2回入れると
  // 「reCAPTCHA has already been rendered in this element」になる
  // （`clear()` を呼んでも要素側に前の跡が残るため。ログアウトして
  // 別の電話番号でログインし直したときに実際に起きた）。
  const host = document.getElementById(containerId);
  host.replaceChildren();
  const slot = document.createElement("div");
  host.append(slot);
  // 見えない形。利用者には何も出ない（押したときに裏で確認される）。
  const verifier = new RecaptchaVerifier(auth, slot, { size: "invisible" });
  recaptchaVerifiers.set(containerId, verifier);
  return verifier;
}

function clearRecaptcha(containerId) {
  const verifier = recaptchaVerifiers.get(containerId);
  if (verifier) {
    try {
      verifier.clear();
    } catch (err) {
      // すでに外れている。次に作り直せればよいので、ここでは止めない
    }
    recaptchaVerifiers.delete(containerId);
  }
  // 入れ物の中身も消す。残っていると次の RecaptchaVerifier が作れない
  document.getElementById(containerId)?.replaceChildren();
}

function checkedPhoneNumber(phoneNumber) {
  const normalized = normalizePhoneNumber(phoneNumber);
  if (!/^\+\d{8,15}$/.test(normalized)) {
    throw new Error("電話番号の形を確かめてください（例：09012345678）");
  }
  return normalized;
}

function checkedCode(code) {
  const value = (code || "").replace(/\s/g, "");
  if (!/^\d{6}$/.test(value)) throw new Error("確認コードは6桁の数字です");
  return value;
}

/** 電話番号に確認コードを送る。 */
export async function startPhoneLogin(phoneNumber) {
  const normalized = checkedPhoneNumber(phoneNumber);
  try {
    phoneConfirmation = await signInWithPhoneNumber(auth, normalized, ensureRecaptcha());
  } catch (err) {
    // 失敗した reCAPTCHA は使い回せない。次の操作で作り直せるように捨てる
    resetPhoneLogin();
    throw err;
  }
}

/** 届いた確認コードでログインする。 */
export async function confirmPhoneCode(code) {
  if (!phoneConfirmation) throw new Error("先に電話番号を入れて、確認コードを受け取ってください");
  const value = checkedCode(code);
  await phoneConfirmation.confirm(value);
  phoneConfirmation = null;
}

/** 電話番号ログインをやり直せるようにする（reCAPTCHA を作り直す）。 */
export function resetPhoneLogin() {
  phoneConfirmation = null;
  clearRecaptcha(LOGIN_CONTAINER);
}

/** ログアウトする。 */
export async function signOutUser() {
  resetPhoneLogin();
  resetPhoneLink();
  await signOut(auth);
}

// --- ログインの方法をつなぐ（使いやすさの改修の計画.md 1章） ---

const METHOD_BY_PROVIDER = { "google.com": "google", phone: "phone" };

/** 今の利用者につながっているログインの方法（"google"・"phone"）。 */
export function linkedMethods() {
  const user = auth?.currentUser;
  if (!user) return [];
  return user.providerData
    .map((info) => METHOD_BY_PROVIDER[info.providerId] || "")
    .filter(Boolean);
}

/** 今の利用者に、Google のログインを足す。本番はリダイレクトで、戻ってきたときにつながる。 */
export async function linkGoogle() {
  const user = auth?.currentUser;
  if (!user) throw new Error("ログインが切れました。もう一度ログインしてください");
  const provider = new GoogleAuthProvider();
  if (getConfig()?.login_mode === "redirect") {
    await linkWithRedirect(user, provider);
    return "redirect"; // この後ページが切り替わる
  }
  await linkWithPopup(user, provider);
  return "linked";
}

/** 今の利用者に足す電話番号へ、確認コードを送る。 */
export async function startPhoneLink(phoneNumber) {
  const user = auth?.currentUser;
  if (!user) throw new Error("ログインが切れました。もう一度ログインしてください");
  const normalized = checkedPhoneNumber(phoneNumber);
  try {
    linkConfirmation = await linkWithPhoneNumber(user, normalized, ensureRecaptcha(LINK_CONTAINER));
  } catch (err) {
    resetPhoneLink();
    throw err;
  }
}

/** 届いた確認コードで、電話番号を今の利用者につなぐ。 */
export async function confirmPhoneLink(code) {
  if (!linkConfirmation) throw new Error("先に電話番号を入れて、確認コードを受け取ってください");
  const value = checkedCode(code);
  conflictCredential = null;
  try {
    await linkConfirmation.confirm(value);
  } catch (err) {
    // 別の利用者の電話番号だった。確認コードで確かめた資格を、消してつなぎ直すために持っておく
    if (err?.code === "auth/credential-already-in-use") {
      conflictCredential = PhoneAuthProvider.credentialFromError(err);
    }
    throw err;
  }
  resetPhoneLink();
}

/** つなごうとした電話番号が、別の利用者としてあったか（消してつなぎ直せるか）。 */
export function hasPhoneConflict() {
  return Boolean(conflictCredential);
}

/**
 * 別の利用者になっている電話番号を、登録が無ければ消す（画面の改修の計画.md 5章の案C）。
 *
 * 1. 確認コードで確かめた資格で、その電話番号の利用者として入る
 * 2. `/api/me` で登録があるかを見る。登録があれば消さずにログアウトする（"registered"）
 * 3. 登録が無ければ、その利用者を消す（"deleted"）。消すとログアウトの状態になる
 *
 * どちらのときも、元の利用者（Google）からはログアウトした状態で終わる。Google で入り直して、
 * 電話番号をもう一度つなぐ（確認コードは1回しか使えないので、もう一度送る）。
 * 何をしたかは、ログアウトの画面で知らせるために `phoneMergeState` に名前だけを残す（電話番号は残さない）。
 */
export async function releaseConflictingPhone() {
  const credential = conflictCredential;
  conflictCredential = null;
  if (!credential) throw new Error("もう一度、電話番号に確認コードを送ってください");
  resetPhoneLink();
  switchingUser = true;
  try {
    const result = await signInWithCredential(auth, credential);
    const res = await apiFetch("/api/me");
    if (!res.ok) throw new Error(await readError(res));
    const me = await res.json();
    if (me.registered) {
      setPhoneMergeState(MERGE_REGISTERED);
      switchingUser = false;
      await signOut(auth);
      return MERGE_REGISTERED;
    }
    setPhoneMergeState(MERGE_RELINK);
    switchingUser = false;
    await deleteUser(result.user);
    return MERGE_RELINK;
  } catch (err) {
    // 途中で止まったら、どの利用者で入っているか分からなくなるので、ログアウトして入り直してもらう
    setPhoneMergeState("");
    switchingUser = false;
    await signOut(auth).catch(() => {});
    throw err;
  }
}

// 消してつなぎ直す途中の段階（この端末のこのタブだけ。名前だけを覚え、電話番号は覚えない）
const MERGE_KEY = "tsunagi.phoneMerge";
export const MERGE_RELINK = "relink";
export const MERGE_REGISTERED = "registered";

function setPhoneMergeState(value) {
  try {
    if (value) window.sessionStorage.setItem(MERGE_KEY, value);
    else window.sessionStorage.removeItem(MERGE_KEY);
  } catch (err) {
    // 覚えられない環境でも、消す・つなぐ処理そのものは続ける
  }
}

/** 消してつなぎ直す途中の段階を読み、消す（1回だけ知らせるため）。 */
export function takePhoneMergeState() {
  try {
    const value = window.sessionStorage.getItem(MERGE_KEY) || "";
    window.sessionStorage.removeItem(MERGE_KEY);
    return value === MERGE_RELINK || value === MERGE_REGISTERED ? value : "";
  } catch (err) {
    return "";
  }
}

/** 段階を読むだけ（消さない）。 */
export function peekPhoneMergeState() {
  try {
    return window.sessionStorage.getItem(MERGE_KEY) || "";
  } catch (err) {
    return "";
  }
}

/** 電話番号をつなぐ操作をやり直せるようにする。 */
export function resetPhoneLink() {
  linkConfirmation = null;
  clearRecaptcha(LINK_CONTAINER);
}

// --- 前回のログインの方法（この端末にだけ覚える。電話番号は覚えない） ---

const LAST_LOGIN_KEY = "tsunagi.lastLoginMethod";
const KNOWN_METHODS = new Set(["google", "phone"]);

export function rememberLoginMethod(method) {
  if (!KNOWN_METHODS.has(method)) return;
  try {
    window.localStorage.setItem(LAST_LOGIN_KEY, method);
  } catch (err) {
    // 覚えられない環境でも、ログインそのものは続ける
  }
}

export function lastLoginMethod() {
  try {
    const value = window.localStorage.getItem(LAST_LOGIN_KEY) || "";
    return KNOWN_METHODS.has(value) ? value : "";
  } catch (err) {
    return "";
  }
}

export const LOGIN_METHOD_LABELS = { google: "Google", phone: "電話番号" };

/** IDトークンを付けて API を呼ぶ。401 のときは一度だけトークンを取り直してやり直す。 */
export async function apiFetch(path, options = {}) {
  const user = auth?.currentUser;
  if (!user) throw new Error("ログインが切れました。もう一度ログインしてください");

  const send = async (forceRefresh) => {
    const token = await user.getIdToken(forceRefresh);
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);
    if (options.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return fetch(path, { ...options, headers });
  };

  let res = await send(false);
  if (res.status === 401) res = await send(true);
  return res;
}

// そのAPIがサーバーに無いと、要求が最後の「画面の配信」に落ちて 404 か
// 405（Method Not Allowed）が返る。文面だけでは原因が分からないので言い換える
// （実際に、古いサーバーが動いたままで「Method Not Allowed」とだけ出た）
const STALE_SERVER_MESSAGE =
  "このAPIがサーバーにありません。サーバーを立て直してください（古いまま動いている可能性があります）";

/** API の応答から、画面に出す言葉を取り出す。 */
export async function readError(res) {
  if (res.status === 405) return STALE_SERVER_MESSAGE;
  try {
    const body = await res.json();
    if (res.status === 404 && body.detail === "Not Found") return STALE_SERVER_MESSAGE;
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail) && body.detail.length > 0) {
      return "入力を確かめてください";
    }
  } catch (err) {
    // JSON でないときは下の既定の文を使う
  }
  if (res.status === 401) return "ログインが切れました。もう一度ログインしてください";
  if (res.status === 403) return "この操作は、いまの立場では行えません";
  if (res.status >= 500) return "サーバーが応答しませんでした。もう一度お試しください";
  return `うまくいきませんでした（HTTP ${res.status}）`;
}

/** Firebase のエラーを、画面に出す言葉に直す。 */
export function describeAuthError(err) {
  const code = err?.code || "";
  const messages = {
    "auth/popup-closed-by-user": "ログインの画面が閉じられました。もう一度お試しください",
    "auth/popup-blocked": "ログインの画面が止められました。ブラウザの設定を確かめてください",
    "auth/cancelled-popup-request": "ログインをやり直してください",
    "auth/network-request-failed": "通信できませんでした。電波を確かめてください",
    "auth/too-many-requests": "試した回数が多すぎます。少し待ってからお試しください",
    "auth/invalid-phone-number": "電話番号の形を確かめてください",
    "auth/invalid-verification-code": "確認コードが違います。届いた6桁を入れてください",
    "auth/code-expired": "確認コードの期限が切れました。もう一度送ってください",
    "auth/unauthorized-domain": "このドメインはログインに使えません（承認済みドメインの設定）",
    // ログインの方法をつなぐとき（使いやすさの改修の計画.md 1章）
    "auth/credential-already-in-use":
      "この Google アカウント（または電話番号）は、別の利用者としてすでにあります（前にその方法でログインしたことがあります）",
    "auth/provider-already-linked": "この方法は、もうつながっています",
    "auth/account-exists-with-different-credential":
      "このメールアドレスは、別のログインの方法ですでに使われています",
    "auth/requires-recent-login": "安全のため、一度ログアウトしてログインし直してから、もう一度お試しください",
  };
  return messages[code] || err?.message || "ログインできませんでした";
}
