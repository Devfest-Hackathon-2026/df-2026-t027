// ご本人が選ぶ「支援の組織」の欄（追加修正⑦。登録の画面とプロフィールの画面で使う）
//
// - 住んでいる市区町村を受け持つ組織を上に出し、全国のすべての組織から名前で探して選べる。
// - 空（つながない）でもよい。つながなくても、ご自分の記録・相談・受けられる支援は使える
//   （追加修正②）。選んだ組織が実在するかはサーバーが確かめる（無ければ 400）。
// - 組織は誰でも作れるため、架空の組織が並ぶおそれがある（追加改修の計画.md 3-4）。
//   選ぶ前に、その組織の専門職に記録（健康・介護の情報を含む）が見えることを知らせる。
// - 組織の一覧は /api/organizations を1回だけ読む（参加コードのハッシュは返らない）。

import { apiFetch, readError } from "./auth.js";

const NONE_LABEL = "つながない（あとから選べます）";

/** 組織を選んだときに出す知らせ（何が見えるようになるか）。 */
export const ORG_VISIBILITY_NOTICE =
  "選んだ組織の専門職（保健師・相談員など）に、あなたの記録（健康・介護の情報を含む）が見えるようになります。";

/** 組織を選んでいないときに出す知らせ。 */
export const NO_ORG_NOTICE =
  "つながなくても、ご自分の記録・相談・受けられる支援は使えます。" +
  "組織につながっていないときの相談は、組織を問わず支援者が見られます。";

let allPromise = null;

/** 全国の組織を読む（1回だけ。失敗したときは次に読み直す）。 */
function loadAll() {
  if (!allPromise) {
    allPromise = apiFetch("/api/organizations").then(async (res) => {
      if (!res.ok) {
        allPromise = null;
        throw new Error(await readError(res));
      }
      const rows = (await res.json()).organizations || [];
      return [...rows].sort((a, b) =>
        String(a.org_name || "").localeCompare(String(b.org_name || ""), "ja"),
      );
    });
  }
  return allPromise;
}

/** その市区町村を受け持つ組織を読む（読めなくても、全国の一覧からは選べる）。 */
async function loadLocal(municipalityCode) {
  if (!municipalityCode) return [];
  const res = await apiFetch(
    `/api/organizations/by-municipality?municipality_code=${encodeURIComponent(municipalityCode)}`,
  );
  if (!res.ok) return [];
  return (await res.json()).organizations || [];
}

function orgLabel(org) {
  const name = org.org_name || org.org_id || "";
  return org.prefecture ? `${name}（${org.prefecture}）` : name;
}

function option(value, label) {
  const item = document.createElement("option");
  item.value = value;
  item.textContent = label;
  return item;
}

/**
 * 「支援の組織」の欄を作る。
 *
 * @param {Object} opts
 * @param {HTMLSelectElement} opts.select 組織を選ぶ欄（required は付けない）
 * @param {HTMLInputElement} [opts.filterInput] 名前で探す欄
 * @param {HTMLElement} [opts.note] 選んだときの知らせを出す場所
 */
export function createOrganizationPicker({ select, filterInput, note }) {
  let all = [];
  let local = [];
  let chosen = "";

  function updateNote() {
    if (!note) return;
    note.textContent = select.value ? ORG_VISIBILITY_NOTICE : NO_ORG_NOTICE;
  }

  function render() {
    const text = (filterInput?.value || "").trim();
    const matches = (org) =>
      !text ||
      String(org.org_name || "").includes(text) ||
      String(org.prefecture || "").includes(text);

    select.innerHTML = "";
    select.append(option("", NONE_LABEL));

    const localIds = new Set(local.map((org) => org.org_id));
    const nearby = document.createElement("optgroup");
    nearby.label = "お住まいの市区町村の組織";
    for (const org of local.filter(matches)) nearby.append(option(org.org_id, orgLabel(org)));
    if (nearby.childElementCount > 0) select.append(nearby);

    const others = document.createElement("optgroup");
    others.label = "ほかの組織（名前で探せます）";
    for (const org of all.filter((item) => !localIds.has(item.org_id)).filter(matches)) {
      others.append(option(org.org_id, orgLabel(org)));
    }
    if (others.childElementCount > 0) select.append(others);

    // 絞り込みで選んでいた組織が消えたら「つながない」に戻る（見えない値を送らない）
    const stillThere = [...select.options].some((item) => item.value === chosen);
    select.value = stillThere ? chosen : "";
    chosen = select.value;
    updateNote();
  }

  select.addEventListener("change", () => {
    chosen = select.value;
    updateNote();
  });
  if (filterInput) filterInput.addEventListener("input", render);

  return {
    /** 全国の組織と、その市区町村の組織を読んで欄を作る。 */
    async load(municipalityCode = "") {
      [all, local] = await Promise.all([loadAll(), loadLocal(municipalityCode)]);
      render();
    },
    /** 住んでいる市区町村を選び直したとき（受け持つ組織を上に出し直す）。 */
    async setMunicipality(municipalityCode) {
      local = await loadLocal(municipalityCode);
      render();
    },
    /** 選んでいる組織を合わせる（プロフィールで、いまの組織を出すとき）。 */
    setValue(orgId) {
      chosen = orgId || "";
      if (filterInput) filterInput.value = "";
      render();
    },
    /** 選んでいる組織のID（つながないときは空）。 */
    value() {
      return select.value || "";
    },
    /** 選んでいる組織の名前（確かめの文に使う）。 */
    label() {
      const found = all.find((org) => org.org_id === select.value);
      return found ? found.org_name || "" : "";
    },
  };
}
