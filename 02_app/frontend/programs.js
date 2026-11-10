// 支援の一覧の画面（使いやすさの改修の計画.md 6-2・11-2）
//
// - **ご本人だけ**が使う。タブは2つ。
//   1. あなたの支援：記録した支援と申請の進み具合。期限の近い順に並べ、11章の絞り込みを使う
//   2. 支援制度の一覧：見られる制度のすべて（全国共通と、つながっている組織の制度）。名前で探せる
// - **AI を使わない**（Gemini の無料枠の回数を使わない）。AI で調べるのは「自分に合うものを調べる」
//   のボタンから、今の F6 の画面（f6.js）で行う。
// - 制度の中身・状況のラベル・次にすること・期限の知らせ・断り書きは **API が返すものだけ**を出す
//   （文言の正本は `services/supports.py`。画面に書き写すとずれる）。
// - 期限が3日以内（期限切れを含む）の未申請があれば、本人のホームの上に知らせを出す。
//   ホームの表示は待たずに、あとから読む（BigQuery の読みに1〜2秒かかるため）。
// - 制度の名前・支援の中身をコンソールに出さない（要件定義書11-3）。

import { apiFetch, readError } from "./auth.js";
import { DEADLINE_FILTERS, matchesDeadline, renderDeadlineChips } from "./f6.js";
import { icon } from "./icons.js";
import {
  clear,
  doneSkeleton,
  el,
  formatDate,
  node,
  setTileCount,
  showNotice,
  showScreen,
  showSkeleton,
  withBusy,
} from "./ui.js";

const ROLE_SURVIVOR = "survivor";
const STATUS_NOT_APPLIED = "not_applied";
// 手続きが終わった状況（期限の近い順の並びでは、いちばん下に回す）
const DONE_STATUSES = new Set(["received", "not_eligible"]);
// ホームに知らせを出す日数。API の `alert_days` を使い、無いときだけこの値にする
const DEFAULT_ALERT_DAYS = 3;
// 「あなたの支援」の絞り込みの初めの値（記録はすべて見せる）
const DEFAULT_FILTER = "all";
// ホームのタイルの数の鍵（app.js のタイルの countKey と同じ）
export const DUE_COUNT_KEY = "supports-due";

let me = null;
let openFinder = () => Promise.resolve();
let state = freshState();
// 古い読みの結果で、新しい画面を上書きしないための番号
let loadToken = 0;

function freshState() {
  return {
    programs: [],
    supports: [],
    filter: DEFAULT_FILTER,
    alertDays: DEFAULT_ALERT_DAYS,
  };
}

/** 立場を受け取る（ホームから渡される。ログアウトでは null）。 */
export function setMe(value) {
  me = value;
  state = freshState();
  loadToken += 1;
  el("home-deadline-banner").hidden = true;
  setTileCount(DUE_COUNT_KEY, 0);
}

function isSurvivor() {
  return me?.role === ROLE_SURVIVOR;
}

// --- 読み ---

async function fetchSupports(survivorId) {
  const res = await apiFetch(`/api/survivors/${encodeURIComponent(survivorId)}/supports`);
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

async function fetchPrograms() {
  const res = await apiFetch("/api/programs");
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

function takeSupports(body) {
  state.supports = body?.supports || [];
  const days = Number(body?.alert_days);
  state.alertDays = Number.isInteger(days) ? days : DEFAULT_ALERT_DAYS;
}

// --- 期限の近い未申請（ホームの知らせ。11-2） ---

function isOpen(row) {
  return row.status === STATUS_NOT_APPLIED;
}

/** 期限が近い（期限切れを含む）未申請か。 */
function isUrgent(row) {
  return (
    isOpen(row) &&
    typeof row.days_to_deadline === "number" &&
    row.days_to_deadline <= state.alertDays
  );
}

function renderBanner() {
  const banner = el("home-deadline-banner");
  const urgent = isSurvivor() ? state.supports.filter(isUrgent) : [];
  setTileCount(DUE_COUNT_KEY, urgent.length);
  if (urgent.length === 0) {
    banner.hidden = true;
    return;
  }
  const overdue = urgent.filter((row) => row.days_to_deadline < 0).length;
  let text = `申請の期限が${state.alertDays}日以内の支援が${urgent.length}件あります。早めに申し込んでください。`;
  if (overdue > 0) text += `期限を過ぎたものが${overdue}件あります。窓口に相談してください。`;
  el("home-deadline-banner-text").textContent = text;
  banner.hidden = false;
}

/** 本人のホームの上の知らせを読む（**待たない**。失敗しても知らせを出さないだけ）。 */
export async function loadHomeBanner() {
  if (!isSurvivor() || !me.survivor_id) {
    el("home-deadline-banner").hidden = true;
    return;
  }
  const token = loadToken;
  try {
    const body = await fetchSupports(me.survivor_id);
    if (token !== loadToken) return;
    takeSupports(body);
    renderBanner();
  } catch (err) {
    // ホームの表示は止めない。知らせが出ないだけにする（中身はログに出さない）
    el("home-deadline-banner").hidden = true;
  }
}

// --- 画面 ---

/** 支援の画面を開く（`tab` は "mine" か "all"。`filter` は期日の絞り込み）。 */
export async function openPrograms({ tab = "mine", filter = DEFAULT_FILTER } = {}) {
  showNotice("");
  if (!isSurvivor()) {
    showNotice("この画面はご本人だけが使えます");
    return;
  }
  if (!me.survivor_id) {
    showNotice("ご自分の記録が見つかりません。ログアウトしてお試しください");
    return;
  }
  state.filter = filter;
  el("programs-search").value = "";
  // 本人向けの画面は文字を大きくする（F6 と同じ。要件定義書F6）
  el("screen-programs").classList.add("large-text");
  selectTab(tab);
  showScreen("screen-programs");

  showSkeleton(el("programs-mine-list"));
  showSkeleton(el("programs-all-list"));
  el("programs-mine-count").textContent = "";
  el("programs-all-count").textContent = "";

  // 2つの読みは互いに関係がないので同時に走らせる
  loadToken += 1;
  const token = loadToken;
  const [supportsResult, programsResult] = await Promise.allSettled([
    fetchSupports(me.survivor_id),
    fetchPrograms(),
  ]);
  if (token !== loadToken) return;

  doneSkeleton(el("programs-mine-list"));
  doneSkeleton(el("programs-all-list"));
  const errors = [];
  if (supportsResult.status === "fulfilled") {
    takeSupports(supportsResult.value);
    el("programs-disclaimer").textContent = supportsResult.value.disclaimer || "";
  } else {
    errors.push(supportsResult.reason?.message);
  }
  if (programsResult.status === "fulfilled") {
    state.programs = programsResult.value.programs || [];
    el("programs-disclaimer").textContent = programsResult.value.disclaimer || "";
  } else {
    errors.push(programsResult.reason?.message);
  }
  renderMine();
  renderAll();
  renderBanner();
  if (errors.length) showNotice(errors.filter(Boolean)[0] || "読み込めませんでした");
}

function selectTab(tab) {
  const mine = tab !== "all";
  el("programs-tab-mine").checked = mine;
  el("programs-tab-all").checked = !mine;
  el("programs-mine-panel").hidden = !mine;
  el("programs-all-panel").hidden = mine;
}

// --- あなたの支援 ---

function daysOf(row) {
  return typeof row.days_to_deadline === "number" ? row.days_to_deadline : Infinity;
}

/** 期限の近い順。手続きが終わったもの（受給済み・対象外）は下に回す。 */
function compareSupports(a, b) {
  const doneA = DONE_STATUSES.has(a.status) ? 1 : 0;
  const doneB = DONE_STATUSES.has(b.status) ? 1 : 0;
  if (doneA !== doneB) return doneA - doneB;
  const daysA = daysOf(a);
  const daysB = daysOf(b);
  if (daysA === daysB) return 0;
  return daysA < daysB ? -1 : 1;
}

/** 選んだ絞り込みに入るか。「すべて」以外は、期限のある未申請だけを数える。 */
function matchesMine(row, key) {
  if (!key || key === "all") return true;
  if (!isOpen(row)) return false;
  return matchesDeadline(row.days_to_deadline, key);
}

function mineCounts(rows) {
  const counts = {};
  for (const { key } of DEADLINE_FILTERS) {
    counts[key] = rows.filter((row) => matchesMine(row, key)).length;
  }
  return counts;
}

function renderMine() {
  const rows = [...state.supports].sort(compareSupports);
  renderDeadlineChips(el("programs-mine-filters"), {
    selected: state.filter,
    counts: mineCounts(rows),
    onSelect: (key) => {
      state.filter = key;
      renderMine();
    },
  });
  el("programs-mine-filters").hidden = rows.length === 0;

  const list = el("programs-mine-list");
  clear(list);
  if (rows.length === 0) {
    el("programs-mine-count").textContent = "";
    list.append(
      emptyState("まだ記録した支援はありません。「支援制度の一覧」から記録するか、「自分に合うものを調べる」で調べられます。"),
    );
    return;
  }
  const shown = rows.filter((row) => matchesMine(row, state.filter));
  el("programs-mine-count").textContent =
    shown.length === 0 ? "この絞り込みに当てはまる支援はありません。" : `${shown.length}件`;
  for (const row of shown) list.append(renderSupport(row));
}

function renderSupport(row) {
  const li = node("li", "card support");
  li.append(node("h3", "row-title", row.program_name || "（名前なし）"));

  const marks = node("div", "row-marks");
  // 状況のラベルは API が返すもの
  marks.append(node("span", statusClass(row.status), row.status_label || ""));
  if (isUrgent(row)) marks.append(node("span", "badge badge-high", "期限が近い"));
  li.append(marks);

  // 期限の知らせと「次にすること」は API が返す文言をそのまま出す
  if (row.deadline_notice) li.append(node("p", "note", row.deadline_notice));
  if (row.next_action) li.append(node("p", null, `次にすること：${row.next_action}`));
  li.append(
    node(
      "p",
      "row-note",
      `申請の期限：${row.deadline ? formatDate(row.deadline) : "まだ決まっていません"}`,
    ),
  );

  const more = node("details", "program-more");
  more.append(node("summary", null, "制度のくわしい内容"));
  more.append(programPairs(row));
  li.append(more);
  return li;
}

function statusClass(status) {
  if (status === "not_applied") return "badge badge-medium";
  if (status === "received") return "badge badge-low";
  return "badge badge-note";
}

// --- 支援制度の一覧 ---

/** 探すときの比べ方（全角・半角と、大文字・小文字と、空白の違いを無くす）。 */
function normalize(text) {
  return String(text || "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/\s+/g, "");
}

function renderAll() {
  const query = normalize(el("programs-search").value);
  const recorded = new Set(state.supports.map((row) => row.program_id));
  const items = state.programs.filter(
    (item) => !query || normalize(item.program_name).includes(query),
  );
  const list = el("programs-all-list");
  clear(list);
  if (state.programs.length === 0) {
    el("programs-all-count").textContent = "";
    list.append(emptyState("見られる支援制度はまだありません。"));
    return;
  }
  el("programs-all-count").textContent =
    items.length === 0
      ? "当てはまる制度はありません。名前の一部で探してみてください。"
      : `${items.length}件（全部で${state.programs.length}件）`;
  for (const item of items) list.append(renderProgram(item, recorded.has(item.program_id)));
}

function renderProgram(item, isRecorded) {
  const li = node("li", "card support");
  li.append(node("h3", "row-title", item.program_name || "（名前なし）"));
  const marks = node("div", "row-marks");
  // 組織に関係なく使える国の制度（追加修正②）
  if (item.nationwide) marks.append(node("span", "badge badge-note", "全国共通"));
  if (isRecorded) marks.append(node("span", "badge badge-low", "記録してあります"));
  if (marks.childElementCount) li.append(marks);
  li.append(programPairs(item));

  const actions = node("div", "actions");
  const button = node("button", "button", isRecorded ? "記録してあります" : "これを記録する");
  button.type = "button";
  button.disabled = isRecorded;
  if (!isRecorded) {
    button.addEventListener("click", () =>
      recordProgram(item, button).catch((err) => showNotice(err.message)),
    );
  }
  actions.append(button);
  li.append(actions);
  return li;
}

/** 制度の中身（API が返す値をそのまま出す。AI に書かせていない）。 */
function programPairs(item) {
  const box = node("dl", "pairs");
  const pairs = [
    ["対象の人", item.conditions],
    ["受けられる内容", item.benefit],
    ["申し込み先", item.apply_to],
    ["期限の決まり", item.deadline_rule],
    ["必要な書類", item.documents],
  ];
  for (const [label, value] of pairs) {
    box.append(node("dt", null, label));
    box.append(node("dd", null, value || "—"));
  }
  // 出典は空白で区切って複数入っていることがある（seed.py の SOURCE_URLS）
  const urls = String(item.source_url || "")
    .split(/\s+/)
    .filter((url) => /^https:\/\//.test(url));
  if (urls.length) {
    box.append(node("dt", null, "出典"));
    const dd = node("dd");
    for (const url of urls) {
      const link = node("a", "source-link", url);
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      dd.append(link);
    }
    box.append(dd);
  }
  return box;
}

/** 「これを記録する」：申請前（未申請）として記録する。被害の程度などは聞かない。 */
async function recordProgram(item, button) {
  await withBusy(button, async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(me.survivor_id)}/supports`, {
      method: "POST",
      body: JSON.stringify({ from_list: true, items: [{ program_id: item.program_id }] }),
    });
    if (!res.ok) {
      showNotice(await readError(res));
      return;
    }
    const saved = await res.json();
    const merged = new Map(state.supports.map((row) => [row.support_id, row]));
    for (const row of saved.supports || []) merged.set(row.support_id, row);
    state.supports = [...merged.values()];
    renderAll();
    renderMine();
    renderBanner();
    showNotice("支援を記録しました。「あなたの支援」に未申請として出ます。", "ok");
  });
}

function emptyState(text) {
  const li = node("li", "empty-state");
  li.append(icon("gift"), node("span", null, text));
  return li;
}

// --- イベントの登録 ---

export function bind({ openFinder: finderHandler } = {}) {
  if (finderHandler) openFinder = finderHandler;
  el("programs-tab-mine").addEventListener("change", () => selectTab("mine"));
  el("programs-tab-all").addEventListener("change", () => selectTab("all"));
  el("programs-search").addEventListener("input", () => renderAll());
  el("programs-find").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () => openFinder()).catch((err) => showNotice(err.message)),
  );
  el("home-deadline-banner-open").addEventListener("click", () =>
    openPrograms({ tab: "mine", filter: "3" }).catch((err) => showNotice(err.message)),
  );
}
