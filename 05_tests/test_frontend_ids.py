"""画面のIDの取り違えを拾うテスト（開発手順書 Step 5。実際に踏んだ不具合から足した）。

JavaScript から `el("…")` で取り出すIDが `index.html` に無いと、`null` に対して処理をして
その場で止まる。**画面は切り替わらず、エラーも出ない**ため、気づくのが遅れる。

実際に `fillFormChoices("location")` が `location-location-type`（存在しないID）を探して
止まり、「登録した記録を開く」が何も起きない状態になった。
組み立てたIDは、このテストでも目で見ても確かめられないので**書かない**ことにする。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "02_app" / "frontend"

SCRIPTS = (
    "app.js",
    "auth.js",
    # 周りの地域の状況（使いやすさの改修の計画.md 8章）
    "disaster.js",
    "f1.js",
    "f2.js",
    "f4.js",
    "f5.js",
    "f6.js",
    "f7.js",
    "f8.js",
    # 施設の職員の画面（使いやすさの改修の計画.md 9章）
    "facility_staff.js",
    # 家族などによる代わりの登録とつなぎ（使いやすさの改修の計画.md 10章）
    "family.js",
    "handovers.js",
    "icons.js",
    "municipalities.js",
    "organizations.js",
    "orgpicker.js",
    "profile.js",
    "programs.js",
    "ui.js",
    "voice.js",
)

ID_PATTERN = re.compile(r'id="([A-Za-z0-9_-]+)"')
EL_PATTERN = re.compile(r'(?:el|getElementById)\("([A-Za-z0-9_-]+)"\)')
# el(`${prefix}-stage`) のように組み立てたもの
TEMPLATE_PATTERN = re.compile(r"(?:el|getElementById)\(`")


def html_ids() -> set[str]:
    return set(ID_PATTERN.findall((FRONTEND / "index.html").read_text(encoding="utf-8")))


def test_画面から取り出すIDはすべてindex_htmlにある():
    known = html_ids()
    missing = []
    for name in SCRIPTS:
        source = (FRONTEND / name).read_text(encoding="utf-8")
        for used in EL_PATTERN.findall(source):
            if used not in known:
                missing.append(f"{name}: {used}")
    assert missing == [], f"index.html に無いIDを使っています: {missing}"


def test_IDを組み立てて取り出さない():
    # 組み立てると、このテストでも grep でも追えなくなる（実際にそれで止まった）
    built = [name for name in SCRIPTS if TEMPLATE_PATTERN.search((FRONTEND / name).read_text(encoding="utf-8"))]
    assert built == [], f"IDを組み立てている箇所があります: {built}"


def test_indexのIDは重複しない():
    text = (FRONTEND / "index.html").read_text(encoding="utf-8")
    found = ID_PATTERN.findall(text)
    duplicated = sorted({name for name in found if found.count(name) > 1})
    assert duplicated == [], f"同じIDが2か所にあります: {duplicated}"


def test_画面の節はすべてscreenのclassを持つ():
    # ui.showScreen は `.screen` を全部隠して1つ出す。class が抜けた画面は隠れなくなる
    text = (FRONTEND / "index.html").read_text(encoding="utf-8")
    sections = re.findall(r'<section id="(screen-[A-Za-z0-9_-]+)"([^>]*)>', text)
    missing = [
        name for name, attrs in sections if not re.search(r'class="screen[ "]', attrs)
    ]
    assert missing == [], f"class=\"screen\" が無い画面があります: {missing}"
