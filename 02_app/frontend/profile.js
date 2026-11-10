// プロフィールの画面（要望4。追加改修の計画.md 5章）
//
// - 支援者（専門職・一般の支援者）は表示名・職種・連絡先・活動する市区町村・住所を変える
//   （PATCH /api/me。連絡先・住所は追加修正④。本人の /api/me にだけ返る値）。
// - ご本人は氏名・年齢・世帯の人数・連絡先を変える（PATCH /api/survivors/{id}/basic）。
//   居場所・段階はここでは変えない（F1の「居場所を変える」を使う）。
// - 立場・組織・ログインの方法は、ここでは変えられない（変えると見られる記録の範囲が
//   変わる。要件定義書10-3）。見るだけの項目として出す。
// - 組織のオーナーには、参加コードと組織の専門職の一覧を出す（追加修正⑥。organizations.js）。
// - ログインの方法（Google・電話番号）をつなげる（使いやすさの改修の計画.md 1章）。
//   つないだあとは、どちらでログインしても同じ利用者になる。
// - 文字の大きさ（ふつう・大きい）を選べる。この端末に覚える（使いやすさの改修の計画.md 4-2の8）。
// - ただし**ご本人は支援の組織を選び直せる**（つなぐ・変える・外す。追加修正⑦）。
//   変える前に、前の組織の専門職と担当の支援者から見えなくなること・担当が外れること・
//   新しい組織の専門職に記録（健康・介護の情報を含む）が見えることを確かめてもらう。

import {
  LOGIN_METHOD_LABELS,
  apiFetch,
  confirmPhoneLink,
  describeAuthError,
  getConfig,
  hasPhoneConflict,
  linkGoogle,
  linkedMethods,
  readError,
  releaseConflictingPhone,
  resetPhoneLink,
  startPhoneLink,
} from "./auth.js";
import { icon } from "./icons.js";
import { bindMunicipalityPicker, describeMunicipality } from "./municipalities.js";
import { renderOwnerPanel } from "./organizations.js";
import { createOrganizationPicker } from "./orgpicker.js";
import {
  clear,
  confirmDialog,
  el,
  fillSelect,
  node,
  saveTextSize,
  savedTextSize,
  showFormError,
  showNotice,
  showScreen,
  withBusy,
} from "./ui.js";

const ROLE_SURVIVOR = "survivor";
const ROLE_PROFESSIONAL = "professional";

const ROLE_LABELS = {
  survivor: "ご本人",
  professional: "専門職",
  volunteer: "支援者（ボランティア・ご家族）",
  // 施設の職員（使いやすさの改修の計画.md 9章）。支援者と同じ項目を直す
  facility_staff: "施設の職員",
};

let me = null;
// 保存のあと、ホームの表示（お名前など）を更新してもらうための呼び出し先
let onSaved = () => {};
// ご本人が支援の組織を選び直したあと、ホームを作り直してもらうための呼び出し先（追加修正⑦）
let onOrganizationChanged = async () => {};
// 「支援の組織」の欄（ご本人のときだけ作る）
let orgPicker = null;

/** 立場を受け取る（ホームから渡される）。 */
export function setMe(value) {
  me = value;
}

function professions() {
  return getConfig()?.choices?.professions || [];
}

function renderReadOnly() {
  el("profile-role").textContent =
    me.role === ROLE_PROFESSIONAL && me.profession
      ? `${ROLE_LABELS[me.role]}（${me.profession}）`
      : ROLE_LABELS[me.role] || "";
  el("profile-org").textContent = me.org_name || me.org_id || "（未設定）";
  el("profile-login-method").textContent =
    LOGIN_METHOD_LABELS[me.login_method] || me.login_method || "";
}

// --- ログインの方法（使いやすさの改修の計画.md 1章） ---

/** つながっているログインの方法と、まだつないでいない方法のボタンを出す。 */
function renderLoginMethods() {
  const linked = linkedMethods();
  const list = el("profile-login-method-list");
  clear(list);
  for (const method of ["google", "phone"]) {
    const has = linked.includes(method);
    const item = node("li", has ? "method method-linked" : "method");
    item.append(icon(has ? "check" : "plus"));
    item.append(node("span", "method-name", LOGIN_METHOD_LABELS[method]));
    item.append(node("span", "badge " + (has ? "badge-active" : "badge-note"), has ? "つないでいます" : "まだです"));
    list.append(item);
  }
  el("profile-link-google").hidden = linked.includes("google");
  const phoneLinked = linked.includes("phone");
  el("form-link-phone").hidden = phoneLinked;
  el("form-link-phone-code").hidden = true;
  showFormError(el("form-link-phone"), "");
  showFormError(el("form-link-phone-code"), "");
}

async function linkGoogleAccount(event) {
  await withBusy(event.currentTarget, async () => {
    try {
      const result = await linkGoogle();
      if (result === "redirect") return; // ページが切り替わる。戻ったときにつながる
      showNotice("Google をつなぎました。次からは Google でもログインできます。", "ok");
      renderLoginMethods();
    } catch (err) {
      showNotice(describeAuthError(err));
    }
  });
}

async function submitLinkPhone(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    try {
      await startPhoneLink(el("link-phone-number").value);
      form.hidden = true;
      el("form-link-phone-code").hidden = false;
      el("link-phone-code").focus();
    } catch (err) {
      showFormError(form, describeAuthError(err));
    }
  });
}

async function submitLinkPhoneCode(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    try {
      await confirmPhoneLink(el("link-phone-code").value);
      el("link-phone-code").value = "";
      el("link-phone-number").value = "";
      showNotice("電話番号をつなぎました。次からは電話番号でもログインできます。", "ok");
      renderLoginMethods();
    } catch (err) {
      if (hasPhoneConflict()) {
        await offerPhoneRelease(form);
        return;
      }
      showFormError(form, describeAuthError(err));
    }
  });
}

// 電話番号が別の利用者としてあったときの説明（画面の改修の計画.md 5章の案C）
const PHONE_RELEASE_MESSAGE = [
  "この電話番号は、別の利用者としてすでにあります（前にこの電話番号でログインしたことがあると起きます）。",
  "",
  "その利用者で、まだ何も登録していなければ、消して、いまの利用者につなぎ直せます。",
  "登録が済んでいるときは消しません。",
  "",
  "進めると、いったんログアウトします。Google でログインし直してから、プロフィールで電話番号をもう一度つないでください（確認コードがもう一度届きます）。",
].join("\n");

/** 別の利用者になっている電話番号を、消してつなぎ直すかを聞く。 */
async function offerPhoneRelease(form) {
  const ok = await confirmDialog(PHONE_RELEASE_MESSAGE, { ok: "消してつなぎ直す" });
  if (!ok) {
    showFormError(form, describeAuthError({ code: "auth/credential-already-in-use" }));
    return;
  }
  try {
    // この後ログアウトの状態になる。知らせはログインの画面に出る（app.js）
    await releaseConflictingPhone();
  } catch (err) {
    showNotice(describeAuthError(err));
  }
}

// --- 文字の大きさ（使いやすさの改修の計画.md 4-2の8） ---

function renderTextSize() {
  const size = savedTextSize() || (me?.role === ROLE_SURVIVOR ? "large" : "normal");
  el("text-size-normal").checked = size !== "large";
  el("text-size-large").checked = size === "large";
}

// 支援者の「活動する都道府県・市区町村」（追加修正④）。部品は1回だけ結び付ける
let supporterPicker = null;

async function ensureSupporterPicker() {
  if (supporterPicker) return supporterPicker;
  supporterPicker = await bindMunicipalityPicker({
    prefectureSelect: el("profile-supporter-prefecture"),
    prefectureFilterInput: el("profile-supporter-prefecture-filter"),
    citySelect: el("profile-supporter-municipality"),
    filterInput: el("profile-supporter-municipality-filter"),
  });
  return supporterPicker;
}

/** 支援者の欄を、いまの値で埋める。 */
function renderSupporterForm() {
  el("profile-display-name").value = me.display_name || "";
  fillSelect(el("profile-profession"), professions(), { placeholder: "選んでください" });
  el("profile-profession").value = me.profession || "";
  el("profile-profession-field").hidden = me.role !== ROLE_PROFESSIONAL;
  el("profile-supporter-contact").value = me.contact || "";
  el("profile-supporter-address").value = me.address || "";
  const code = me.municipality_code || "";
  ensureSupporterPicker()
    .then((picker) => picker.setCode(code))
    .catch((err) => showNotice(err.message));
}

/** ご本人の欄を、自分の記録を読み直して埋める（氏名・年齢などは /api/me に無いため）。 */
async function renderSurvivorForm() {
  const note = el("profile-survivor-note");
  note.hidden = false;
  note.className = "note";
  note.textContent = "読み込んでいます…";

  const res = await apiFetch(`/api/survivors/${encodeURIComponent(me.survivor_id || "")}`);
  if (!res.ok) {
    note.className = "notice-box notice-ng";
    note.textContent = await readError(res);
    return;
  }
  const body = await res.json();
  const basic = body.survivor || {};
  note.hidden = true;
  el("profile-name").value = basic.full_name || "";
  el("profile-age").value = basic.age ?? "";
  el("profile-household").value = basic.household_size ?? "";
  el("profile-contact").value = basic.contact || "";
  el("profile-address").value = basic.address || "";

  // 住んでいる市区町村は見るだけ（要望8。変えると見られる記録の範囲が変わるため）
  const row = el("profile-municipality-row-label");
  const value = el("profile-municipality");
  if (basic.municipality_code) {
    row.hidden = false;
    value.hidden = false;
    value.textContent = "読み込んでいます…";
    describeMunicipality(basic.municipality_code)
      .then((label) => {
        value.textContent = label || basic.municipality_code;
      })
      .catch(() => {
        value.textContent = basic.municipality_code;
      });
  } else {
    row.hidden = true;
    value.hidden = true;
  }

  await renderOrganizationForm(basic.municipality_code || "");
}

// --- 支援の組織を選び直す（ご本人。追加修正⑦） ---

/** 「支援の組織」の欄を、いまの組織に合わせて作る（受け持つ組織を上に出す）。 */
async function renderOrganizationForm(municipalityCode) {
  el("profile-current-org").textContent = me.org_id
    ? me.org_name || me.org_id
    : "つながっていません";
  showFormError(el("form-profile-organization"), "");
  if (!orgPicker) {
    orgPicker = createOrganizationPicker({
      select: el("profile-survivor-org"),
      filterInput: el("profile-org-filter"),
      note: el("profile-org-note"),
    });
  }
  try {
    await orgPicker.load(municipalityCode);
    orgPicker.setValue(me.org_id || "");
  } catch (err) {
    showFormError(el("form-profile-organization"), err.message);
  }
}

/** 変える前に確かめる文（何が起きるかを具体的に書く）。 */
function organizationConfirmText(nextOrgId, nextName) {
  const leaving = me.org_id
    ? `いまの組織（${me.org_name || me.org_id}）の専門職と担当の支援者からは、あなたの記録が見えなくなり、担当の支援者は外れます。`
    : "";
  if (!nextOrgId) {
    return `支援の組織を外します。${leaving}外したあとの相談は、組織を問わず支援者が見られます。外しますか？`;
  }
  return (
    `支援の組織を「${nextName || "選んだ組織"}」にします。${leaving}` +
    "新しい組織の専門職（保健師・相談員など）に、あなたの記録（健康・介護の情報を含む）が見えるようになります。" +
    "この組織にしますか？"
  );
}

async function submitOrganization(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  if (!orgPicker) return;
  const nextOrgId = orgPicker.value();
  if (nextOrgId === (me.org_id || "")) {
    showFormError(form, "いまと同じ組織です");
    return;
  }
  if (!(await confirmDialog(organizationConfirmText(nextOrgId, orgPicker.label())))) return;

  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/me/organization", {
      method: "POST",
      body: JSON.stringify({ org_id: nextOrgId }),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    me = await res.json();
    await onOrganizationChanged(me);
    showNotice(
      me.org_id
        ? `支援の組織を${me.org_name || "選んだ組織"}にしました。`
        : "支援の組織を外しました。",
      "ok",
    );
  });
}

/** 「プロフィール」のタブ・ボタンを押したとき。
 *
 *  `focusOrganization` を渡すと、ご本人の「支援の組織」の欄まで送る（ホームの
 *  「支援の組織を選ぶ」から開いたとき。追加修正⑦）。 */
export async function openProfile({ focusOrganization = false } = {}) {
  showNotice("");
  if (!me) {
    showNotice("ログインの状態を読み直してください");
    return;
  }
  renderReadOnly();
  renderLoginMethods();
  renderTextSize();
  showFormError(el("form-profile-supporter"), "");
  showFormError(el("form-profile-survivor"), "");

  const isSurvivor = me.role === ROLE_SURVIVOR;
  el("form-profile-supporter").hidden = isSurvivor;
  el("profile-survivor-fields").hidden = !isSurvivor;
  // 組織のオーナーだけ、参加コードと専門職の一覧を出す（追加修正⑥）。待たずに読む
  renderOwnerPanel().catch((err) => showNotice(err.message));

  if (isSurvivor) {
    showScreen("screen-profile");
    await renderSurvivorForm();
    if (focusOrganization) {
      el("form-profile-organization").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } else {
    renderSupporterForm();
    showScreen("screen-profile");
  }
}

async function submitSupporter(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const body = {
    display_name: el("profile-display-name").value,
    profession: el("profile-profession").value || null,
    contact: el("profile-supporter-contact").value,
    municipality_code: el("profile-supporter-municipality").value,
    address: el("profile-supporter-address").value,
  };
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/me", { method: "PATCH", body: JSON.stringify(body) });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    me = await res.json();
    renderReadOnly();
    renderSupporterForm();
    showNotice("保存しました。", "ok");
    onSaved(me);
  });
}

async function submitSurvivor(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const body = {
    full_name: el("profile-name").value,
    // 空なら null（0歳を null にしないよう、`|| null` は使わない）
    age: el("profile-age").value.trim() === "" ? null : Number(el("profile-age").value),
    household_size:
      el("profile-household").value.trim() === "" ? null : Number(el("profile-household").value),
    contact: el("profile-contact").value,
    address: el("profile-address").value,
  };
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(me.survivor_id)}/basic`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    const saved = await res.json();
    const basic = saved.survivor || {};
    // 表示名は氏名と同じ扱いなので、ホームの表示も合わせる（要望4）
    me = { ...me, display_name: basic.full_name || me.display_name };
    showNotice("保存しました。前の内容も消えずに残ります。", "ok");
    onSaved(me);
  });
}

// --- イベントの登録 ---

export function bind({ onSaved: savedHandler, onOrganizationChanged: organizationHandler }) {
  onSaved = savedHandler || (() => {});
  onOrganizationChanged = organizationHandler || (async () => {});
  el("form-profile-supporter").addEventListener("submit", submitSupporter);
  el("form-profile-survivor").addEventListener("submit", submitSurvivor);
  el("form-profile-organization").addEventListener("submit", submitOrganization);
  el("profile-link-google").addEventListener("click", linkGoogleAccount);
  el("form-link-phone").addEventListener("submit", submitLinkPhone);
  el("form-link-phone-code").addEventListener("submit", submitLinkPhoneCode);
  el("link-phone-restart").addEventListener("click", () => {
    resetPhoneLink();
    el("form-link-phone-code").hidden = true;
    el("form-link-phone").hidden = false;
    el("link-phone-code").value = "";
  });
  for (const id of ["text-size-normal", "text-size-large"]) {
    el(id).addEventListener("change", (event) => {
      if (event.currentTarget.checked) saveTextSize(event.currentTarget.value);
    });
  }
}
