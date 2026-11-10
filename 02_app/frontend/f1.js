// F1 被災者の記録と担当の画面（開発手順書 Step 5-3〜5-7）
//
// - 立場は /api/me が返すものだけを使う。画面で持っている値で判断しない（要件定義書10-3）。
// - 見る権限のない記録は API が 404 を返す。画面でも「見つかりません」と出すだけにする。
// - 確認コードは**登録の応答でしか返らない**。画面に出すのはそのときだけで、保存しない。
// - 氏名・連絡先・確認コードをコンソールに出さない。
// - 一覧は **F4 の訪ねる順番**（悪化の兆しの高い人から）で返ってくる。画面では並べ替えない
//   （並べ替えの順は `int_survivor_list` が正本。画面で並べ直すと2か所になる）。
// - 悪化の兆しのカードと度合いの変更は f4.js が描く（詳細を描き終わってから呼ぶ）。

import { apiFetch, getConfig, readError } from "./auth.js";
import { DEADLINE_FILTERS, matchesDeadline } from "./f6.js";
import { bindMunicipalityPicker, describeMunicipality } from "./municipalities.js";
import {
  addPair,
  clear,
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

const ROLE_PROFESSIONAL = "professional";
const ROLE_VOLUNTEER = "volunteer";
const ROLE_SURVIVOR = "survivor";

const STATUS_LABELS = { active: "有効", pending: "ご本人の承認待ち", removed: "外れました" };
const CONSENT_LABELS = { approved: "ご本人が承認", verbal_consent: "口頭の同意を記録" };

let me = null;
let detail = null;
let supporters = [];
let onHome = () => {};
// 詳細を描いたあとに呼ぶ（F2・F3 のカードは f2.js、F4 のカードは f4.js が描く。
// f1 からほかの画面のファイルを参照しないため、呼び出しは app.js につなぐ）
let onDetailRendered = () => {};
// 一覧を読み終わったあとに呼ぶ（ホームの「対応が必要な人」を同じ応答から作れるようにする）
let onListLoaded = () => {};

/** 立場を受け取る（ホームから渡される）。 */
export function setMe(value) {
  me = value;
}

function choices() {
  return getConfig()?.choices || { stages: [], location_types: [] };
}

function fillFilters() {
  const { stages, location_types } = choices();
  fillSelect(el("filter-stage"), stages, { placeholder: "すべての段階" });
  fillSelect(el("filter-location-type"), location_types, { placeholder: "すべての場所" });
  // 期限の近い未申請がある人だけに絞る（使いやすさの改修の計画.md 11章。専門職だけ）
  fillSelect(
    el("filter-deadline"),
    DEADLINE_FILTERS.map((item) => ({
      value: item.key,
      label: item.key === "all" ? "期限のある未申請がある人" : `未申請の期限：${item.label}`,
    })),
    { placeholder: "絞らない" },
  );
}

// 最後に読んだ一覧の応答（期限の絞り込みは画面の中で行い、読み直さない）
let lastListBody = null;

// IDは組み立てずに、そのまま書く。組み立てると取り違えても気づけない
// （`location` から `location-location-type` を作ってしまい、画面が止まった）。
function fillFormChoices(stageId, locationTypeId) {
  const { stages, location_types } = choices();
  fillSelect(el(stageId), stages, { placeholder: "選んでください" });
  fillSelect(el(locationTypeId), location_types, { placeholder: "選んでください" });
}

// --- 一覧 ---

export async function openList() {
  showNotice("");
  // 選択肢はここで作る。`bind()` の時点では /api/public-config をまだ読んでいないため、
  // そこで作ると「すべての段階」しか無い空の絞り込みになる
  fillFilters();
  const isProfessional = me?.role === ROLE_PROFESSIONAL;
  el("survivors-new").hidden = !isProfessional;
  el("filter-assignee-field").hidden = !isProfessional;
  el("filter-deadline-field").hidden = !isProfessional;
  el("survivors-note").textContent = isProfessional
    ? "担当する市町の方の一覧です。悪化の兆しが高い方から、訪ねる順番に出ます。"
    : "担当になっている方だけが、訪ねる順番に出ます。";
  showScreen("screen-survivors");
  await reloadList();
}

async function reloadList() {
  const params = new URLSearchParams();
  const stage = el("filter-stage").value;
  const locationType = el("filter-location-type").value;
  const assignee = el("filter-assignee").value;
  if (stage) params.set("stage", stage);
  if (locationType) params.set("location_type", locationType);
  if (assignee) params.set("assignee", assignee);

  const list = el("survivors-list");
  // 読むあいだ、中身の入る場所を灰色で出す（使いやすさの改修の計画.md 4-2の7）
  showSkeleton(list, 4);
  el("survivors-count").textContent = "読み込んでいます…";

  const res = await apiFetch(`/api/survivors?${params.toString()}`);
  if (!res.ok) {
    el("survivors-count").textContent = "";
    showNotice(await readError(res));
    return;
  }
  const body = await res.json();
  supporters = body.supporters || [];
  fillAssigneeFilter(assignee);
  renderDuplicates(body.possible_duplicates || []);
  // 「医療の診断ではない」という断り書きは、APIが返す文言をそのまま出す（F4）
  el("survivors-risk-note").textContent = body.attention?.disclaimer || "";
  onListLoaded(body);

  lastListBody = body;
  renderListRows();
}

/** 一覧の行を描く（期限の絞り込みは、ここで画面の中だけで行う）。 */
function renderListRows() {
  const body = lastListBody;
  const list = el("survivors-list");
  clear(list);
  if (!body) return;
  if (body.count === 0) {
    el("survivors-count").textContent = "0件";
    list.append(node("li", "card note", "まだ記録がありません。"));
    return;
  }
  const deadline = el("filter-deadline-field").hidden ? "" : el("filter-deadline").value;
  const rows = deadline
    ? body.survivors.filter(
        (item) =>
          (item.unapplied_count || 0) > 0 && matchesDeadline(item.days_to_nearest_deadline, deadline),
      )
    : body.survivors;
  el("survivors-count").textContent = deadline ? `${rows.length}件（全${body.count}件）` : `${body.count}件`;
  if (rows.length === 0) {
    list.append(node("li", "card note", "この絞り込みに当てはまる方はいません。"));
    return;
  }
  for (const item of rows) {
    list.append(renderListItem(item));
  }
}

function fillAssigneeFilter(selected) {
  if (me?.role !== ROLE_PROFESSIONAL) return;
  fillSelect(
    el("filter-assignee"),
    supporters.map((item) => ({ value: item.user_id, label: item.display_name || item.user_id })),
    { placeholder: "すべての担当" },
  );
  el("filter-assignee").value = selected || "";
}

function renderDuplicates(groups) {
  const box = el("survivors-duplicates");
  clear(box);
  if (groups.length === 0) {
    box.hidden = true;
    return;
  }
  box.append(
    node("p", null, "同じ方が二重に登録されているおそれがあります。確かめてください。"),
  );
  const list = node("ul", "list");
  for (const group of groups) {
    list.append(node("li", null, `${group.full_name}（${group.ages.join("歳・")}歳）`));
  }
  box.append(list);
  box.hidden = false;
}

function renderListItem(item) {
  const li = node("li", "card row");
  const button = node("button", "row-button");
  button.type = "button";

  const title = node("span", "row-title", nameWithAge(item.full_name, item.age));
  button.append(title);

  const marks = node("span", "row-marks");
  if (item.risk_level) {
    marks.append(
      node("span", `badge badge-${item.risk_level}`, `悪化の兆し：${item.risk_level_label}`),
    );
  }
  if (item.risk_source === "professional") {
    marks.append(node("span", "badge badge-note", "専門職が判断"));
  }
  if (!item.linked_to_self) {
    marks.append(node("span", "badge badge-note", "ご本人未登録"));
  }
  // 家族などが代わりに登録した記録（使いやすさの改修の計画.md 10章）
  if (item.registered_by_family) {
    marks.append(node("span", "badge badge-note", "家族が登録"));
  }
  if (item.assignee_count > 0) {
    marks.append(node("span", "badge badge-note", `担当${item.assignee_count}人`));
  }
  if (marks.childElementCount > 0) button.append(marks);

  button.append(
    node(
      "span",
      "row-note",
      `${item.stage_label}／${item.location_type_label}${
        item.location_detail ? `（${item.location_detail}）` : ""
      }`,
    ),
  );
  button.append(
    node(
      "span",
      "row-note",
      item.never_visited
        ? "まだ訪問の記録がありません"
        : `最後の訪問：${formatDate(item.last_visited_at)}（${item.days_since_last_visit}日前）`,
    ),
  );

  // 悪化の兆しの根拠と次にすることは、詳細を開かずに見えるようにする（訪ねる順番を決めるため）
  if (item.risk_next_action) {
    button.append(node("span", "row-note", `次にすること：${item.risk_next_action}`));
  }
  const evidence = (item.risk_evidence || [])[0];
  if (evidence) {
    button.append(node("span", "row-note", `根拠：「${evidence.quote}」`));
  }

  button.addEventListener("click", () => openDetail(item.survivor_id));
  li.append(button);
  return li;
}

// --- 登録 ---

// 住んでいる市区町村の一覧（要望8）。専門職の登録では組織がすでに決まっているので任意。
let newMunicipalityPickerLoaded = false;

async function ensureNewMunicipalityPicker() {
  if (newMunicipalityPickerLoaded) return;
  await bindMunicipalityPicker({
    prefectureSelect: el("new-prefecture"),
    prefectureFilterInput: el("new-prefecture-filter"),
    citySelect: el("new-municipality"),
    filterInput: el("new-municipality-filter"),
  });
  newMunicipalityPickerLoaded = true;
}

function openNew() {
  showNotice("");
  const form = el("form-survivor-new");
  form.reset();
  // reset で絞り込みの文字は消えるが、選択肢は絞り込んだままなので作り直す（追加修正⑤）
  el("new-prefecture-filter").dispatchEvent(new Event("input"));
  showFormError(form, "");
  el("similar-box").hidden = true;
  el("new-force").hidden = true;
  fillFormChoices("new-stage", "new-location-type");
  showScreen("screen-survivor-new");
  // 一覧の読み込みを待たずに画面を出す（ほかの入力欄はすぐ使える）
  ensureNewMunicipalityPicker().catch((err) => showNotice(err.message));
}

function newSurvivorBody(acknowledged) {
  return {
    full_name: el("new-name").value,
    age: numberOrNull(el("new-age").value),
    household_size: numberOrNull(el("new-household").value),
    contact: el("new-contact").value,
    municipality_code: el("new-municipality").value || null,
    address: el("new-address").value,
    stage: el("new-stage").value,
    location_type: el("new-location-type").value,
    location_detail: el("new-location-detail").value,
    acknowledged_similar: acknowledged,
  };
}

function numberOrNull(value) {
  const text = (value || "").trim();
  if (!text) return null;
  const parsed = Number(text);
  return Number.isInteger(parsed) ? parsed : null;
}

async function submitNew(acknowledged, button) {
  const form = el("form-survivor-new");
  showFormError(form, "");
  await withBusy(button, async () => {
    const res = await apiFetch("/api/survivors", {
      method: "POST",
      body: JSON.stringify(newSurvivorBody(acknowledged)),
    });
    if (res.status === 409) {
      const body = await res.json();
      renderSimilar(body.detail);
      return;
    }
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    const body = await res.json();
    showIssuedCode(body);
  });
}

function renderSimilar(detailBody) {
  el("similar-message").textContent =
    detailBody?.message || "氏名と年齢が近い記録があります。";
  const list = el("similar-list");
  clear(list);
  for (const item of detailBody?.similar || []) {
    list.append(
      node(
        "li",
        null,
        `${nameWithAge(item.full_name, item.age)}／${item.stage_label || ""} ${
          item.location_detail || ""
        }／登録：${formatDate(item.registered_at)}${
          // 家族が代わりに登録した記録は、家族の連絡先がある（使いやすさの改修の計画.md 10章）
          item.registered_by_family ? "／家族が登録した記録です" : ""
        }`,
      ),
    );
  }
  el("similar-box").hidden = false;
  el("new-force").hidden = false;
}

function showIssuedCode(body) {
  el("issued-code").textContent = body.link_code.code;
  el("issued-expires").textContent = `有効期限：${formatDate(body.link_code.expires_at)}まで`;
  el("issued-open").dataset.survivorId = body.survivor.survivor_id;
  showScreen("screen-link-code");
}

// --- 詳細 ---

// 施設を決めた直後に渡される居場所の初期値（F5 → F1。**1回だけ**使う）
let pendingLocation = null;

export async function openDetail(survivorId, { location } = {}) {
  showNotice("");
  const res = await apiFetch(`/api/survivors/${encodeURIComponent(survivorId)}`);
  if (!res.ok) {
    showNotice(await readError(res));
    return;
  }
  if (location) pendingLocation = location;
  detail = await res.json();
  renderDetail();
  showScreen("screen-survivor");
}

function renderDetail() {
  const survivor = detail.survivor;
  const isProfessional = me?.role === ROLE_PROFESSIONAL;
  const isSelf = me?.role === ROLE_SURVIVOR;

  el("detail-title").textContent = isSelf
    ? "ご自分の記録"
    : nameWithAge(survivor.full_name, survivor.age);
  el("detail-subtitle").textContent = `${survivor.stage_label}／${survivor.location_type_label}`;

  const basic = el("detail-basic");
  clear(basic);
  addPair(basic, "お名前", survivor.full_name);
  addPair(basic, "年齢", survivor.age === null || survivor.age === undefined ? "" : `${survivor.age}歳`);
  addPair(basic, "世帯の人数", survivor.household_size ? `${survivor.household_size}人` : "");
  addPair(basic, "連絡先", survivor.contact);
  addPair(basic, "住所", survivor.address);
  // 住んでいる市区町村（要望8）。municipalities.js の一覧から名前を引く（BigQueryには無い）
  if (survivor.municipality_code) {
    const municipalityValue = node("dd", null, "読み込んでいます…");
    basic.append(node("dt", null, "住んでいる市区町村"), municipalityValue);
    describeMunicipality(survivor.municipality_code)
      .then((label) => {
        municipalityValue.textContent = label || survivor.municipality_code;
      })
      .catch(() => {
        municipalityValue.textContent = survivor.municipality_code;
      });
  }
  addPair(basic, "今の段階", survivor.stage_label);
  addPair(
    basic,
    "今いる場所",
    survivor.location_detail
      ? `${survivor.location_type_label}（${survivor.location_detail}）`
      : survivor.location_type_label,
  );
  addPair(basic, "登録", formatDate(survivor.registered_at));
  if (!isSelf) {
    addPair(basic, "ご本人のアカウント", survivor.linked_to_self ? "つながっています" : "まだです");
  }
  // 家族などが代わりに登録した記録（使いやすさの改修の計画.md 10章）。家族の名前や連絡先は出さない
  if (survivor.registered_by_family) {
    addPair(
      basic,
      "登録した人",
      isSelf
        ? "ご家族などが代わりに登録しました。違うところはプロフィールで直してください"
        : "家族が登録した記録です",
    );
    if (!isSelf && survivor.proxy_consent_label) {
      addPair(basic, "ご本人の同意", survivor.proxy_consent_label);
    }
  }
  if (!isSelf && survivor.has_family) {
    addPair(basic, "ご家族", "家族がこの記録につながっています");
  }

  renderWatch(survivor);
  renderLocationForm(survivor, isProfessional || isSelf);
  renderHistory();
  renderAssignments();
  renderAssignForm(survivor, isProfessional);
  // F3 の健康・介護の情報と F2 の訪問・見守りの記録（f2.js が描く）
  onDetailRendered(detail);
}

function renderWatch(survivor) {
  const card = el("detail-watch-card");
  if (!survivor.watch) {
    card.hidden = true;
    return;
  }
  const list = el("detail-watch");
  clear(list);
  addPair(
    list,
    "最後の訪問",
    survivor.watch.last_visited_at
      ? `${formatDate(survivor.watch.last_visited_at)}（${survivor.watch.days_since_last_visit}日前）`
      : "まだありません",
  );
  addPair(list, "30日以内の記録", `${survivor.watch.visits_30d ?? 0}件`);
  addPair(list, "確認待ちの更新の案", `${survivor.watch.pending_proposals ?? 0}件`);
  addPair(list, "未対応の相談", `${survivor.watch.open_consultations ?? 0}件`);
  card.hidden = false;
}

function renderLocationForm(survivor, canChange) {
  const card = el("detail-location-card");
  card.hidden = !canChange;
  if (!canChange) return;
  fillFormChoices("location-stage", "location-type");
  el("location-stage").value = survivor.stage || "";
  el("location-type").value = survivor.location_type || "";
  el("location-detail").value = survivor.location_detail || "";
  // 施設を決めた直後だけ、施設の種類から入れた初期値を優先する（要確認43）。
  // 変えるのは専門職なので、**初期値として入れるだけ**で保存はしない
  if (pendingLocation) {
    if (pendingLocation.location_type) {
      el("location-type").value = pendingLocation.location_type;
    }
    if (pendingLocation.location_detail) {
      el("location-detail").value = pendingLocation.location_detail;
    }
    pendingLocation = null;
  }
  showFormError(el("form-location"), "");
}

function renderHistory() {
  const list = el("detail-history");
  clear(list);
  const history = detail.location_history || [];
  if (history.length === 0) {
    list.append(node("li", "note", "まだ変更がありません。"));
    return;
  }
  for (const item of history) {
    const text = item.prev_location_type
      ? `${formatDate(item.changed_at)}：${item.prev_location_type_label} → ${
          item.location_type_label
        }${item.location_detail ? `（${item.location_detail}）` : ""}`
      : `${formatDate(item.changed_at)}：${item.location_type_label}${
          item.location_detail ? `（${item.location_detail}）` : ""
        }（登録のとき）`;
    list.append(node("li", null, `${text}／${item.stage_label}`));
  }
}

function renderAssignments() {
  const list = el("detail-assignments");
  clear(list);
  const rows = detail.assignments || [];
  if (rows.length === 0) {
    // 組織につながっていないご本人には担当が付かない（追加修正②）
    const orgless = me?.role === ROLE_SURVIVOR && !me?.org_id;
    list.append(
      node(
        "li",
        "note",
        orgless
          ? "支援の組織につなぐと、その組織が担当の支援者を割り当てられます。"
          : "まだ担当の支援者がいません。",
      ),
    );
    return;
  }
  const isSelf = me?.role === ROLE_SURVIVOR;
  const isProfessional = me?.role === ROLE_PROFESSIONAL;

  for (const item of rows) {
    const li = node("li", "assignment");
    li.append(
      node(
        "span",
        null,
        // 利用者のIDへ落とさない（名前が引けない立場で、画面にIDが出てしまう）
        item.supporter_name || "（名前が読めませんでした）",
      ),
    );
    li.append(node("span", `badge badge-${item.status}`, STATUS_LABELS[item.status] || item.status));
    if (item.consent_type) {
      li.append(node("span", "row-note", CONSENT_LABELS[item.consent_type] || ""));
    }

    if (isSelf && item.status === "pending") {
      li.append(actionButton("この方を担当にする", "button button-primary", () =>
        actOnAssignment(item.assignment_id, "approve"),
      ));
    }
    if (item.status !== "removed" && (isSelf || isProfessional)) {
      li.append(actionButton("外す", "button", () =>
        actOnAssignment(item.assignment_id, "remove"),
      ));
    }
    list.append(li);
  }
}

function actionButton(label, className, handler) {
  const button = node("button", className, label);
  button.type = "button";
  button.addEventListener("click", () => withBusy(button, handler));
  return button;
}

async function actOnAssignment(assignmentId, action) {
  const survivorId = detail.survivor.survivor_id;
  const res = await apiFetch(
    `/api/survivors/${encodeURIComponent(survivorId)}/assignments/${encodeURIComponent(
      assignmentId,
    )}/${action}`,
    { method: "POST", body: JSON.stringify({}) },
  );
  if (!res.ok) {
    showNotice(await readError(res));
    return;
  }
  // 知らせは読み直したあとに出す（`openDetail` が先頭で知らせを消すため）
  await openDetail(survivorId);
  showNotice(action === "approve" ? "担当になりました。" : "担当を外しました。", "ok");
}

function renderAssignForm(survivor, isProfessional) {
  const card = el("detail-assign-card");
  card.hidden = !isProfessional;
  if (!isProfessional) return;
  const candidates = detail.supporter_candidates || [];
  fillSelect(
    el("assign-supporter"),
    candidates.map((item) => ({ value: item.user_id, label: item.display_name || item.user_id })),
    { placeholder: candidates.length ? "選んでください" : "割り当てられる支援者がいません" },
  );
  const consent = el("assign-consent");
  consent.checked = false;
  // 本人のアカウントがつながっているときは、本人が認めるまで有効にしない（F1）
  consent.disabled = survivor.linked_to_self;
  el("assign-consent-note").textContent = survivor.linked_to_self
    ? "ご本人のアカウントがつながっています。ご本人が承認すると担当になります。"
    : "ご本人のアカウントがまだないため、口頭で同意を得たことの記録が必要です。";
  showFormError(el("form-assign"), "");
}

async function submitAssign(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const survivorId = detail.survivor.survivor_id;
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(survivorId)}/assignments`, {
      method: "POST",
      body: JSON.stringify({
        supporter_user_id: el("assign-supporter").value,
        verbal_consent: el("assign-consent").checked,
      }),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    const body = await res.json();
    await openDetail(survivorId);
    showNotice(
      body.assignment.status === "active"
        ? "担当になりました。"
        : "割り当てました。ご本人が認めると担当になります。",
      "ok",
    );
  });
}

async function submitLocation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const survivorId = detail.survivor.survivor_id;
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(survivorId)}/location`, {
      method: "POST",
      body: JSON.stringify({
        stage: el("location-stage").value,
        location_type: el("location-type").value,
        location_detail: el("location-detail").value,
      }),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    await openDetail(survivorId);
    showNotice("居場所を変えました。前の居場所は履歴に残ります。", "ok");
  });
}

// --- イベントの登録 ---

export function bind({
  onHome: homeHandler,
  onDetailRendered: detailHandler,
  onListLoaded: listHandler,
}) {
  onHome = homeHandler;
  if (detailHandler) onDetailRendered = detailHandler;
  if (listHandler) onListLoaded = listHandler;

  for (const id of ["filter-stage", "filter-location-type", "filter-assignee"]) {
    el(id).addEventListener("change", () => {
      reloadList().catch((err) => showNotice(err.message));
    });
  }
  el("filter-deadline").addEventListener("change", () => renderListRows());
  el("survivors-reload").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () => reloadList()),
  );
  el("survivors-new").addEventListener("click", openNew);

  el("form-survivor-new").addEventListener("submit", (event) => {
    event.preventDefault();
    submitNew(false, event.currentTarget.querySelector("button[type=submit]"));
  });
  el("new-force").addEventListener("click", (event) => submitNew(true, event.currentTarget));

  el("issued-open").addEventListener("click", (event) => {
    const survivorId = event.currentTarget.dataset.survivorId;
    if (survivorId) openDetail(survivorId);
  });

  el("form-location").addEventListener("submit", submitLocation);
  el("form-assign").addEventListener("submit", submitAssign);

  for (const button of document.querySelectorAll(".back-to-survivors")) {
    button.addEventListener("click", () => {
      if (me?.role === ROLE_SURVIVOR) {
        onHome();
        return;
      }
      openList().catch((err) => showNotice(err.message));
    });
  }
  for (const button of document.querySelectorAll(".back-to-home")) {
    button.addEventListener("click", () => onHome());
  }
}

export { ROLE_PROFESSIONAL, ROLE_SURVIVOR, ROLE_VOLUNTEER };
