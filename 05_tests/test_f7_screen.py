"""F7 組織全体の状況の画面のテスト（開発手順書 Step 11 の完了の確認）。

守ること：

- 入口は**専門職のホームだけ**（要件定義書10-5の表の「専」）。本人・一般の支援者には出さない。
- 画面は**数を作らない**。`GET /api/dashboard` が返した数をそのまま出す
  （人数の正本は mart だけ。2か所で数えると食い違ったときに決められない）。
- 断り書き・区分の呼び方は **APIが返すものだけ**を出す（正本は `services/dashboard.py`）。
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.services import dashboard

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"


def f7_source() -> str:
    return (FRONTEND / "f7.js").read_text(encoding="utf-8")


def home_actions_for(role: str) -> str:
    """`app.js` の HOME_ACTIONS から、その立場の分だけを取り出す。"""
    source = (FRONTEND / "app.js").read_text(encoding="utf-8")
    body = source.split("const HOME_ACTIONS = {", 1)[1]
    # HOME_ACTIONS の閉じ括弧より先（bind など）まで読まないように切る
    body = body.split("\n};", 1)[0]
    roles = ("professional", "volunteer", "survivor")
    starts = {name: body.index(f"{name}: [") for name in roles if f"{name}: [" in body}
    assert role in starts, f"HOME_ACTIONS に {role} がありません"
    after = [pos for name, pos in starts.items() if pos > starts[role]]
    return body[starts[role] : min(after)] if after else body[starts[role] :]


def test_専門職のホームに組織全体の状況の入口がある():
    assert "f7.openDashboard" in home_actions_for("professional")


def test_本人と一般の支援者のホームには入口を出さない():
    # 本人に悪化の兆しの度合いを返さない決まり（10-3）と、使えるのは専門職だけ（10-5）
    for role in ("survivor", "volunteer"):
        assert "f7." not in home_actions_for(role), f"{role} のホームに F7 の入口があります"


def test_画面の節がindex_htmlにある():
    text = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert '<section id="screen-dashboard" class="screen screen-medium" hidden>' in text


def test_画面に断り書きを書き写していない():
    # 文言の正本は services/dashboard.py。画面に書き写すとずれる（Step 8・10 と同じ形）
    source = f7_source()
    assert dashboard.NOTE not in source
    for label in dashboard.RISK_LEVELS.values():
        assert label not in source, f"度合いの呼び方を画面に書いています: {label}"
    assert dashboard.EMPTY_LABEL not in source


def test_画面は人数を足し合わせない():
    # 人数の正本は mart だけ（`GET /api/dashboard` が足し合わせた数を返す）
    source = f7_source()
    for sign in (".reduce(", "+="):
        assert sign not in source, f"画面で数を作っています: {sign}"


def test_画面はAPIが返す4つの数をすべて出す():
    source = f7_source()
    for metric in dashboard.METRICS:
        assert metric in source, f"画面に出していない数があります: {metric}"


def test_画面はいつ時点の集計かを出す():
    assert "as_of" in f7_source()


def test_画面の数のIDはindex_htmlにある():
    # el("…") のIDは test_frontend_ids.py が全部まとめて確かめる。ここでは
    # 4つの数の置き場所だけ、名前を決めて固定する（取り違えると数が出ない）
    text = (FRONTEND / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', text))
    for metric in dashboard.METRICS:
        assert f"dashboard-total-{metric.replace('_', '-')}" in ids, metric
