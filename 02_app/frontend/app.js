// 画面の切り替えと初回の登録（F0。開発手順書 Step 4-4・4-5）
//
// - 未ログインではログイン画面だけを出す（要件定義書F0）。
// - 立場（本人・専門職・一般の支援者）ごとにホームを切り替える。
//   立場は /api/me が返すものだけを使い、画面で持っている値で判断しない（要件定義書10-3）。
// - F1（被災者の記録と担当）の画面は f1.js。画面の共通の部品は ui.js。

import {
  LOGIN_METHOD_LABELS,
  MERGE_REGISTERED,
  MERGE_RELINK,
  apiFetch,
  confirmPhoneCode,
  describeAuthError,
  getConfig,
  lastLoginMethod,
  loginWithGoogle,
  peekPhoneMergeState,
  readError,
  rememberLoginMethod,
  resetPhoneLogin,
  signOutUser,
  startAuth,
  startPhoneLogin,
  takePhoneMergeState,
} from "./auth.js";
import * as f1 from "./f1.js";
import * as f2 from "./f2.js";
import * as f4 from "./f4.js";
import * as f5 from "./f5.js";
import * as f6 from "./f6.js";
import * as f7 from "./f7.js";
import * as f8 from "./f8.js";
// 周りの地域の状況（使いやすさの改修の計画.md 8章）
import * as disaster from "./disaster.js";
import * as facilityStaff from "./facility_staff.js";
import * as family from "./family.js";
import * as handovers from "./handovers.js";
import { applyIcons, icon } from "./icons.js";
import { bindMunicipalityPicker } from "./municipalities.js";
import * as organizations from "./organizations.js";
import { createOrganizationPicker } from "./orgpicker.js";
import * as profile from "./profile.js";
import * as programs from "./programs.js";
import { installVoiceInput } from "./voice.js";
import {
  applyTextSize,
  clear,
  confirmDialog,
  el,
  fillSelect,
  installFormValidation,
  node,
  savedTextSize,
  setHistoryHandlers,
  showFormError,
  showNotice,
  showScreen,
  validateForm,
  withBusy,
} from "./ui.js";

const ROLE_LABELS = {
  survivor: "ご本人",
  professional: "専門職",
  volunteer: "支援者（ボランティア・ご家族）",
  // 施設の職員（使いやすさの改修の計画.md 9章）
  facility_staff: "施設の職員",
};

// 組織がない一般の支援者のホーム・タブの鍵（要望1）。`me.role` には入らない値にする
// （stg_users の role の値と混ざらないように）。
const NO_ORG_KEY = "volunteer_no_org";
// 住んでいる市区町村にまだ組織がない本人（要望8・11章の要確認E-1）。
const SURVIVOR_NO_ORG_KEY = "survivor_no_org";

/** ホーム・タブの表を引く鍵。組織がない人だけ、立場とは別の鍵を使う。 */
function homeKey(me) {
  if (!me.org_id && me.role === "volunteer") return NO_ORG_KEY;
  if (!me.org_id && me.role === "survivor") return SURVIVOR_NO_ORG_KEY;
  return me.role;
}

// ホームの見出しと説明。「これからできること」の文の一覧は、タイルと同じことを書いていて
// 長いのでやめた（使いやすさの改修の計画.md 4-2の5）。できることはタイルで見せる。
const HOME_GUIDE = {
  [NO_ORG_KEY]: {
    title: "支援者のホーム",
    subtitle: "まだ組織に入っていません。参加コードで入るか、組織を作れます。",
  },
  // 組織につながっていない本人（追加修正②）。組織がなくても本人向けの機能は使える
  [SURVIVOR_NO_ORG_KEY]: {
    title: "ご本人のホーム",
    subtitle:
      "ご自分の記録と居場所、健康の情報、相談、受けられる支援を使えます。" +
      "支援の組織につなぐと、その組織の相談員に相談が届きます（つながなくても使えます）。",
  },
  survivor: {
    title: "ご本人のホーム",
    subtitle: "ご自分の記録と居場所、担当の支援者を確かめられます。",
  },
  professional: {
    title: "専門職のホーム",
    subtitle: "担当する地域の被災者の記録を登録し、担当の支援者を割り当てられます。",
  },
  volunteer: {
    title: "支援者のホーム",
    subtitle: "担当になった方の記録だけを見られます。",
  },
  // 施設の職員（使いやすさの改修の計画.md 9章）。自分の施設だけを直す
  facility_staff: {
    title: "施設の職員のホーム",
    subtitle: "自分の施設の空き・断水・停電・対応できる介護と医療を直せます。",
  },
};

// ご本人のホームのボタン（組織があってもなくても同じ。追加修正②）
// 色で目立たせるタイル（primary）は置かない。理由が画面に無く、1つだけ色が違う訳が
// 本人に伝わらなかった（画面の改修の計画.md 3章）。対応が要るものは右上の数で知らせる
const SURVIVOR_ACTIONS = [
  {
    label: "ご自分の記録を見る",
    icon: "file",
    run: (me) => {
      if (!me.survivor_id) {
        showNotice("ご自分の記録が見つかりません。ログアウトしてお試しください");
        return Promise.resolve();
      }
      return f1.openDetail(me.survivor_id);
    },
  },
  {
    label: "相談する",
    icon: "chat",
    run: (me) => {
      if (!me.survivor_id) {
        showNotice("ご自分の記録が見つかりません。ログアウトしてお試しください");
        return Promise.resolve();
      }
      f8.openConsultationNew();
      return Promise.resolve();
    },
  },
  {
    // 支援の画面（あなたの支援・支援制度の一覧。使いやすさの改修の計画.md 6-2）。
    // AIで調べる画面（F6）へは、この画面の「自分に合うものを調べる」から進む。
    // 期限が3日以内の未申請の数を、タイルの右上に出す（11-2）
    label: "受けられる支援を見る",
    icon: "gift",
    countKey: programs.DUE_COUNT_KEY,
    run: () => programs.openPrograms(),
  },
  {
    // 記録の詳細を開かずに、引き継ぎシートの一覧を開く（追加修正⑪）
    label: "引き継ぎシート",
    icon: "clipboard",
    run: (me) => handovers.openHandoverList(me.survivor_id),
  },
  // 家族を招く・外す（使いやすさの改修の計画.md 10章。family.js）
  { label: "家族とつなぐ", icon: "users", run: () => family.openMembers() },
];

// 一般の支援者（ご家族）のタイル（使いやすさの改修の計画.md 10章。組織があってもなくても同じ）
const FAMILY_ACTIONS = [
  { label: "家族などを代わりに登録する", icon: "plus", run: () => family.openProxyNew() },
  { label: "家族の記録", icon: "heart", run: () => family.openList() },
];

// 立場ごとのホームのボタン（F1の画面への入口）。
const HOME_ACTIONS = {
  [NO_ORG_KEY]: [
    {
      label: "参加コードで組織に入る",
      icon: "key",
      primary: true,
      run: () => organizations.openJoinOrganization(),
    },
    { label: "組織を作る", icon: "building", run: () => organizations.openCreateOrganization() },
    ...FAMILY_ACTIONS,
  ],
  // 組織につながっていない本人も、本人向けの機能はそのまま使える（追加修正②）。
  // 支援の組織はプロフィールで選ぶ（追加修正⑦）
  [SURVIVOR_NO_ORG_KEY]: [
    ...SURVIVOR_ACTIONS,
    {
      label: "支援の組織を選ぶ",
      icon: "building",
      run: () => profile.openProfile({ focusOrganization: true }),
    },
  ],
  professional: [
    {
      label: "被災者の一覧を見る",
      icon: "list",
      primary: true,
      countKey: "attention",
      run: () => f1.openList(),
    },
    {
      label: "ご本人からの相談",
      icon: "chat",
      countKey: "consultations",
      run: () => {
        openConsultationsTab();
        return Promise.resolve();
      },
    },
    {
      label: "期限が近い未申請",
      icon: "calendar",
      countKey: "support-alerts",
      run: () => {
        showNotice("");
        showScreen("screen-home");
        el("home-support-alerts-card").scrollIntoView({ behavior: "smooth", block: "start" });
        return Promise.resolve();
      },
    },
    { label: "受け入れ先の施設", icon: "building", run: () => f5.openFacilities() },
    { label: "組織全体の状況", icon: "chart", run: () => f7.openDashboard() },
    // 周りの地域の状況（使いやすさの改修の計画.md 8章）
    { label: "周りの地域の状況を入れる", icon: "map-pin", run: () => disaster.openEditor() },
  ],
  volunteer: [
    { label: "担当している方の一覧", icon: "list", primary: true, run: () => f1.openList() },
    ...FAMILY_ACTIONS,
  ],
  survivor: [...SURVIVOR_ACTIONS],
  // 施設の職員（使いやすさの改修の計画.md 9章）。入口は「自分の施設」だけ
  facility_staff: [
    {
      label: "自分の施設の空きを直す",
      icon: "building",
      primary: true,
      run: () => facilityStaff.openMyFacility(),
    },
  ],
};

/** ご自分の記録を読んでから F6 の画面（AIで調べる）を出す。読みは1回。
 *  記録済みの支援と世帯の人数を、その応答から初期値にする。 */
async function openSupportFinder() {
  if (!currentMe?.survivor_id) {
    showNotice("ご自分の記録が見つかりません。ログアウトしてお試しください");
    return;
  }
  await f1.openDetail(currentMe.survivor_id);
  f6.openSupports();
}

// 今ログインしている人の情報（ホームの後から出すタブや「ご本人からの相談」の
// 再読み込みに使う。立場・組織などの正本は毎回 /api/me から来る値で、ここは
// 最後に renderHome した値を覚えておくだけ）。
let currentMe = null;

/** 「相談」タブ（専門職）。専用の画面は作らず、ホームの「ご本人からの相談」の
 *  カードを出して読み直す（組織全体の相談の一覧は、今のところこのカードだけ）。 */
function openConsultationsTab() {
  showNotice("");
  showScreen("screen-home");
  f8.loadConsultations().catch((err) => showNotice(err.message));
  el("home-consultations-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

// 画面の下に出す、いつでも押せるタブ（要望3）。ホームの入口のボタンと中身は重なるが、
// タブは「今どこにいても次の画面へ1回で行ける」ためのもので、ホームのボタンを置き換えない。
const TAB_DEFS = {
  [NO_ORG_KEY]: [
    { key: "home", label: "ホーム", icon: "home", run: () => { showNotice(""); showScreen("screen-home"); } },
    { key: "profile", label: "プロフィール", icon: "user", run: () => profile.openProfile() },
  ],
  // 組織につながっていない本人も、本人と同じタブを使う（追加修正②）。下の survivor と同じ
  [SURVIVOR_NO_ORG_KEY]: [],
  professional: [
    { key: "home", label: "ホーム", icon: "home", run: () => { showNotice(""); showScreen("screen-home"); } },
    { key: "list", label: "被災者の一覧", icon: "list", run: () => f1.openList() },
    { key: "consultations", label: "相談", icon: "chat", run: () => openConsultationsTab() },
    { key: "profile", label: "プロフィール", icon: "user", run: () => profile.openProfile() },
  ],
  volunteer: [
    { key: "home", label: "ホーム", icon: "home", run: () => { showNotice(""); showScreen("screen-home"); } },
    { key: "list", label: "担当の一覧", icon: "list", run: () => f1.openList() },
    { key: "profile", label: "プロフィール", icon: "user", run: () => profile.openProfile() },
  ],
  survivor: [
    { key: "home", label: "ホーム", icon: "home", run: () => { showNotice(""); showScreen("screen-home"); } },
    {
      key: "self",
      label: "自分の記録",
      icon: "file",
      run: () => {
        if (!currentMe?.survivor_id) {
          showNotice("ご自分の記録が見つかりません。ログアウトしてお試しください");
          return Promise.resolve();
        }
        return f1.openDetail(currentMe.survivor_id);
      },
    },
    {
      key: "consult",
      label: "相談する",
      icon: "chat",
      run: () => {
        f8.openConsultationNew();
        return Promise.resolve();
      },
    },
    // 支援の画面（使いやすさの改修の計画.md 6-2）
    { key: "supports", label: "支援", icon: "gift", run: () => programs.openPrograms() },
    { key: "profile", label: "プロフィール", icon: "user", run: () => profile.openProfile() },
  ],
  // 施設の職員（使いやすさの改修の計画.md 9章）
  facility_staff: [
    { key: "home", label: "ホーム", icon: "home", run: () => { showNotice(""); showScreen("screen-home"); } },
    { key: "facility", label: "自分の施設", icon: "building", run: () => facilityStaff.openMyFacility() },
    { key: "profile", label: "プロフィール", icon: "user", run: () => profile.openProfile() },
  ],
};
TAB_DEFS[SURVIVOR_NO_ORG_KEY] = TAB_DEFS.survivor;

/** タブのうち、今いる画面に合う1つだけを目立たせる。 */
function setActiveTab(key) {
  for (const button of document.querySelectorAll("#tabbar .tab")) {
    button.classList.toggle("active", button.dataset.tabKey === key);
  }
}

/** 立場に応じたタブを作る（ホームを出すたびに作り直す）。 */
function renderTabbar(me) {
  const bar = el("tabbar");
  clear(bar);
  for (const tab of TAB_DEFS[homeKey(me)] || []) {
    const button = node("button", "tab");
    button.type = "button";
    button.dataset.tabKey = tab.key;
    if (tab.icon) button.append(icon(tab.icon));
    button.append(node("span", "tab-label", tab.label));
    button.addEventListener("click", () =>
      withBusy(button, async () => {
        setActiveTab(tab.key);
        try {
          await tab.run();
        } catch (err) {
          showNotice(err.message);
        }
      }),
    );
    bar.append(button);
  }
  bar.hidden = bar.childElementCount === 0;
  setActiveTab("home");
}

// --- 立場ごとのホーム ---

function renderHome(me) {
  const guide = HOME_GUIDE[homeKey(me)];
  if (!guide) {
    showNotice("立場を読み取れませんでした。ログアウトしてもう一度お試しください");
    return;
  }
  currentMe = me;
  f1.setMe(me);
  f2.setMe(me);
  f4.setMe(me);
  f5.setMe(me);
  f6.setMe(me);
  f8.setMe(me);
  facilityStaff.setMe(me);
  handovers.setMe(me);
  profile.setMe(me);
  programs.setMe(me);
  organizations.setMe(me);
  disaster.setMe(me);
  renderTabbar(me);
  // 右上のプロフィール（追加修正⑧）。下のタブにも残す（どちらからでも開ける）
  el("appbar-profile").hidden = false;
  el("home-title").textContent = guide.title;
  el("home-subtitle").textContent = guide.subtitle;
  el("home-name").textContent = me.display_name || "（未設定）";
  el("home-role").textContent =
    me.role === "professional" && me.profession
      ? `${ROLE_LABELS[me.role]}（${me.profession}）`
      : ROLE_LABELS[me.role];
  el("home-org").textContent = me.org_name || (me.org_id ? me.org_id : "組織につながっていません");
  // 連絡先・活動する市区町村が空の支援者に、プロフィールで入れるよう案内する（追加修正④）。
  // 使うのは止めない（前から登録している人が、今日の活動を続けられるように）
  el("home-profile-notice").hidden = !me.profile_incomplete;

  // 本人は、文字の大きさを選んでいなければ「大きい」で始める（使いやすさの改修の計画.md 4-2の8）
  applyTextSize(savedTextSize() || (me.role === "survivor" ? "large" : "normal"));

  const actions = el("home-actions");
  clear(actions);
  for (const action of HOME_ACTIONS[homeKey(me)] || []) {
    // タイル：アイコンと短い言葉。対応が要る数は右上に出す（使いやすさの改修の計画.md 4-2の5）
    const button = node("button", action.primary ? "tile tile-primary" : "tile");
    button.type = "button";
    const mark = node("span", "tile-icon");
    mark.append(icon(action.icon || "info"));
    button.append(mark, node("span", "tile-label", action.label));
    if (action.countKey) {
      const badge = node("span", "tile-count");
      badge.dataset.countKey = action.countKey;
      badge.hidden = true;
      button.append(badge);
    }
    button.addEventListener("click", () =>
      withBusy(button, async () => {
        try {
          await action.run(me);
        } catch (err) {
          showNotice(err.message);
        }
      }),
    );
    actions.append(button);
  }

  showScreen("screen-home");

  // 「対応が必要な人」（F4）と「ご本人からの相談」（F8）は、ホームを出したあとに読む。
  // **待たない**（BigQuery の読みに1〜2秒かかるため、ホームの表示を遅らせない）。
  // 2つを同時に走らせる（順に待つと専門職のホームが2秒遅れて埋まる）
  f4.loadAttention().catch((err) => showNotice(err.message));
  f8.loadConsultations().catch((err) => showNotice(err.message));
  // 組織につながっていない方からの相談（追加修正②。支援者全員。組織がない支援者も）
  f8.loadPublicConsultations().catch((err) => showNotice(err.message));
  // 周りの地域の状況（使いやすさの改修の計画.md 8章。全員）。これも待たない
  disaster.loadHomeStatus().catch((err) => showNotice(err.message));
  // 期限が3日以内の未申請の知らせ（ご本人だけ。使いやすさの改修の計画.md 11-2）。これも待たない
  programs.loadHomeBanner();
  // 施設の職員は、ホームを出したらそのまま自分の施設を開く（使いやすさの改修の計画.md 9章）
  if (facilityStaff.isFacilityStaff()) {
    setActiveTab("facility");
    facilityStaff.openMyFacility().catch((err) => showNotice(err.message));
  }
}

// --- 初回の登録 ---

// 都道府県・市区町村の一覧は municipalities.js が /data/municipalities.json から読む
// （BigQueryもAPIも使わない）。支援の組織は本人が選ぶ（任意。追加修正⑦）。
// 住んでいる市区町村を受け持つ組織を上に出し、全国の組織から名前で探せる（orgpicker.js）。
let municipalityPickerLoaded = false;
let survivorOrgPicker = null;

async function loadMunicipalityPicker() {
  if (municipalityPickerLoaded) return;
  survivorOrgPicker = createOrganizationPicker({
    select: el("survivor-org"),
    filterInput: el("survivor-org-filter"),
    note: el("survivor-org-note"),
  });
  await bindMunicipalityPicker({
    prefectureSelect: el("survivor-prefecture"),
    prefectureFilterInput: el("survivor-prefecture-filter"),
    citySelect: el("survivor-municipality"),
    filterInput: el("survivor-municipality-filter"),
    // 市区町村を選び直したら、受け持つ組織を上に出し直す（選んだ組織はそのまま残す）
    onCityChange: (code) => {
      survivorOrgPicker.setMunicipality(code).catch((err) => showNotice(err.message));
    },
  });
  await survivorOrgPicker.load("");
  municipalityPickerLoaded = true;
}

// 支援者の「活動する都道府県・市区町村」（追加修正④）。本人の登録と同じ部品を使う
let supporterPickerLoaded = false;

async function loadSupporterMunicipalityPicker() {
  if (supporterPickerLoaded) return;
  await bindMunicipalityPicker({
    prefectureSelect: el("supporter-prefecture"),
    prefectureFilterInput: el("supporter-prefecture-filter"),
    citySelect: el("supporter-municipality"),
    filterInput: el("supporter-municipality-filter"),
  });
  supporterPickerLoaded = true;
}

function fillChoices() {
  const choices = getConfig()?.choices;
  if (!choices) return;
  fillSelect(el("survivor-stage"), choices.stages, { placeholder: "選んでください" });
  fillSelect(el("survivor-location-type"), choices.location_types, {
    placeholder: "選んでください",
  });
  // 専門職でない方は「該当なし」を選ぶ（追加修正③）。サーバーは「該当なし」を職種なしとして保存する
  fillSelect(
    el("supporter-profession"),
    [choices.no_profession, ...choices.professions].filter(Boolean),
    { placeholder: "選んでください" },
  );
}

function numberOrNull(value) {
  const text = (value || "").trim();
  if (!text) return null;
  const parsed = Number(text);
  return Number.isInteger(parsed) ? parsed : null;
}

function survivorBody() {
  return {
    municipality_code: el("survivor-municipality").value,
    // 選んだ支援の組織（空なら組織なし。実在するかはサーバーが確かめる。追加修正⑦）
    org_id: survivorOrgPicker ? survivorOrgPicker.value() : "",
    full_name: el("survivor-name").value,
    age: numberOrNull(el("survivor-age").value),
    household_size: numberOrNull(el("survivor-household").value),
    contact: el("survivor-contact").value,
    address: el("survivor-address").value,
    stage: el("survivor-stage").value,
    location_type: el("survivor-location-type").value,
    location_detail: el("survivor-location-detail").value,
  };
}

/** 本人の登録の段を切り替える（使いやすさの改修の計画.md 4-2の3）。 */
function showSurvivorStep(step) {
  el("survivor-step-1").hidden = step !== 1;
  el("survivor-step-2").hidden = step !== 2;
  el("survivor-step-mark-1").className = step === 1 ? "step step-current" : "step step-done";
  el("survivor-step-mark-2").className = step === 2 ? "step step-current" : "step";
  window.scrollTo({ top: 0 });
}

async function submitSurvivor(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const body = survivorBody();
  // 組織を選んだときは、その組織の専門職に記録が見えることを確かめてもらう（追加修正⑦）
  if (
    body.org_id &&
    !(await confirmDialog(
      `「${survivorOrgPicker.label() || "選んだ組織"}」の専門職（保健師・相談員など）に、` +
        "あなたの記録（健康・介護の情報を含む）が見えるようになります。この組織で登録しますか？",
      { ok: "この組織で登録する" },
    ))
  ) {
    return;
  }
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/onboarding/survivor", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (await goHomeIfRegistered(res)) return;
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    showSurvivorHome(await res.json());
  });
}

/** 登録のAPIが「すでに登録が済んでいます」（409）を返したら、誤りにせずホームを出す
 *  （使いやすさの改修の計画.md 1章。別の画面やタブで登録が済んでいたときなど）。 */
async function goHomeIfRegistered(res) {
  if (res.status !== 409) return false;
  await onSignedIn();
  return true;
}

/** 本人の登録のあとにホームを出す（組織がなくても、ふつうに使える前提で案内する。追加修正②）。 */
function showSurvivorHome(me) {
  renderHome(me);
  showNotice(
    me.org_id
      ? `登録しました。${me.org_name || "支援の組織"}の相談員に相談が届きます。`
      : "登録しました。支援の組織につなぐと、その組織の相談員に相談が届きます（つながなくても使えます）。",
    "ok",
  );
}

async function submitLinkCode(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/onboarding/survivor", {
      method: "POST",
      body: JSON.stringify({ link_code: el("link-code").value }),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    // つなぎ込みの応答には組織の名前が入らない（読みを1回増やさないため）。
    // /api/me をもう一度読んでホームを出す
    showNotice("記録とつながりました。", "ok");
    await onSignedIn();
  });
}

async function submitSupporter(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const body = {
    join_code: el("supporter-code").value,
    display_name: el("supporter-name").value,
    profession: el("supporter-profession").value || null,
    // 追加修正④。連絡先・活動する市区町村は必須、住所は任意（サーバーでも確かめる）
    contact: el("supporter-contact").value,
    municipality_code: el("supporter-municipality").value,
    address: el("supporter-address").value,
  };
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/onboarding/supporter", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (await goHomeIfRegistered(res)) return;
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    renderHome(await res.json());
  });
}

// --- ログインの状態に合わせて画面を出す ---

async function onSignedIn() {
  el("sign-out").hidden = false;
  try {
    const res = await apiFetch("/api/me");
    if (!res.ok) {
      showNotice(await readError(res));
      showScreen("screen-login");
      return;
    }
    const me = await res.json();
    // 前回のログインの方法を覚える（名前だけ。使いやすさの改修の計画.md 1章）。
    // 登録の画面に「前は○○でした」と出すため、覚える前の値を先に読む
    const previous = lastLoginMethod();
    rememberLoginMethod(me.login_method);
    // 別の利用者になっていた電話番号を消したあと、入り直したとき（画面の改修の計画.md 5章）
    const relink = takePhoneMergeState() === MERGE_RELINK;
    if (me.registered) {
      renderHome(me);
      if (relink) {
        showNotice(
          "プロフィールの「ログインの方法」から、電話番号をもう一度つないでください。",
          "ok",
        );
      }
      return;
    }
    fillChoices();
    renderChoiceHint(previous, me.login_method);
    showScreen("screen-choice");
  } catch (err) {
    showNotice(err.message);
    showScreen("screen-login");
  }
}

/** 「どちらで使いますか」に、前回と違う方法で入っていないかの手がかりを出す。 */
function renderChoiceHint(previous, current) {
  const hint = el("choice-last-login");
  if (previous && current && previous !== current) {
    hint.textContent =
      `この端末では、前回は${LOGIN_METHOD_LABELS[previous]}でログインしていました。` +
      `いまは${LOGIN_METHOD_LABELS[current] || "別の方法"}でログインしています。`;
    hint.hidden = false;
  } else {
    hint.hidden = true;
  }
}

/** ログインの画面に、前回のログインの方法を出す（使いやすさの改修の計画.md 1章）。 */
function renderLastLogin() {
  const method = lastLoginMethod();
  const note = el("login-last-method");
  note.hidden = !method;
  note.textContent = method ? `前回は${LOGIN_METHOD_LABELS[method]}でログインしました。` : "";
  for (const card of document.querySelectorAll("[data-login-method]")) {
    card.classList.toggle("login-last", card.dataset.loginMethod === method);
  }
}

function onSignedOut() {
  el("sign-out").hidden = true;
  el("appbar-profile").hidden = true;
  currentMe = null;
  f1.setMe(null);
  f2.setMe(null);
  f4.setMe(null);
  f5.setMe(null);
  f6.setMe(null);
  f8.setMe(null);
  facilityStaff.setMe(null);
  handovers.setMe(null);
  profile.setMe(null);
  programs.setMe(null);
  organizations.setMe(null);
  clear(el("tabbar"));
  el("tabbar").hidden = true;
  resetPhoneLogin();
  el("form-phone-code").hidden = true;
  el("form-phone").hidden = false;
  renderLastLogin();
  showScreen("screen-login");
  showPhoneMergeNotice();
}

// 別の利用者になっていた電話番号を、消してつなぎ直す途中でログアウトしたとき（画面の改修の計画.md 5章）
function showPhoneMergeNotice() {
  const state = peekPhoneMergeState();
  if (state === MERGE_RELINK) {
    // 段階は、Google で入り直したときの知らせのために残す（onSignedIn で消す）
    showNotice(
      "電話番号の古い利用者を消しました。Google でログインし直してから、" +
        "プロフィールで電話番号をもう一度つないでください。",
      "ok",
    );
  } else if (state === MERGE_REGISTERED) {
    takePhoneMergeState();
    showNotice(
      "この電話番号では、別の方として登録が済んでいるため、消しませんでした。" +
        "Google でログインし直してください。",
    );
  }
}

function bindEvents() {
  el("notice-close").addEventListener("click", () => showNotice(""));
  // 別の方法で入り直す（使いやすさの改修の計画.md 1章）。登録していないので確かめは要らない
  el("choice-relogin").addEventListener("click", (event) =>
    withBusy(event.currentTarget, async () => {
      try {
        await signOutUser();
      } catch (err) {
        showNotice(describeAuthError(err));
      }
    }),
  );

  el("login-google").addEventListener("click", async (event) => {
    showNotice("");
    await withBusy(event.currentTarget, async () => {
      try {
        await loginWithGoogle();
      } catch (err) {
        showNotice(describeAuthError(err));
      }
    });
  });

  el("form-phone").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    showFormError(form, "");
    await withBusy(form.querySelector("button[type=submit]"), async () => {
      try {
        await startPhoneLogin(el("phone-number").value);
        form.hidden = true;
        el("form-phone-code").hidden = false;
        el("phone-code").focus();
      } catch (err) {
        showFormError(form, describeAuthError(err));
      }
    });
  });

  el("form-phone-code").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    showFormError(form, "");
    await withBusy(form.querySelector("button[type=submit]"), async () => {
      try {
        await confirmPhoneCode(el("phone-code").value);
      } catch (err) {
        showFormError(form, describeAuthError(err));
      }
    });
  });

  el("phone-restart").addEventListener("click", () => {
    resetPhoneLogin();
    el("form-phone-code").hidden = true;
    el("form-phone").hidden = false;
    el("phone-code").value = "";
  });

  el("survivor-step-next").addEventListener("click", () => {
    const form = el("form-survivor");
    if (!validateForm(form, el("survivor-step-1"))) return;
    showFormError(el("survivor-step-1"), "");
    showSurvivorStep(2);
    el("survivor-stage").focus();
  });
  el("survivor-step-back").addEventListener("click", () => showSurvivorStep(1));

  el("choose-survivor").addEventListener("click", async (event) => {
    showNotice("");
    await withBusy(event.currentTarget, async () => {
      try {
        await loadMunicipalityPicker();
        showSurvivorStep(1);
        showScreen("screen-survivor-self");
      } catch (err) {
        showNotice(err.message);
      }
    });
  });

  el("choose-supporter").addEventListener("click", () => {
    showNotice("");
    showScreen("screen-supporter");
    loadSupporterMunicipalityPicker().catch((err) => showNotice(err.message));
  });
  el("home-profile-notice-open").addEventListener("click", () => {
    profile.openProfile().catch((err) => showNotice(err.message));
  });

  for (const button of document.querySelectorAll(".back-to-choice")) {
    button.addEventListener("click", () => {
      showNotice("");
      showScreen("screen-choice");
    });
  }

  el("form-survivor").addEventListener("submit", submitSurvivor);
  el("form-link-code").addEventListener("submit", submitLinkCode);
  el("form-supporter").addEventListener("submit", submitSupporter);

  // 右上のプロフィール（追加修正⑧）。タブの「プロフィール」と同じ動きにする
  el("appbar-profile").addEventListener("click", (event) =>
    withBusy(event.currentTarget, async () => {
      showNotice("");
      setActiveTab("profile");
      try {
        await profile.openProfile();
      } catch (err) {
        showNotice(err.message);
      }
    }),
  );

  el("sign-out").addEventListener("click", async (event) => {
    // 電話番号でログインした人は、次に開くとき確認コードを打ち直すことになる
    // （ログインしたままにする設定を外すため。要望5）。先に伝えてから進める
    const message =
      currentMe?.login_method === "phone"
        ? "ログアウトすると、次にログインするときにもう一度、電話番号の確認コードが必要になります。ログアウトしますか？"
        : "ログアウトしますか？";
    const button = event.currentTarget;
    if (!(await confirmDialog(message, { ok: "ログアウトする" }))) return;
    await withBusy(button, async () => {
      try {
        await signOutUser();
      } catch (err) {
        showNotice(describeAuthError(err));
      }
    });
  });

  // F1 の画面（一覧・登録・詳細・担当）
  f1.bind({
    onHome: () => {
      showNotice("");
      showScreen("screen-home");
      // 「ホームに戻る」はどの画面にも置いてあるので、ここでタブの印も戻す（要望3）
      setActiveTab("home");
    },
    // 詳細の「健康・介護の情報」と「訪問・見守りの記録」は f2.js、
    // 「悪化の兆し」は f4.js が描く
    onDetailRendered: (detail) => {
      f2.renderDetailSections(detail);
      f4.renderDetailRisk(detail);
      f8.renderDetailConsultations(detail);
      // 受け入れ先のカードは f5.js が出し、その中の引き継ぎシートの一覧は handovers.js が描く
      f5.renderDetailPlacement(detail);
      handovers.renderDetailHandovers(detail);
      // 受けられる支援と申請の状況（F6）
      f6.renderDetailSupports(detail);
    },
    // 一覧を読んだら、ホームの「対応が必要な人」も同じ応答から作り直す（読みを増やさない）
    onListLoaded: (body) => {
      f4.renderAttention(body);
      // 「期限が近い未申請の支援」（F6）も同じ応答から作る（読みは増えない）
      f6.renderSupportAlerts(body);
    },
  });

  // F2（訪問・見守りの記録）と F3（健康・介護の情報）の画面
  f2.bind({ openDetail: (survivorId) => f1.openDetail(survivorId) });

  // F4（悪化の兆しと訪問の順番）の画面。
  // F4 がホームのために読んだ一覧の応答を、F6 の知らせにも渡す（読みを増やさない）
  f4.bind({
    openDetail: (survivorId) => f1.openDetail(survivorId),
    onLoaded: (body) => f6.renderSupportAlerts(body),
  });

  // F6（受けられる支援と申請の状況）の画面
  f6.bind({
    openDetail: (survivorId) => f1.openDetail(survivorId),
    openConsultation: () => f8.openConsultationNew(),
    // 世帯の人数を直す入口（使いやすさの改修の計画.md 6-4）
    openProfile: () => profile.openProfile(),
  });

  // 支援の画面（ご本人。使いやすさの改修の計画.md 6-2）。AIで調べる画面は F6 のものを使う
  programs.bind({ openFinder: () => openSupportFinder() });

  // F7（組織全体の状況）の画面。ホームの入口から開くので、渡すものは無い
  f7.bind();

  // F8（被災者からの相談）の画面
  f8.bind({ openDetail: (survivorId) => f1.openDetail(survivorId) });

  // F5（受け入れ先の施設と移る先の決定）の画面。
  // 決めたあと「引き継ぎシートを作る」へ進むときは、決めた移る先を初期値に渡す。
  // 「居場所を変える（記録に戻る）」へ進むときは、施設の種類から決めた居場所を初期値に渡す
  f5.bind({
    openDetail: (survivorId) => f1.openDetail(survivorId, { location: f5.suggestedLocation() }),
    openHandover: () => handovers.openHandoverNew(f5.destination()),
  });

  // 周りの地域の状況（使いやすさの改修の計画.md 8章）
  disaster.bind();

  // F3（引き継ぎシート）の画面
  handovers.bind({ openDetail: (survivorId) => f1.openDetail(survivorId) });

  // 施設の職員の「自分の施設」（使いやすさの改修の計画.md 9章）
  facilityStaff.bind();
  // 家族などによる代わりの登録とつなぎ（使いやすさの改修の計画.md 10章）
  family.bind();

  // プロフィール（要望4）。保存のあとは、ホームに出ているお名前・立場も合わせる
  profile.bind({
    onSaved: (me) => {
      currentMe = me;
      f1.setMe(me);
      f8.setMe(me);
      el("home-profile-notice").hidden = !me.profile_incomplete;
      el("home-name").textContent = me.display_name || "（未設定）";
      el("home-role").textContent =
        me.role === "professional" && me.profession
          ? `${ROLE_LABELS[me.role]}（${me.profession}）`
          : ROLE_LABELS[me.role];
    },
    // ご本人が支援の組織を選び直したとき（追加修正⑦）。ホームの案内とタブが変わるので
    // 作り直し、プロフィールの画面に戻す（知らせは profile.js が出す）
    onOrganizationChanged: (me) => {
      renderHome(me);
      return profile.openProfile();
    },
  });

  // 組織に入る・組織を作る（要望1・2。組織がない支援者だけ）
  let pendingCreatedMe = null;
  organizations.bind({
    // 組織に入ると、その場でホームを作り直す（記録に触れる立場になるため）
    onJoined: (me) => renderHome(me),
    // 組織を作ったときは、発行した参加コードの画面を先に見せる（organizations.js が
    // すでに画面を切り替えている）。ホームに進むのは「ホームに進む」を押したとき
    onCreated: (result) => {
      pendingCreatedMe = result.me;
    },
    // 自分をオーナーから外したとき（追加修正⑥）。/api/me を読み直して、立場の表示を合わせる
    onOwnerChanged: async () => {
      const res = await apiFetch("/api/me");
      if (!res.ok) return;
      const me = await res.json();
      currentMe = me;
      profile.setMe(me);
      organizations.setMe(me);
    },
  });
  el("org-created-continue").addEventListener("click", () => {
    if (pendingCreatedMe) renderHome(pendingCreatedMe);
  });
}

// ログインの前と、登録の前にだけ出す画面
const SIGNED_OUT_SCREENS = new Set(["screen-loading", "screen-login"]);
const ONBOARDING_SCREENS = new Set(["screen-choice", "screen-survivor-self", "screen-supporter"]);

/** 「戻る」で出してよい画面を決める（ログアウトしたあとに記録の画面へ戻らない、など）。 */
function historyGuard(target) {
  if (!currentMe) {
    // 登録の前（ログイン済み）は、選択と登録の画面の中だけを行き来する
    if (!el("sign-out").hidden) return ONBOARDING_SCREENS.has(target) ? target : "screen-choice";
    return SIGNED_OUT_SCREENS.has(target) ? target : "screen-login";
  }
  if (SIGNED_OUT_SCREENS.has(target) || ONBOARDING_SCREENS.has(target)) return "screen-home";
  return target;
}

/** 「戻る」で画面を出したあと、下のタブの印を合わせる。 */
function onHistoryShown(screen) {
  const tabByScreen = {
    "screen-home": "home",
    "screen-survivors": "list",
    "screen-profile": "profile",
    "screen-programs": "supports",
    "screen-my-facility": "facility",
  };
  if (tabByScreen[screen]) setActiveTab(tabByScreen[screen]);
}

/** JavaScript が途中で止まったことを、画面に出す。

  画面の切り替えより前で止まると「押しても何も起きない」状態になり、
  原因がまったく分からない（実際に、存在しないIDを探して止まった）。 */
function watchScriptErrors() {
  const report = (message) =>
    showNotice(`画面の処理が途中で止まりました：${message}。開発者ツールのコンソールもご確認ください`);
  window.addEventListener("error", (event) => report(event.message));
  window.addEventListener("unhandledrejection", (event) =>
    report(event.reason?.message || String(event.reason)),
  );
}

async function main() {
  watchScriptErrors();
  // index.html の data-icon にアイコンを描く（使いやすさの改修の計画.md 2章・7章）
  applyIcons();
  // 送る前に、欄ごとに入力を確かめる（使いやすさの改修の計画.md 4-2の2）
  installFormValidation();
  // 相談と訪問の記録の欄に、声の入力のボタンを付ける（使いやすさの改修の計画.md 13章）
  installVoiceInput();
  // スマートフォンの「戻る」で前の画面に戻る（使いやすさの改修の計画.md 4-2の1）
  setHistoryHandlers({ guard: historyGuard, onShown: onHistoryShown });
  applyTextSize(savedTextSize());
  bindEvents();
  try {
    await startAuth({
      onUser: onSignedIn,
      onSignedOut,
      onError: (message) => showNotice(message),
    });
  } catch (err) {
    showScreen("screen-login");
    showNotice(`アプリを読み込めませんでした：${err.message}`);
  }
}

main();
