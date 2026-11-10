"""右上のプロフィールとログアウト（追加修正の計画.md 8章）。

- 上のバーに「プロフィール」と「ログアウト」を並べる。登録が済むまでは隠す
- 下のタブの「プロフィール」も残す（どちらからでも開ける）
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")


def _appbar() -> str:
    match = re.search(r'<header class="appbar">(.*?)</header>', INDEX_HTML, re.S)
    assert match
    return match.group(1)


def test_上のバーにプロフィールとログアウトが並ぶ():
    bar = _appbar()
    assert 'id="appbar-profile"' in bar
    assert 'id="sign-out"' in bar
    assert bar.index('id="appbar-profile"') < bar.index('id="sign-out"')


def test_上のバーのプロフィールは登録の前は隠れている():
    assert re.search(r'id="appbar-profile"[^>]*\shidden', _appbar())
    # 登録が済んだ人のホームを出すときに出し、ログアウトで隠す
    assert 'el("appbar-profile").hidden = false' in APP_JS
    assert 'el("appbar-profile").hidden = true' in APP_JS


def test_下のタブのプロフィールも残す():
    assert APP_JS.count('{ key: "profile", label: "プロフィール"') >= 4
