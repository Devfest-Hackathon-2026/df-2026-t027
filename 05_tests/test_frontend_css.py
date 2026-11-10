"""画面の見た目の決まりのうち、**ブラウザで踏んだ崩れ**を固定するテスト。

自動テストでは幅を測れないので、踏んだ原因そのもの（CSS の書き方）を固定する。
"""

from __future__ import annotations

import re
from pathlib import Path

STYLE = Path(__file__).resolve().parents[1] / "02_app" / "frontend" / "style.css"


def rule(selector: str) -> str:
    """`selector { … }` の中身を取り出す。"""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", STYLE.read_text(encoding="utf-8"))
    assert match, f"{selector} の指定が見つかりません"
    return match.group(1)


def test_値の欄は折り返せないほど長い文字列でも折り返す():
    """`.pairs` の値（出典のURLなど）が画面からはみ出さないようにする。

    2026-11-09 に 360px 幅で踏んだ：支援制度の「出典」の長いURLは途中で折り返せないため、
    `grid-template-columns: 7rem 1fr` の `1fr` が URL の幅まで広がり、
    **ページ全体の scrollWidth が 345px → 728px** になった（実測）。

    `minmax(0, 1fr)` にしても 728px のままで直らない（文字が枠からはみ出して描かれる）。
    **`overflow-wrap: anywhere`** だと、どこでも折り返せるようになり
    （＝最小の幅が小さくなり）345px に収まった。`break-word` は最小の幅を変えないため使わない。
    """
    assert "overflow-wrap: anywhere" in rule(".pairs dd")


def test_画面の幅で中身の幅を3つに分ける():
    """2026-11-10、PCの実機で中身が480pxのまま左右が大きく空いた（追加修正①）。

    そのあと720pxに広げたが、PCでは左右に何もない帯が残り、一覧も1列のままだった。
    スマートフォン・タブレット・PCで分ける（画面の改修の計画.md 1章）。
    """
    css = STYLE.read_text(encoding="utf-8")
    assert "--page-max: 480px" in css
    assert re.search(r"@media \(min-width: 768px\)\s*\{\s*:root\s*\{\s*--page-max: 100%;", css)
    assert re.search(r"@media \(min-width: 1024px\)\s*\{\s*:root\s*\{\s*--page-max: 1200px;", css)
    assert "max-width: var(--page-max)" in rule(".page")


def test_タブのバーは左右の余白で真ん中に集めない():
    """左右の余白で真ん中に集めると、広い画面で左右に何もない帯ができた（画面の改修の計画.md 1章）。"""
    assert "max-width: var(--page-max)" in rule(".appbar-inner")
    tabbar = rule(".tabbar")
    assert "padding-left" not in tabbar
    assert "padding-right" not in tabbar


def test_上のバーのボタンは上の余白を消す():
    """`.link-button` の `margin-top: 8px` で、ログアウトがアプリ名より下にずれていた（追加修正①）。"""
    assert "margin: 0" in rule(".appbar .link-button")
