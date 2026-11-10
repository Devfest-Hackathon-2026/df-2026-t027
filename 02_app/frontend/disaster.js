// 周りの地域の状況の画面（使いやすさの改修の計画.md 8章）
//
// - ホームの「周りの地域の状況」のカードと、専門職が状況を入れる・直す画面をここで持つ。
// - ホームのカードは全員に出す。本人は住んでいる市区町村、支援者は活動する市区町村を先に、
//   同じ県のほかの市区町村をその下に出す。どの市区町村かはサーバーが決める（画面では決めない）。
// - ホームの表示を止めないように、カードは待たずに読む（f4・f8 と同じ形）。
// - 項目の選択肢と、古い情報に付ける知らせの文言は API が返すものを使う
//   （正本は `services/disaster_status.py`。画面に書き写さない）。
// - 入れられるのは専門職だけ（組織のオーナーも専門職）。直せるのは自分の組織が入れたものだけで、
//   それは API の `editable` で分かる。
// - 出典のリンクは別のタブで開き、開いた先にこのアプリを触らせない（rel="noopener"）。

import { apiFetch, readError } from "./auth.js";
import { bindMunicipalityPicker } from "./municipalities.js";
import {
  clear,
  doneSkeleton,
  el,
  fillSelect,
  formatDate,
  node,
  showFormError,
  showNotice,
  showScreen,
  showSkeleton,
  withBusy,
} from "./ui.js";

const ROLE_PROFESSIONAL = "professional";

let me = null;
let picker = null;
// いま直している1件（空なら新しく入れる）
let editing = null;

/** 立場を受け取る（ホームから渡される）。 */
export function setMe(value) {
  me = value;
}

function isProfessional() {
  return me?.role === ROLE_PROFESSIONAL && Boolean(me?.org_id);
}

async function fetchStatus(municipalityCode) {
  const query = municipalityCode ? `?municipality_code=${encodeURIComponent(municipalityCode)}` : "";
  const res = await apiFetch(`/api/disaster-status${query}`);
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

// --- 1件のカード ---

function renderItem(item, { onEdit } = {}) {
  const li = node("li", "card support");
  li.append(node("h4", "row-title", `${item.municipality_name}：${item.item_kind_label}`));

  const marks = node("div", "row-marks");
  marks.append(node("span", "badge badge-note", item.disaster_name));
  if (item.deadline) marks.append(node("span", "badge badge-medium", `期限 ${formatDate(item.deadline)}`));
  if (item.is_stale) marks.append(node("span", "badge badge-high", item.stale_notice));
  li.append(marks);

  li.append(node("p", null, item.state));
  if (item.note) li.append(node("p", "row-note", item.note));
  li.append(node("p", "row-note", `更新した日：${formatDate(item.updated_at)}`));

  const source = node("p", "row-note");
  source.append("出典：");
  const link = node("a", null, item.source_url);
  link.href = item.source_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  source.append(link);
  li.append(source);

  if (onEdit && item.editable) {
    const actions = node("div", "actions");
    const button = node("button", "button", "この内容を直す");
    button.type = "button";
    button.addEventListener("click", () => onEdit(item));
    actions.append(button);
    li.append(actions);
  }
  return li;
}

/** 自分の市区町村と、同じ県のほかの市区町村に分けて描く。 */
function renderGroups(container, body, { onEdit } = {}) {
  doneSkeleton(container);
  clear(container);
  const groups = [
    { title: `${body.municipality_name || "選んだ市区町村"}の状況`, items: body.own || [] },
    { title: `${body.prefecture || "同じ県"}のほかの市区町村`, items: body.nearby || [] },
  ];
  for (const group of groups) {
    container.append(node("h3", "row-title", group.title));
    if (group.items.length === 0) {
      container.append(node("p", "note", "いま入っている情報はありません。"));
      continue;
    }
    const list = node("ul", "cards");
    for (const item of group.items) list.append(renderItem(item, { onEdit }));
    container.append(list);
  }
}

function areaText(body) {
  if (!body.municipality_code) return body.notice || "";
  return `${body.prefecture} ${body.municipality_name}と、同じ県の市区町村の状況です。` +
    `更新から${body.stale_days}日を超えたものには印が付きます。`;
}

// --- ホームのカード ---

/** ホームに「周りの地域の状況」を出す。呼ぶ側は待たずに呼んでよい（失敗しても知らせるだけ）。 */
export async function loadHomeStatus() {
  const card = el("home-disaster-card");
  if (!me?.role) {
    card.hidden = true;
    return;
  }
  const list = el("home-disaster-list");
  el("home-disaster-area").textContent = "読み込んでいます…";
  showSkeleton(list, 2);
  card.hidden = false;
  let body;
  try {
    body = await fetchStatus("");
  } catch (err) {
    doneSkeleton(list);
    clear(list);
    el("home-disaster-area").textContent = "読み込めませんでした。読み直してください";
    return;
  }
  el("home-disaster-area").textContent = areaText(body);
  if (!body.municipality_code) {
    doneSkeleton(list);
    clear(list);
    return;
  }
  renderGroups(list, body);
}

// --- 状況を入れる・直す画面（専門職だけ） ---

/** 「周りの地域の状況を入れる」を開く（ホームのタイルから）。 */
export async function openEditor() {
  showNotice("");
  if (!isProfessional()) {
    showNotice("周りの地域の状況を入れられるのは、組織に入っている専門職だけです");
    return;
  }
  showScreen("screen-disaster");
  showSkeleton(el("disaster-list"), 2);
  el("disaster-area").textContent = "読み込んでいます…";
  if (!picker) {
    picker = await bindMunicipalityPicker({
      prefectureSelect: el("disaster-prefecture"),
      prefectureFilterInput: el("disaster-prefecture-filter"),
      citySelect: el("disaster-municipality"),
      filterInput: el("disaster-municipality-filter"),
      // 市区町村を選び直したら、その市区町村と同じ県の状況を読み直す
      onCityChange: (code) => {
        if (code) loadEditorList(code).catch((err) => showNotice(err.message));
      },
    });
  }
  // 最初は自分の市区町村で開く（どこかはサーバーが決める）
  const body = await fetchStatus("");
  fillSelect(el("disaster-kind"), body.item_kinds || [], { placeholder: "選んでください" });
  resetForm();
  if (body.municipality_code) picker.setCode(body.municipality_code);
  renderEditorList(body);
}

async function loadEditorList(code) {
  el("disaster-area").textContent = "読み込んでいます…";
  showSkeleton(el("disaster-list"), 2);
  renderEditorList(await fetchStatus(code));
}

function renderEditorList(body) {
  el("disaster-area").textContent = areaText(body);
  const list = el("disaster-list");
  if (!body.municipality_code) {
    doneSkeleton(list);
    clear(list);
    return;
  }
  renderGroups(list, body, { onEdit: startEdit });
}

/** フォームを「新しく入れる」に戻す。 */
function resetForm() {
  editing = null;
  const form = el("form-disaster");
  showFormError(form, "");
  el("disaster-form-title").textContent = "状況を入れる";
  el("disaster-name").value = "";
  el("disaster-kind").value = "";
  el("disaster-state").value = "";
  el("disaster-deadline").value = "";
  el("disaster-source-url").value = "";
  el("disaster-note").value = "";
  el("disaster-ended").checked = false;
  el("disaster-ended-field").hidden = true;
  el("disaster-cancel-edit").hidden = true;
  el("disaster-submit").textContent = "この内容で入れる";
}

/** 1件を直すために、フォームに入れて見せる。 */
function startEdit(item) {
  editing = item;
  showFormError(el("form-disaster"), "");
  el("disaster-form-title").textContent = "状況を直す";
  picker.setCode(item.municipality_code);
  el("disaster-name").value = item.disaster_name || "";
  el("disaster-kind").value = item.item_kind || "";
  el("disaster-state").value = item.state || "";
  el("disaster-deadline").value = item.deadline || "";
  el("disaster-source-url").value = item.source_url || "";
  el("disaster-note").value = item.note || "";
  el("disaster-ended").checked = Boolean(item.ended);
  el("disaster-ended-field").hidden = false;
  el("disaster-cancel-edit").hidden = false;
  el("disaster-submit").textContent = "この内容に直す";
  el("form-disaster").scrollIntoView({ behavior: "smooth", block: "start" });
}

function formBody() {
  return {
    municipality_code: el("disaster-municipality").value,
    disaster_name: el("disaster-name").value,
    item_kind: el("disaster-kind").value,
    state: el("disaster-state").value,
    // 空なら期限なし（直すときは、空で送ると期限を消す）
    deadline: el("disaster-deadline").value,
    source_url: el("disaster-source-url").value.trim(),
    note: el("disaster-note").value,
  };
}

async function saveStatus(body) {
  if (editing) {
    return apiFetch(`/api/disaster-status/${encodeURIComponent(editing.status_id)}`, {
      method: "PATCH",
      body: JSON.stringify({ ...body, ended: el("disaster-ended").checked }),
    });
  }
  return apiFetch("/api/disaster-status", {
    method: "POST",
    body: JSON.stringify({ ...body, deadline: body.deadline || null }),
  });
}

async function submitStatus(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const body = formBody();
  await withBusy(el("disaster-submit"), async () => {
    const wasEditing = Boolean(editing);
    const res = await saveStatus(body);
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    resetForm();
    showNotice(wasEditing ? "直しました。前の内容も残っています。" : "入れました。", "ok");
    await loadEditorList(body.municipality_code);
  });
}

/** ボタンとフォームを結び付ける（app.js が1回だけ呼ぶ）。 */
export function bind() {
  el("home-disaster-reload").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () => loadHomeStatus().catch((err) => showNotice(err.message))),
  );
  el("form-disaster").addEventListener("submit", submitStatus);
  el("disaster-cancel-edit").addEventListener("click", () => resetForm());
}
