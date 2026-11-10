// 組織に入る・組織を作る（要望1・2。組織がない支援者だけが使う画面）
//
// - 組織がない一般の支援者は、記録には触れない（要件定義書10-3と同じ考え方。
//   バックエンドの require_roles が組織を求める）。できるのは、参加コードで組織に
//   入るか、組織を作るか、プロフィールを直すことだけ。
// - 組織を作ると、専門職用・一般の支援者用の参加コードが発行される。作った人は
//   その組織のオーナーになり、あとからプロフィールの画面でコードを見られる（追加修正⑥）。
// - オーナーの欄（renderOwnerPanel）：参加コードを見る・発行し直す、組織の専門職に
//   オーナーの権限を付ける・外す。使えるかどうかはサーバーが毎回決める（オーナーでなければ403）。
//   参加コードの値は console にも出さない。

import { apiFetch, getConfig, readError } from "./auth.js";
import { bindMunicipalityPicker } from "./municipalities.js";
import {
  clear,
  confirmDialog,
  el,
  fillSelect,
  node,
  showFormError,
  showNotice,
  showScreen,
  withBusy,
} from "./ui.js";

let me = null;
let onJoined = () => {};
let onCreated = () => {};

/** 立場を受け取る（ホームから渡される）。 */
export function setMe(value) {
  me = value;
}

function professions() {
  return getConfig()?.choices?.professions || [];
}

/** 「参加コードで組織に入る」を押したとき。 */
export function openJoinOrganization() {
  showNotice("");
  const form = el("form-join-organization");
  form.reset();
  showFormError(form, "");
  // 専門職でない方は「該当なし」を選ぶ（追加修正③）
  fillSelect(
    el("join-org-profession"),
    [getConfig()?.choices?.no_profession, ...professions()].filter(Boolean),
    { placeholder: "選んでください" },
  );
  showScreen("screen-join-organization");
}

async function submitJoin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const body = {
    join_code: el("join-org-code").value,
    profession: el("join-org-profession").value || null,
  };
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/me/join", { method: "POST", body: JSON.stringify(body) });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    const updated = await res.json();
    showNotice("組織に入りました。", "ok");
    onJoined(updated);
  });
}

// 受け持つ市区町村の一覧（要望8の一覧を使い回す）。
let createOrgPickerLoaded = false;

async function ensureCreateOrgPicker() {
  if (createOrgPickerLoaded) return;
  await bindMunicipalityPicker({
    prefectureSelect: el("create-org-prefecture"),
    prefectureFilterInput: el("create-org-prefecture-filter"),
    citySelect: el("create-org-municipality"),
    filterInput: el("create-org-municipality-filter"),
  });
  createOrgPickerLoaded = true;
}

/** 「組織を作る」を押したとき。 */
export function openCreateOrganization() {
  showNotice("");
  const form = el("form-create-organization");
  form.reset();
  // reset で絞り込みの文字は消えるが、選択肢は絞り込んだままなので作り直す（追加修正⑤）
  el("create-org-prefecture-filter").dispatchEvent(new Event("input"));
  showFormError(form, "");
  fillSelect(el("create-org-profession"), professions(), { placeholder: "選んでください" });
  showScreen("screen-create-organization");
  ensureCreateOrgPicker().catch((err) => showNotice(err.message));
}

function prefectureName() {
  const select = el("create-org-prefecture");
  return select.options[select.selectedIndex]?.textContent || "";
}

async function submitCreate(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const body = {
    org_name: el("create-org-name").value,
    prefecture: prefectureName(),
    municipality_code: el("create-org-municipality").value,
    profession: el("create-org-profession").value || null,
  };
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/organizations", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    const created = await res.json();
    el("org-created-name").textContent = created.organization?.org_name || "";
    el("org-created-pro-code").textContent = created.pro_join_code || "";
    el("org-created-vol-code").textContent = created.vol_join_code || "";
    showScreen("screen-organization-created");
    onCreated(created);
  });
}

// --- 組織のオーナーの欄（追加修正⑥。プロフィールの画面） ---

// 自分のオーナーの権限が変わったとき（自分を外したとき）に知らせる先
let onOwnerChanged = () => {};

function showStatus(id, text, kind) {
  const target = el(id);
  target.textContent = text || "";
  target.className = kind ? `notice-box notice-${kind}` : "note";
  target.hidden = !text;
}

function renderCodes(body) {
  el("profile-owner-pro-code").textContent = body.pro_join_code || "—";
  el("profile-owner-vol-code").textContent = body.vol_join_code || "—";
  if (body.available) {
    showStatus("profile-owner-codes-status", "");
  } else {
    // 元のコードを保存する前に作った組織（初期データなど）
    showStatus(
      "profile-owner-codes-status",
      "この組織の参加コードは保存されていません。「参加コードを発行し直す」を押すと、新しいコードが出ます。",
      "warn",
    );
  }
}

async function loadCodes() {
  showStatus("profile-owner-codes-status", "読み込んでいます…");
  const res = await apiFetch("/api/organizations/join-codes");
  if (!res.ok) {
    showStatus("profile-owner-codes-status", await readError(res), "ng");
    return;
  }
  renderCodes(await res.json());
}

function renderMembers(members) {
  const list = el("profile-owner-members");
  clear(list);
  if (!members.length) {
    list.append(node("li", null, "専門職がいません"));
    return;
  }
  for (const member of members) {
    const item = node("li");
    const label = [member.display_name || "（表示名なし）"];
    if (member.profession) label.push(`（${member.profession}）`);
    if (member.is_owner) label.push("・オーナー");
    item.append(node("span", null, label.join("")));
    const self = member.user_id === me?.user_id;
    const button = node(
      "button",
      "button",
      member.is_owner ? (self ? "自分をオーナーから外す" : "オーナーから外す") : "オーナーにする",
    );
    button.type = "button";
    button.addEventListener("click", () =>
      withBusy(button, () =>
        changeOwner(member, !member.is_owner).catch((err) => showNotice(err.message)),
      ),
    );
    item.append(button);
    list.append(item);
  }
}

async function loadMembers() {
  showStatus("profile-owner-members-status", "読み込んでいます…");
  const res = await apiFetch("/api/organizations/members");
  if (!res.ok) {
    showStatus("profile-owner-members-status", await readError(res), "ng");
    return;
  }
  showStatus("profile-owner-members-status", "");
  renderMembers((await res.json()).members || []);
}

async function changeOwner(member, owner) {
  const name = member.display_name || "この人";
  const message = owner
    ? `${name}さんをオーナーにします。参加コードを見る・発行し直すことができるようになります。よろしいですか？`
    : `${name}さんをオーナーから外します。よろしいですか？`;
  if (!(await confirmDialog(message))) return;
  const res = await apiFetch("/api/organizations/owners", {
    method: "POST",
    body: JSON.stringify({ user_id: member.user_id, owner }),
  });
  if (!res.ok) {
    showStatus("profile-owner-members-status", await readError(res), "ng");
    return;
  }
  const body = await res.json();
  showStatus("profile-owner-members-status", owner ? "オーナーにしました。" : "オーナーから外しました。", "ok");
  if (!body.me_is_owner) {
    // 自分を外した。オーナーの欄を閉じ、/api/me を読み直してもらう
    el("profile-owner").hidden = true;
    onOwnerChanged();
    return;
  }
  renderMembers(body.members || []);
}

async function reissueCodes() {
  const ok = await confirmDialog(
    "参加コードを発行し直します。前のコードでは、もう組織に入れなくなります" +
      "（すでに入っている人はそのままです）。よろしいですか？",
    { ok: "発行し直す", danger: true },
  );
  if (!ok) return;
  const res = await apiFetch("/api/organizations/join-codes", { method: "POST" });
  if (!res.ok) {
    showStatus("profile-owner-codes-status", await readError(res), "ng");
    return;
  }
  renderCodes(await res.json());
  showNotice("参加コードを発行し直しました。前のコードは使えません。", "ok");
}

/** プロフィールの画面で、オーナーの欄を出す（オーナーでなければ隠す）。 */
export async function renderOwnerPanel() {
  const panel = el("profile-owner");
  panel.hidden = !me?.is_owner;
  if (panel.hidden) return;
  el("profile-owner-pro-code").textContent = "";
  el("profile-owner-vol-code").textContent = "";
  clear(el("profile-owner-members"));
  await Promise.all([loadCodes(), loadMembers()]);
}

// --- イベントの登録 ---

export function bind({
  onJoined: joinedHandler,
  onCreated: createdHandler,
  onOwnerChanged: ownerChangedHandler,
}) {
  onJoined = joinedHandler || (() => {});
  onCreated = createdHandler || (() => {});
  onOwnerChanged = ownerChangedHandler || (() => {});
  el("form-join-organization").addEventListener("submit", submitJoin);
  el("form-create-organization").addEventListener("submit", submitCreate);
  el("profile-owner-reissue").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () => reissueCodes().catch((err) => showNotice(err.message))),
  );
}
