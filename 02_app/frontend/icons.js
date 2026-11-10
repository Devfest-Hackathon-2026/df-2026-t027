// 画面のアイコン（使いやすさの改修の計画.md 2章・7章）
//
// - 外から読み込まない。形はこのファイルの SVG の線だけで描く（このリポジトリで描いたもの）。
// - 24×24 の枠に、線の太さ 2 で描く。色は文字の色（currentColor）に合わせる。
// - 飾りのアイコンは読み上げない（aria-hidden）。アイコンだけのボタンは、ボタンの側に
//   aria-label を付ける。
// - 静的な画面（index.html）は `<span data-icon="名前"></span>` を置き、`applyIcons()` で描く。

const SVG_NS = "http://www.w3.org/2000/svg";

// 名前 → SVG の中身（path・circle・rect）。
const SHAPES = {
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/>',
  users:
    '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/>' +
    '<circle cx="17" cy="9" r="2.5"/><path d="M17 14c2.5 0 4.5 2 4.5 5"/>',
  "log-out":
    '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/>',
  home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
  list:
    '<path d="M9 6h12M9 12h12M9 18h12"/><circle cx="4.5" cy="6" r="1"/>' +
    '<circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z"/>',
  file:
    '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>' +
    '<path d="M8 13h8M8 17h5"/>',
  gift:
    '<rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13"/><path d="M5 12v9h14v-9"/>' +
    '<path d="M12 8C10 4 6 4 6 6.5S10 8 12 8c2 0 6 1 6-1.5S14 4 12 8z"/>',
  building:
    '<rect x="4" y="3" width="16" height="18" rx="1"/>' +
    '<path d="M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2M10 21v-3h4v3"/>',
  chart: '<path d="M4 20V11M10 20V5M16 20v-7M2 20h20"/>',
  clipboard:
    '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1"/>' +
    '<path d="M9 10h6M9 14h6M9 18h3"/>',
  mic:
    '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/>',
  "mic-off":
    '<path d="M3 3l18 18"/><path d="M9 9v2a3 3 0 0 0 5 2.2M15 11V6a3 3 0 0 0-5.7-1.3"/>' +
    '<path d="M5 11a7 7 0 0 0 11 5.7M19 11a7 7 0 0 1-.6 2.8"/><path d="M12 18v3"/>',
  "map-pin":
    '<path d="M12 21s-7-6.4-7-12a7 7 0 0 1 14 0c0 5.6-7 12-7 12z"/><circle cx="12" cy="9" r="2.5"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M14 9l2 2"/>',
  alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
  inbox:
    '<path d="M3 13l3-8h12l3 8"/><path d="M3 13v6a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-6h-5l-1 2h-6l-1-2z"/>',
  "arrow-left": '<path d="M19 12H5M11 18l-6-6 6-6"/>',
  heart:
    '<path d="M12 20s-8-4.6-8-10.5A4.5 4.5 0 0 1 12 7a4.5 4.5 0 0 1 8 2.5C20 15.4 12 20 12 20z"/>',
  link:
    '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/>' +
    '<path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  text: '<path d="M4 7V5h16v2M9 19h6M12 5v14"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
};

export const ICON_NAMES = Object.keys(SHAPES);

/** アイコンの SVG を1つ作る（飾りなので読み上げない）。 */
export function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("class", className);
  // 中身はこのファイルの固定の文字列だけ（利用者の入力は入らない）
  svg.innerHTML = SHAPES[name] || "";
  return svg;
}

/** `data-icon` の付いた要素に、アイコンを描く（index.html の静的な部分）。 */
export function applyIcons(root = document) {
  for (const slot of root.querySelectorAll("[data-icon]")) {
    if (slot.firstElementChild) continue;
    slot.append(icon(slot.dataset.icon));
  }
}
