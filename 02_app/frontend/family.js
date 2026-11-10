// 家族などによる代わりの登録とつなぎ（使いやすさの改修の計画.md 10章）
//
// - 一般の支援者（ご家族。組織がなくてもよい）が、ご本人の代わりに記録を登録する。
//   本人の登録と同じ欄に「ご本人の同意」を足す（同意を得た／同意できない状態。必須）。
// - 登録した人と、招待コードでつながった人は「家族の記録」で記録を見て直せる。
//   見られるのは基本の情報と居場所だけで、健康・介護の情報は出さない（APIも返さない）。
// - 代わりに登録した人は確認コードを出せる。ご本人は登録の画面の「確認コード」に入れてつながる。
// - ご本人はプロフィールから招待コードを出し、つながっている家族を外せる。
// - 立場・見てよいかはサーバーが決める（要件定義書10-3）。画面は返ってきたものを出すだけ。

import { apiFetch, getConfig, readError } from "./auth.js";
import { bindMunicipalityPicker, describeMunicipality } from "./municipalities.js";
import { createOrganizationPicker } from "./orgpicker.js";
import {
  addPair,
  clear,
  confirmDialog,
  doneSkeleton,
  el,
  fillSelect,
  formatDate,
  nameWithAge,
  node,
  showFormError,
  showNotice,
  showScreen,
  showSkeleton,
  withBusy,
} from "./ui.js";

// いま開いている家族の記録（GET /api/family/survivors/{id} の応答）
let record = null;
let orgPicker = null;
let newPickerLoaded = false;

function numberOrNull(value) {
  const text = (value || "").trim();
  if (!text) return null;
  const parsed = Number(text);
  return Number.isInteger(parsed) ? parsed : null;
}

function fillChoices(stageId, locationTypeId) {
  const choices = getConfig()?.choices;
  if (!choices) return;
  fillSelect(el(stageId), choices.stages, { placeholder: "選んでください" });
  fillSelect(el(locationTypeId), choices.location_types, { placeholder: "選んでください" });
}

// --- 代わりに登録する ---

async function ensureNewPickers() {
  if (newPickerLoaded) return;
  orgPicker = createOrganizationPicker({
    select: el("family-org"),
    filterInput: el("family-org-filter"),
    note: el("family-org-note"),
  });
  await bindMunicipalityPicker({
    prefectureSelect: el("family-prefecture"),
    prefectureFilterInput: el("family-prefecture-filter"),
    citySelect: el("family-municipality"),
    filterInput: el("family-municipality-filter"),
    // 市区町村を選び直したら、受け持つ組織を上に出し直す（本人の登録と同じ）
    onCityChange: (code) => {
      orgPicker.setMunicipality(code).catch((err) => showNotice(err.message));
    },
  });
  await orgPicker.load("");
  newPickerLoaded = true;
}

/** 「家族などを代わりに登録する」の画面を開く。 */
export async function openProxyNew() {
  showNotice("");
  const form = el("form-family-new");
  form.reset();
  showFormError(form, "");
  fillChoices("family-stage", "family-location-type");
  showScreen("screen-family-new");
  await ensureNewPickers();
}

function proxyBody() {
  return {
    proxy_consent: el("family-consent").value,
    municipality_code: el("family-municipality").value,
    org_id: orgPicker ? orgPicker.value() : "",
    full_name: el("family-name").value,
    stage: el("family-stage").value,
    location_type: el("family-location-type").value,
    location_detail: el("family-location-detail").value,
    age: numberOrNull(el("family-age").value),
    household_size: numberOrNull(el("family-household").value),
    contact: el("family-contact").value,
    address: el("family-address").value,
  };
}

async function submitProxyNew(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const body = proxyBody();
  // 組織を選んだときは、その組織の専門職に記録が見えることを確かめてもらう（本人の登録と同じ）
  if (
    body.org_id &&
    !(await confirmDialog(
      `「${orgPicker.label() || "選んだ組織"}」の専門職（保健師・相談員など）に、` +
        "この方の記録（あとで入る健康・介護の情報を含む）が見えるようになります。この組織で登録しますか？",
      { ok: "この組織で登録する" },
    ))
  ) {
    return;
  }
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/family/survivors", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    const created = await res.json();
    await openRecord(created.survivor.survivor_id);
    showNotice("登録しました。ご本人とつなぐときは「確認コードを出す」を押してください。", "ok");
  });
}

// --- 家族の記録の一覧 ---

/** 「家族の記録」の一覧を開く。 */
export async function openList() {
  showNotice("");
  const list = el("family-list");
  el("family-list-empty").hidden = true;
  showFormError(el("form-family-join"), "");
  showScreen("screen-family-list");
  showSkeleton(list);
  const res = await apiFetch("/api/family/survivors");
  doneSkeleton(list);
  if (!res.ok) {
    clear(list);
    showNotice(await readError(res));
    return;
  }
  const body = await res.json();
  clear(list);
  for (const item of body.survivors || []) list.append(renderListItem(item));
  el("family-list-empty").hidden = (body.survivors || []).length > 0;
}

function renderListItem(item) {
  const li = node("li", "card row");
  const button = node("button", "row-button");
  button.type = "button";
  button.append(node("span", "row-title", nameWithAge(item.full_name, item.age)));
  const marks = node("span", "row-marks");
  if (item.registered_by_family) marks.append(node("span", "badge badge-note", "代わりに登録"));
  marks.append(
    node("span", "badge badge-note", item.linked_to_self ? "ご本人とつながっています" : "ご本人未登録"),
  );
  button.append(marks);
  button.append(
    node(
      "span",
      "row-note",
      `${item.stage_label}／${item.location_type_label}${
        item.location_detail ? `（${item.location_detail}）` : ""
      }`,
    ),
  );
  button.addEventListener("click", () =>
    withBusy(button, () => openRecord(item.survivor_id).catch((err) => showNotice(err.message))),
  );
  li.append(button);
  return li;
}

async function submitJoin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch("/api/family/join", {
      method: "POST",
      body: JSON.stringify({ code: el("family-join-code").value }),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    form.reset();
    await openList();
    showNotice("家族としてつながりました。", "ok");
  });
}

// --- 家族の記録 ---

/** 家族として記録を開く（つながっていなければサーバーが 404 を返す）。 */
export async function openRecord(survivorId) {
  showNotice("");
  const res = await apiFetch(`/api/family/survivors/${encodeURIComponent(survivorId)}`);
  if (!res.ok) {
    showNotice(await readError(res));
    return;
  }
  record = await res.json();
  renderRecord();
  showScreen("screen-family-record");
}

function renderRecord() {
  const survivor = record.survivor;
  el("family-record-title").textContent = nameWithAge(survivor.full_name, survivor.age);
  el("family-record-subtitle").textContent = survivor.registered_by_family
    ? "ご家族などが代わりに登録した記録です。"
    : "招待コードでつながった記録です。";

  const basic = el("family-record-basic");
  clear(basic);
  addPair(basic, "お名前", survivor.full_name);
  addPair(basic, "年齢", survivor.age === null || survivor.age === undefined ? "" : `${survivor.age}歳`);
  addPair(basic, "世帯の人数", survivor.household_size ? `${survivor.household_size}人` : "");
  addPair(basic, "連絡先", survivor.contact);
  addPair(basic, "住所", survivor.address);
  if (survivor.municipality_code) {
    const value = node("dd", null, "読み込んでいます…");
    basic.append(node("dt", null, "住んでいる市区町村"), value);
    describeMunicipality(survivor.municipality_code)
      .then((label) => {
        value.textContent = label || survivor.municipality_code;
      })
      .catch(() => {
        value.textContent = survivor.municipality_code;
      });
  }
  addPair(
    basic,
    "今いる場所",
    survivor.location_detail
      ? `${survivor.location_type_label}（${survivor.location_detail}）`
      : survivor.location_type_label,
  );
  addPair(basic, "今の段階", survivor.stage_label);
  if (survivor.proxy_consent_label) addPair(basic, "ご本人の同意", survivor.proxy_consent_label);
  addPair(basic, "支援の組織", survivor.has_org ? "つながっています" : "つながっていません");
  addPair(basic, "登録", formatDate(survivor.registered_at));

  // 本人とつなぐ（つながる前だけ）
  el("family-link-card").hidden = survivor.linked_to_self;
  el("family-linked-note").hidden = !survivor.linked_to_self;
  el("family-link-box").hidden = true;

  // 居場所を変える・基本の情報を直すの初期値
  fillChoices("family-edit-stage", "family-edit-location-type");
  el("family-edit-stage").value = survivor.stage || "";
  el("family-edit-location-type").value = survivor.location_type || "";
  el("family-edit-location-detail").value = survivor.location_detail || "";
  el("family-edit-name").value = survivor.full_name || "";
  el("family-edit-age").value = survivor.age ?? "";
  el("family-edit-household").value = survivor.household_size ?? "";
  el("family-edit-contact").value = survivor.contact || "";
  el("family-edit-address").value = survivor.address || "";
  showFormError(el("form-family-location"), "");
  showFormError(el("form-family-basic"), "");

  renderHistory();
}

function renderHistory() {
  const list = el("family-record-history");
  clear(list);
  const history = record.location_history || [];
  if (history.length === 0) {
    list.append(node("li", "note", "まだ変更がありません。"));
    return;
  }
  for (const item of history) {
    const place = `${item.location_type_label}${item.location_detail ? `（${item.location_detail}）` : ""}`;
    const text = item.prev_location_type
      ? `${formatDate(item.changed_at)}：${item.prev_location_type_label} → ${place}`
      : `${formatDate(item.changed_at)}：${place}（登録のとき）`;
    list.append(node("li", null, `${text}／${item.stage_label}`));
  }
}

async function issueLinkCode(event) {
  const button = event.currentTarget;
  if (!record) return;
  await withBusy(button, async () => {
    const res = await apiFetch(
      `/api/family/survivors/${encodeURIComponent(record.survivor.survivor_id)}/link-code`,
      { method: "POST" },
    );
    if (!res.ok) {
      showNotice(await readError(res));
      return;
    }
    const body = await res.json();
    el("family-link-code").textContent = body.link_code.code;
    el("family-link-expires").textContent = `有効期限：${formatDate(body.link_code.expires_at)}まで`;
    el("family-link-box").hidden = false;
  });
}

async function submitLocation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(
      `/api/family/survivors/${encodeURIComponent(record.survivor.survivor_id)}/location`,
      {
        method: "POST",
        body: JSON.stringify({
          stage: el("family-edit-stage").value,
          location_type: el("family-edit-location-type").value,
          location_detail: el("family-edit-location-detail").value,
        }),
      },
    );
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    await openRecord(record.survivor.survivor_id);
    showNotice("居場所を変えました。前の居場所は履歴に残ります。", "ok");
  });
}

async function submitBasic(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(
      `/api/family/survivors/${encodeURIComponent(record.survivor.survivor_id)}/basic`,
      {
        method: "PATCH",
        body: JSON.stringify({
          full_name: el("family-edit-name").value,
          age: numberOrNull(el("family-edit-age").value),
          household_size: numberOrNull(el("family-edit-household").value),
          contact: el("family-edit-contact").value,
          address: el("family-edit-address").value,
        }),
      },
    );
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    await openRecord(record.survivor.survivor_id);
    showNotice("保存しました。", "ok");
  });
}

// --- ご本人：家族を招く・外す ---

/** ご本人の「家族とつなぐ」の画面を開く。 */
export async function openMembers() {
  showNotice("");
  el("family-invite-box").hidden = true;
  showScreen("screen-family-members");
  await loadMembers();
}

async function loadMembers() {
  const list = el("family-members");
  el("family-members-empty").hidden = true;
  showSkeleton(list, 2);
  const res = await apiFetch("/api/family/members");
  doneSkeleton(list);
  clear(list);
  if (!res.ok) {
    showNotice(await readError(res));
    return;
  }
  const members = (await res.json()).members || [];
  for (const member of members) list.append(renderMember(member));
  el("family-members-empty").hidden = members.length > 0;
}

function renderMember(member) {
  // 担当の一覧と同じ形の1行にする（f1.js の renderAssignments）
  const li = node("li", "assignment");
  const name = member.display_name || "（お名前なし）";
  li.append(node("span", null, name));
  li.append(
    node("span", "row-note", `${member.via_label || ""}／つないだ日：${formatDate(member.linked_at)}`),
  );
  const remove = node("button", "button", "外す");
  remove.type = "button";
  remove.addEventListener("click", async () => {
    const ok = await confirmDialog(
      `${name}さんを外しますか？外すと、${name}さんはあなたの記録を見られなくなります。`,
      { ok: "外す", danger: true },
    );
    if (!ok) return;
    await withBusy(remove, async () => {
      const res = await apiFetch(
        `/api/family/members/${encodeURIComponent(member.user_id)}/remove`,
        { method: "POST" },
      );
      if (!res.ok) {
        showNotice(await readError(res));
        return;
      }
      showNotice("外しました。", "ok");
      await loadMembers();
    });
  });
  li.append(remove);
  return li;
}

async function issueInvite(event) {
  await withBusy(event.currentTarget, async () => {
    const res = await apiFetch("/api/family/invites", { method: "POST" });
    if (!res.ok) {
      showNotice(await readError(res));
      return;
    }
    const body = await res.json();
    el("family-invite-code").textContent = body.invite_code.code;
    el("family-invite-expires").textContent = `有効期限：${formatDate(body.invite_code.expires_at)}まで`;
    el("family-invite-box").hidden = false;
  });
}

// --- イベントの登録 ---

export function bind() {
  el("form-family-new").addEventListener("submit", submitProxyNew);
  el("form-family-join").addEventListener("submit", submitJoin);
  el("form-family-location").addEventListener("submit", submitLocation);
  el("form-family-basic").addEventListener("submit", submitBasic);
  el("family-link-issue").addEventListener("click", issueLinkCode);
  el("family-invite-issue").addEventListener("click", issueInvite);
  el("family-list-new").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () => openProxyNew().catch((err) => showNotice(err.message))),
  );
  el("family-record-back").addEventListener("click", () => {
    openList().catch((err) => showNotice(err.message));
  });
  // プロフィール（ご本人）の「家族を招く・外す」
  el("profile-family-open").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () => openMembers().catch((err) => showNotice(err.message))),
  );
}
