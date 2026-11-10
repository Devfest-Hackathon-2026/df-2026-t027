// 都道府県・市区町村の選択（要望8。要件定義書には無い追加の一覧）
//
// - 一覧は /data/municipalities.json を読む（BigQueryもAPIも使わない。
//   04_scripts/build_municipalities.py が作ったファイル）。読みは1回だけ（結果を持っておく）。
// - 市区町村は、選んだ都道府県の中だけを出す。文字を入れると、名前・読みの両方で絞り込める
//   （例：「わじま」でも「輪島」でも輪島市が出る）。
// - 都道府県も、文字で絞り込める（追加修正⑤。例：「いしかわ」「石川」）。絞り込んで
//   1件になったら、その都道府県を選んだことにする（続けて市区町村を選べるように）。
// - 政令指定都市の区（札幌市中央区など）には分けない。市の単位までにする
//   （追加改修の計画.md 11章の要確認F）。

let dataPromise = null;

/** 一覧を読む（1回だけ。2回目以降は同じ結果を返す）。 */
export function loadMunicipalities() {
  if (!dataPromise) {
    dataPromise = fetch("/data/municipalities.json").then((res) => {
      if (!res.ok) throw new Error("市区町村の一覧を読み込めませんでした");
      return res.json();
    });
  }
  return dataPromise;
}

/**
 * 都道府県・市区町村の2段の選択欄を結び付ける。
 *
 * @param {Object} opts
 * @param {HTMLSelectElement} opts.prefectureSelect
 * @param {HTMLSelectElement} opts.citySelect
 * @param {HTMLInputElement} [opts.prefectureFilterInput] 都道府県を文字で絞り込む欄（任意。追加修正⑤）
 * @param {HTMLInputElement} [opts.filterInput] 市区町村を文字で絞り込む欄（任意）
 * @param {(code: string) => void} [opts.onCityChange] 市区町村を選び直したときに呼ぶ
 * @returns {Promise<{ setCode: (code: string) => void }>}
 *   あとから選択を1件に合わせたいとき用（プロフィールなどで前の値を出す）。
 */
export async function bindMunicipalityPicker({
  prefectureSelect,
  citySelect,
  prefectureFilterInput,
  filterInput,
  onCityChange,
}) {
  const data = await loadMunicipalities();

  /** 名前か読みに、入れた文字が含まれるか（都道府県・市区町村で共通）。 */
  function matches(item, text) {
    if (!text) return true;
    return item.name.includes(text) || (item.kana || "").includes(text);
  }

  /** 都道府県の選択肢を、絞り込みの文字に合わせて作り直す。選んでいた値は残せるなら残す。 */
  function fillPrefectures() {
    const filterText = (prefectureFilterInput?.value || "").trim();
    const prefectures = data.prefectures.filter((pref) => matches(pref, filterText));
    const previous = prefectureSelect.value;
    prefectureSelect.innerHTML = "";
    const prefPlaceholder = document.createElement("option");
    prefPlaceholder.value = "";
    prefPlaceholder.textContent = prefectures.length ? "選んでください" : "見つかりません";
    prefectureSelect.append(prefPlaceholder);
    for (const pref of prefectures) {
      const option = document.createElement("option");
      option.value = pref.code;
      option.textContent = pref.name;
      prefectureSelect.append(option);
    }
    if (filterText && prefectures.length === 1) {
      prefectureSelect.value = prefectures[0].code;
    } else if (prefectures.some((pref) => pref.code === previous)) {
      prefectureSelect.value = previous;
    }
    return prefectureSelect.value !== previous;
  }

  fillPrefectures();

  function citiesInSelectedPrefecture() {
    const prefCode = prefectureSelect.value;
    if (!prefCode) return [];
    return data.municipalities.filter((city) => city.pref_code === prefCode);
  }

  function fillCities() {
    const filterText = (filterInput?.value || "").trim();
    const cities = citiesInSelectedPrefecture().filter((city) => matches(city, filterText));
    citySelect.innerHTML = "";
    const cityPlaceholder = document.createElement("option");
    cityPlaceholder.value = "";
    cityPlaceholder.textContent = prefectureSelect.value
      ? "選んでください"
      : "先に都道府県を選んでください";
    citySelect.append(cityPlaceholder);
    for (const city of cities) {
      const option = document.createElement("option");
      option.value = city.code;
      option.textContent = city.name;
      citySelect.append(option);
    }
  }

  function onPrefectureChanged() {
    if (filterInput) filterInput.value = "";
    fillCities();
    if (onCityChange) onCityChange("");
  }

  prefectureSelect.addEventListener("change", onPrefectureChanged);
  if (prefectureFilterInput) {
    prefectureFilterInput.addEventListener("input", () => {
      // 絞り込みで選んでいた都道府県が変わったときだけ、市区町村を作り直す
      if (fillPrefectures()) onPrefectureChanged();
    });
  }
  if (filterInput) filterInput.addEventListener("input", fillCities);
  if (onCityChange) citySelect.addEventListener("change", () => onCityChange(citySelect.value));

  fillCities();

  return {
    /** 選択を、指定した団体コードに合わせる（前の値を出すときに使う）。 */
    setCode(code) {
      if (!code) return;
      const city = data.municipalities.find((item) => item.code === code);
      if (!city) return;
      if (prefectureFilterInput) {
        prefectureFilterInput.value = "";
        fillPrefectures();
      }
      prefectureSelect.value = city.pref_code;
      if (filterInput) filterInput.value = "";
      fillCities();
      citySelect.value = code;
    },
  };
}

/** 団体コードから「都道府県 市区町村」の名前を引く（詳細・プロフィールの表示に使う）。 */
export async function describeMunicipality(code) {
  if (!code) return "";
  const data = await loadMunicipalities();
  const city = data.municipalities.find((item) => item.code === code);
  if (!city) return "";
  const pref = data.prefectures.find((item) => item.code === city.pref_code);
  return pref ? `${pref.name} ${city.name}` : city.name;
}
