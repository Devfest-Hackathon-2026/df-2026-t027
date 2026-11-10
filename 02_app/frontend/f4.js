// F4 悪化の兆しと訪問の順番の画面（開発手順書 Step 7-2・7-4）
//
// - ホームの「対応が必要な人」と、詳細の「悪化の兆し」の表示・度合いの変更をここで持つ。
//   詳細の画面そのものは f1.js が描き、描き終わってからこのファイルが呼ばれる（f2.js と同じ形）。
// - **度合いは本人の画面に出さない。** 本人向けの応答には `risk` が入らないので、
//   入っていなければカードごと隠す（画面側で立場を見て隠すのではなく、無いものを隠す）。
// - 「医療の診断ではない」という断り書きは、APIが返す文言をそのまま出す
//   （文言の正本は `services/assessments.py` の DISCLAIMER。画面に書き写すとずれる）。
// - 度合いの選択肢も /api/public-config の choices から作る（画面に値を書かない）。

import { apiFetch, getConfig, readError } from "./auth.js";
import {
  addPair,
  clear,
  el,
  fillSelect,
  formatDateTime,
  node,
  setTileCount,
  showFormError,
  showNotice,
  showSkeleton,
  withBusy,
} from "./ui.js";

const ROLE_PROFESSIONAL = "professional";
const ROLE_VOLUNTEER = "volunteer";
// 被災者の一覧を読める立場（専門職と一般の支援者。本人と施設の職員は読まない）
const SUPPORTER_ROLES = [ROLE_PROFESSIONAL, ROLE_VOLUNTEER];

// 根拠の出どころ（`services/assessments.py` の KIND_LABELS と同じ）。
const KIND_LABELS = { visit: "訪問・見守りの記録", consultation: "ご本人からの相談" };

let me = null;
let detail = null;
let openDetail = () => Promise.resolve();
// 読んだ一覧の応答を渡す先（F6 の「期限が近い未申請」。読みを増やさないため）
let onLoaded = () => {};

/** 立場を受け取る（ホームから渡される）。 */
export function setMe(value) {
  me = value;
}

// --- ホームの「対応が必要な人」 ---

/** ホームに「対応が必要な人」を出す（一覧と同じ応答から作るので、読みは増えない）。
 *
 *  ホームの表示を止めないように、呼ぶ側は待たずに呼んでよい（失敗しても知らせるだけ）。 */
export async function loadAttention() {
  const card = el("home-attention-card");
  // 組織がない支援者は記録に触れない（要望1）。被災者の一覧そのものを読まない。
  // 施設の職員も記録に触れない（使いやすさの改修の計画.md 9章）。読めるのは支援者だけ
  if (!me || !SUPPORTER_ROLES.includes(me.role) || !me.org_id) {
    card.hidden = true;
    onLoaded(null);
    return;
  }
  el("home-attention-count").textContent = "読み込んでいます…";
  showSkeleton(el("home-attention"), 2);
  card.hidden = false;
  const res = await apiFetch("/api/survivors");
  if (!res.ok) {
    el("home-attention-count").textContent = "読み込めませんでした。読み直してください";
    clear(el("home-attention"));
    onLoaded(null);
    return;
  }
  const body = await res.json();
  renderAttention(body);
  // 同じ応答から F6 の知らせも作る（ホーム専用のAPIを増やさない）
  onLoaded(body);
}

/** 一覧の応答（`attention`）からホームのカードを描く。 */
export function renderAttention(body) {
  const attention = body?.attention;
  const card = el("home-attention-card");
  if (!attention) {
    card.hidden = true;
    return;
  }
  const list = el("home-attention");
  clear(list);
  // ホームの「被災者の一覧」のタイルに、対応が要る人の数を出す（使いやすさの改修の計画.md 4-2の5）
  setTileCount("attention", attention.count);
  el("home-attention-count").textContent =
    attention.count === 0
      ? "いまのところ、対応が必要な方はいません。"
      : `${attention.count}人`;
  for (const item of attention.survivors || []) {
    const li = node("li", "attention");
    const button = node("button", "link-button", `${item.full_name}（${item.risk_level_label}）`);
    button.type = "button";
    button.addEventListener("click", () => {
      openDetail(item.survivor_id).catch((err) => showNotice(err.message));
    });
    li.append(button);
    li.append(
      node(
        "span",
        "row-note",
        item.never_visited
          ? "まだ訪問の記録がありません"
          : `最後の訪問から${item.days_since_last_visit}日`,
      ),
    );
    if (item.next_action) li.append(node("span", "row-note", `次にすること：${item.next_action}`));
    list.append(li);
  }
  el("home-attention-note").textContent = attention.disclaimer || "";
  card.hidden = false;
}

// --- 詳細の「悪化の兆し」 ---

/** 詳細の画面の「悪化の兆し」を描く（f1.js が詳細を描いたあとに呼ばれる）。 */
export function renderDetailRisk(detailBody) {
  detail = detailBody;
  const card = el("detail-risk-card");
  const risk = detailBody?.survivor?.risk;
  if (!risk) {
    // 本人向けの応答には入らない（要件定義書F4の最後・10-3）
    card.hidden = true;
    return;
  }

  const pairs = el("detail-risk");
  clear(pairs);
  addPair(pairs, "度合い", risk.level_label || "まだ判断がありません");
  addPair(pairs, "次に必要な対応", risk.next_action);
  addPair(pairs, "判断", risk.source_label);
  addPair(pairs, "判断した日時", formatDateTime(risk.assessed_at));

  const evidence = el("detail-risk-evidence");
  clear(evidence);
  const items = risk.evidence || [];
  if (items.length === 0) {
    evidence.append(node("li", "note", "根拠はまだありません。"));
  } else {
    for (const item of items) {
      const kind = KIND_LABELS[item.source_kind] || "記録";
      evidence.append(node("li", null, `「${item.quote}」（${kind}）`));
    }
  }
  el("detail-risk-note").textContent = risk.disclaimer || "";

  renderRiskForm(risk);
  card.hidden = false;
}

function renderRiskForm(risk) {
  const form = el("form-risk");
  const canChange = me?.role === ROLE_PROFESSIONAL;
  form.hidden = !canChange;
  if (!canChange) return;
  const levels = getConfig()?.choices?.risk_levels || [];
  fillSelect(el("risk-level"), levels, { placeholder: "選んでください" });
  el("risk-level").value = risk.level || "";
  el("risk-reason").value = "";
  showFormError(form, "");
}

async function submitRisk(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showFormError(form, "");
  const survivorId = detail?.survivor?.survivor_id;
  if (!survivorId) {
    showFormError(form, "記録が読み取れませんでした。一覧から開き直してください");
    return;
  }
  await withBusy(form.querySelector("button[type=submit]"), async () => {
    const res = await apiFetch(`/api/survivors/${encodeURIComponent(survivorId)}/assessment`, {
      method: "PUT",
      body: JSON.stringify({
        level: el("risk-level").value,
        reason: el("risk-reason").value,
      }),
    });
    if (!res.ok) {
      showFormError(form, await readError(res));
      return;
    }
    // 読み直したあとに知らせを出す（`openDetail` が先頭で知らせを消すため）
    await openDetail(survivorId);
    showNotice("度合いを変えました。前の判断も記録として残ります。", "ok");
  });
}

// --- イベントの登録 ---

export function bind({ openDetail: openDetailHandler, onLoaded: loadedHandler }) {
  if (openDetailHandler) openDetail = openDetailHandler;
  if (loadedHandler) onLoaded = loadedHandler;
  el("home-attention-reload").addEventListener("click", (event) =>
    withBusy(event.currentTarget, () =>
      loadAttention().catch((err) => showNotice(err.message)),
    ),
  );
  el("form-risk").addEventListener("submit", submitRisk);
}
