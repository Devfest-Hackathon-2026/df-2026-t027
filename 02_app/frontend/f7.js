// F7 組織全体の状況の画面（開発手順書 Step 11）
//
// - **使えるのは専門職だけ**（要件定義書10-5の表の「専」）。入口も専門職のホームにだけ出す。
//   本人と一般の支援者には `GET /api/dashboard` が 403 を返す。
// - **画面は数を作らない。** 人数の正本は mart だけで、API が足し合わせた数をそのまま出す
//   （2か所で数えると、食い違ったときにどちらが正しいか決められない。要件定義書1055行）。
// - 区分の呼び方（段階・居場所の種類・度合い）と断り書きも **API が返すものだけ**を出す
//   （文言の正本は `services/dashboard.py`。画面に書き写すとずれる。Step 8・10 と同じ形）。
// - mart は1時間ごとにしか変わらないため、**いつ時点の集計か**を必ず先頭に出す。
//   これが無いと、入れたばかりの記録が出ていないことを不具合と読み違える。
// - 人数だけを扱い、氏名も記録の本文も受け取らない。

import { apiFetch, readError } from "./auth.js";
import { clear, el, formatDateTime, node, showNotice, showScreen, withBusy } from "./ui.js";

/** 合計の4つの数を出す（`mart_org_status` の人数の列と同じ並び）。 */
function renderTotal(total) {
  const counts = total || {};
  el("dashboard-total-survivor-count").textContent = people(counts.survivor_count);
  el("dashboard-total-with-open-consultation").textContent = people(counts.with_open_consultation);
  el("dashboard-total-with-unapplied-support").textContent = people(counts.with_unapplied_support);
  el("dashboard-total-with-pending-proposal").textContent = people(counts.with_pending_proposal);
}

function people(value) {
  return `${Number.isFinite(value) ? value : 0}人`;
}

/** 区分ごとの人数を1つの一覧に出す（区分の呼び方は API の `label` をそのまま使う）。 */
function renderGroup(list, items) {
  clear(list);
  if (!items || items.length === 0) {
    list.append(node("li", "note", "まだ記録がありません。"));
    return;
  }
  for (const item of items) {
    const row = node("li", null);
    row.append(node("span", "row-title", item.label));
    row.append(node("span", "row-note", people(item.survivor_count)));
    list.append(row);
  }
}

function render(body) {
  // 集計が1行も無いときは `as_of` が null になる（まだ mart を作っていない組織）
  el("dashboard-as-of").textContent = body.as_of
    ? `${formatDateTime(body.as_of)} 時点の集計です。`
    : "集計はまだ作られていません。";
  el("dashboard-note").textContent = body.note || "";
  renderTotal(body.total);
  renderGroup(el("dashboard-by-stage"), body.by_stage);
  renderGroup(el("dashboard-by-location-type"), body.by_location_type);
  renderGroup(el("dashboard-by-risk-level"), body.by_risk_level);
}

/** 組織全体の状況を読んで画面を出す（読みは mart 1回だけ）。 */
export async function openDashboard() {
  showNotice("");
  const res = await apiFetch("/api/dashboard");
  if (!res.ok) throw new Error(await readError(res));
  render(await res.json());
  showScreen("screen-dashboard");
}

export function bind() {
  el("dashboard-reload").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () =>
      openDashboard().catch((err) => showNotice(err.message)),
    ),
  );
}
