"""使いやすさと見た目の改修（使いやすさの改修の計画.md 1章・2章・4章・7章）。

画面の動きは自動テストで確かめにくいので、作りのほうを固定する。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
AUTH_JS = (FRONTEND / "auth.js").read_text(encoding="utf-8")
UI_JS = (FRONTEND / "ui.js").read_text(encoding="utf-8")
ICONS_JS = (FRONTEND / "icons.js").read_text(encoding="utf-8")
PROFILE_JS = (FRONTEND / "profile.js").read_text(encoding="utf-8")
STYLE_CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")


def _tag(element_id: str) -> str:
    match = re.search(rf'<[a-z]+\s[^>]*id="{element_id}"[^>]*>', INDEX_HTML, re.S)
    assert match, element_id
    return match.group(0)


# --- 2章 アイコンのボタン ---


def test_右上のプロフィールはアイコンのボタンで読み上げの名前がある():
    tag = _tag("appbar-profile")
    assert 'class="icon-button"' in tag
    assert 'aria-label="プロフィール"' in tag
    assert 'title="プロフィール"' in tag


def test_ログアウトはアイコンと文字のボタン():
    match = re.search(r'<button id="sign-out".*?</button>', INDEX_HTML, re.S)
    assert match
    assert 'data-icon="log-out"' in match.group(0)
    assert "ログアウト" in match.group(0)


def test_アイコンは外から読み込まない():
    assert "http" not in ICONS_JS.replace("http://www.w3.org/2000/svg", "")
    assert 'setAttribute("aria-hidden", "true")' in ICONS_JS


def test_htmlで使うアイコンの名前がicons_jsにある():
    names = set(re.findall(r'data-icon="([a-z-]+)"', INDEX_HTML))
    for name in names:
        assert re.search(rf'^\s*"?{re.escape(name)}"?:', ICONS_JS, re.M), name


def test_処理中もアイコンのボタンの中身を消さない():
    body = re.search(r"export async function withBusy\(.*?\n\}", UI_JS, re.S).group(0)
    assert "Array.from(button.childNodes)" in body
    assert "replaceChildren(...saved)" in body


# --- 1章 ログインの方法 ---


def test_ログインの方法をつなげる():
    for name in ("linkWithPopup", "linkWithRedirect", "linkWithPhoneNumber"):
        assert name in AUTH_JS
    assert 'id="profile-login-methods"' in INDEX_HTML
    assert "linkGoogle" in PROFILE_JS and "startPhoneLink" in PROFILE_JS


def test_前回のログインの方法は名前だけ覚える():
    body = re.search(r"export function rememberLoginMethod\(.*?\n\}", AUTH_JS, re.S).group(0)
    assert "KNOWN_METHODS.has(method)" in body
    assert 'id="login-last-method"' in INDEX_HTML


def test_登録済みなら409でホームに進む():
    assert "res.status !== 409" in APP_JS
    assert APP_JS.count("await goHomeIfRegistered(res)") == 2


def test_登録の画面に別の方法で入り直す案内がある():
    assert 'id="choice-relogin"' in INDEX_HTML
    assert "Google と電話番号は、別の利用者として扱われます" in INDEX_HTML


# --- 4章 使いやすさ ---


def test_画面を切り替えると履歴を足す():
    assert "history.pushState" in UI_JS
    assert 'window.addEventListener("popstate"' in UI_JS
    assert "setHistoryHandlers({ guard: historyGuard" in APP_JS


def test_履歴に記録のIDを入れない():
    for call in re.findall(r"history\.(?:push|replace)State\((.*?)\)", UI_JS):
        assert "survivor" not in call


def test_送る前に欄ごとに確かめる():
    assert "installFormValidation();" in APP_JS
    assert '"submit",' in UI_JS and "stopImmediatePropagation" in UI_JS
    assert ".field-error" in STYLE_CSS


def test_知らせは画面の下に出し閉じるボタンがある():
    assert 'id="notice-close"' in INDEX_HTML
    assert ".toast {" in STYLE_CSS
    assert "NOTICE_OK_MS" in UI_JS


def test_ブラウザの確かめの枠を使わない():
    for path in FRONTEND.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        if path.name == "ui.js":
            # 枠を出せない古いブラウザのときだけ使う
            assert text.count("window.confirm(") == 1
            continue
        assert "window.confirm(" not in text, path.name
    assert '<dialog id="confirm-dialog"' in INDEX_HTML


def test_ホームはタイルで文の一覧を出さない():
    assert 'id="home-next"' not in INDEX_HTML
    assert "next: [" not in APP_JS
    assert 'class="tiles"' in INDEX_HTML
    assert ".tile {" in STYLE_CSS


def test_文字の大きさを選べる():
    assert 'name="text-size"' in INDEX_HTML
    assert "html.text-large" in STYLE_CSS
    assert "saveTextSize" in PROFILE_JS


# --- 7章 見た目 ---


def test_色はrootにまとめる():
    root = re.search(r":root \{(.*?)\}", STYLE_CSS, re.S).group(1)
    for token in ("--accent", "--accent-soft", "--focus", "--shadow"):
        assert token in root


def test_上のバーに主な色を付ける():
    appbar = re.search(r"\.appbar \{(.*?)\}", STYLE_CSS, re.S).group(1)
    assert "background: var(--accent)" in appbar


# --- 11章 期日での絞り込み ---


def test_期限の近い未申請を期日で絞り込める():
    f6 = (FRONTEND / "f6.js").read_text(encoding="utf-8")
    f1 = (FRONTEND / "f1.js").read_text(encoding="utf-8")
    for label in ("期限切れ", "3日以内", "7日以内", "30日以内", "すべて"):
        assert f'label: "{label}"' in f6
    for prefix in ("home", "survivors"):
        assert f'id="{prefix}-support-alerts-filters"' in INDEX_HTML
    assert 'id="filter-deadline"' in INDEX_HTML
    assert "matchesDeadline(item.days_to_nearest_deadline, deadline)" in f1
