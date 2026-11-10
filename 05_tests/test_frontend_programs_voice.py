"""支援の一覧（使いやすさの改修の計画.md 6-2・11-2）と、相談の音声入力（13章）の画面の作り。

画面の動きは自動テストで確かめにくいので、作りのほうを固定する（test_frontend_usability.py と同じ考え方）。
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.services import supports

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
PROGRAMS_JS = (FRONTEND / "programs.js").read_text(encoding="utf-8")
VOICE_JS = (FRONTEND / "voice.js").read_text(encoding="utf-8")
ICONS_JS = (FRONTEND / "icons.js").read_text(encoding="utf-8")


def _section(screen_id: str) -> str:
    match = re.search(rf'<section id="{screen_id}".*?</section>', INDEX_HTML, re.S)
    assert match, screen_id
    return match.group(0)


# --- 6-2 支援の一覧 ---


def test_支援の画面がありタブが2つある():
    assert "<!-- 支援の一覧（使いやすさの改修の計画.md 6-2） -->" in INDEX_HTML
    section = _section("screen-programs")
    assert 'id="programs-tab-mine"' in section and "あなたの支援" in section
    assert 'id="programs-tab-all"' in section and "支援制度の一覧" in section
    assert 'id="programs-search"' in section
    assert 'id="programs-find"' in section and "自分に合うものを調べる" in section


def test_支援の一覧ではAIを呼ばない():
    # 一覧は GET の2本だけを読む。AI で調べる API は、この画面からは呼ばない
    assert '"/api/programs"' in PROGRAMS_JS
    assert "/supports`" in PROGRAMS_JS
    assert "support-candidates" not in PROGRAMS_JS


def test_これを記録するは一覧からの記録として送る():
    assert "これを記録する" in PROGRAMS_JS
    assert "from_list: true" in PROGRAMS_JS


def test_カードに制度の中身と出典を出す():
    for label in ("対象の人", "受けられる内容", "申し込み先", "期限の決まり", "必要な書類", "出典"):
        assert f'"{label}"' in PROGRAMS_JS, label
    assert 'rel = "noopener noreferrer"' in PROGRAMS_JS


def test_あなたの支援は11章の絞り込みを使う():
    assert 'from "./f6.js"' in PROGRAMS_JS
    for name in ("renderDeadlineChips", "matchesDeadline", "DEADLINE_FILTERS"):
        assert name in PROGRAMS_JS, name


def test_本人のタブとホームの入口から支援の画面を開く():
    tab_defs = APP_JS.split("const TAB_DEFS = {", 1)[1]
    survivor_tabs = tab_defs.split("  survivor: [", 1)[1].split("\n  ],", 1)[0]
    assert 'key: "supports", label: "支援", icon: "gift"' in survivor_tabs
    assert "programs.openPrograms()" in survivor_tabs
    actions = re.search(r"const SURVIVOR_ACTIONS = \[(.*?)\n\];", APP_JS, re.S).group(1)
    assert "run: () => programs.openPrograms()" in actions
    assert "countKey: programs.DUE_COUNT_KEY" in actions
    # AI で調べる画面は、支援の画面のボタンから開く
    assert "programs.bind({ openFinder: () => openSupportFinder() })" in APP_JS
    assert "f6.openSupports();" in APP_JS


def test_本人のホームに期限の知らせを待たずに出す():
    assert 'id="home-deadline-banner"' in _section("screen-home")
    render_home = APP_JS.split("function renderHome(me) {", 1)[1].split("\n}\n", 1)[0]
    assert "programs.setMe(me);" in render_home
    # 待たない（await を付けない）
    assert "  programs.loadHomeBanner();" in render_home
    assert "await programs.loadHomeBanner" not in APP_JS
    assert "programs.setMe(null);" in APP_JS


def test_ホームの知らせの日数はサーバーと同じ():
    match = re.search(r"const DEFAULT_ALERT_DAYS = (\d+);", PROGRAMS_JS)
    assert match
    assert int(match.group(1)) == supports.SURVIVOR_ALERT_DAYS == 3
    assert "alert_days" in PROGRAMS_JS


def test_支援の画面はコンソールに出さない():
    assert "console." not in PROGRAMS_JS


# --- 13章 音声入力 ---


def test_声の入力はブラウザの文字起こしを使う():
    assert "window.SpeechRecognition || window.webkitSpeechRecognition" in VOICE_JS
    assert 'recognition.lang = "ja-JP"' in VOICE_JS
    assert "recognition.interimResults = true" in VOICE_JS
    # Gemini にもサーバーにも送らない
    assert "apiFetch" not in VOICE_JS and "fetch(" not in VOICE_JS


def test_声の入力のボタンは押した状態と読み上げの名前を持つ():
    assert 'setAttribute("aria-pressed"' in VOICE_JS
    assert 'setAttribute("aria-label"' in VOICE_JS
    for name in ("mic", "mic-off"):
        assert f'"{name}"' in VOICE_JS
        assert re.search(rf'^\s*"?{re.escape(name)}"?:', ICONS_JS, re.M), name


def test_使えないブラウザではキーボードのマイクを案内する():
    assert "キーボードのマイクのボタンでも話して入力できます" in VOICE_JS
    body = VOICE_JS.split("function attach(textarea) {", 1)[1].split("\n}\n", 1)[0]
    unsupported = body.split("if (!isSupported()) {", 1)[1].split("return;", 1)[0]
    # 使えないときはボタンを作らない
    assert "button" not in unsupported


def test_初めて使うときに了承をもらい端末に覚える():
    assert "ブラウザの会社のサーバーに送られることがあります" in VOICE_JS
    assert "confirmDialog(CONSENT_MESSAGE" in VOICE_JS
    # localStorage は使えないことがあるので try で包む
    for call in ("localStorage.getItem(CONSENT_KEY)", "localStorage.setItem(CONSENT_KEY"):
        before = VOICE_JS.split(call, 1)[0]
        assert before.rstrip().endswith("try {") or before.rsplit("try {", 1)[-1].count("}") == 0, call


def test_声も文字もログに出さない():
    assert "console." not in VOICE_JS


def test_相談と訪問の欄に声の入力を付ける():
    assert 'attach(el("consultation-raw-text"))' in VOICE_JS
    assert 'attach(el("visit-raw-text"))' in VOICE_JS
    assert "installVoiceInput();" in APP_JS
