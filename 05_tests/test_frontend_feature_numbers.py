"""画面に機能の番号（F0〜F8）を出さない（追加修正の計画.md 9章）。

「（F8）」などは開発で使う番号で、使う人には意味が分からない。
コメントの中の番号は画面に出ないので、そのままでよい。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"

FEATURE_NUMBER = re.compile(r"（F\d")


def _strip_js_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in text.splitlines())


def test_画面のJavaScriptの文字列に機能の番号がない():
    for path in sorted(FRONTEND.glob("*.js")):
        code = _strip_js_comments(path.read_text(encoding="utf-8"))
        assert not FEATURE_NUMBER.search(code), f"{path.name} の画面に出る文字に「（F○）」があります"


def test_indexhtmlの文字に機能の番号がない():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    assert not FEATURE_NUMBER.search(html), "index.html の画面に出る文字に「（F○）」があります"
