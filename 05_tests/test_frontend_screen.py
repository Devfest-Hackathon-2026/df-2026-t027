"""画面の改修の計画.md の画面の決まりを固定するテスト。

自動テストでは幅を測れないので、CSS と HTML の書き方を確かめる。幅ごとの見え方は手で確かめる（1-5）。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")


def _media_block(query: str) -> str:
    """`@media <query> { … }` の中身を、かっこの対応をたどって取り出す（最後に書いたもの）。"""
    start = STYLE_CSS.rfind(f"@media {query}")
    assert start >= 0, f"@media {query} が見つかりません"
    opened = STYLE_CSS.index("{", start)
    depth = 0
    for index in range(opened, len(STYLE_CSS)):
        if STYLE_CSS[index] == "{":
            depth += 1
        elif STYLE_CSS[index] == "}":
            depth -= 1
            if depth == 0:
                return STYLE_CSS[opened + 1 : index]
    raise AssertionError(f"@media {query} が閉じていません")


def _rule_in(block: str, selector: str) -> str:
    match = re.search(r"(?:^|\n)\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", block)
    assert match, f"{selector} の指定が見つかりません"
    return match.group(1)


# --- 1章・2章：広い画面の作り ---


def test_PCではタブを左の縦のメニューにする():
    block = _media_block("screen and (min-width: 1024px)")
    tabbar = _rule_in(block, ".tabbar")
    assert "flex-direction: column" in tabbar
    assert "width: var(--sidebar)" in tabbar
    # 中身はメニューの右に置く
    page = _rule_in(block, "body:has(#tabbar:not([hidden])) .page")
    assert "var(--sidebar)" in page


def test_広い画面では一覧を格子に並べる():
    block = _media_block("screen and (min-width: 768px)")
    cards = _rule_in(block, ".cards")
    assert "display: grid" in cards
    assert "repeat(auto-fill, minmax(320px, 1fr))" in cards
    # 0件の知らせは全部の列を使う（細い1マスに押し込まない）
    assert re.search(r"\.cards > \.note,\s*\.cards > \.skeleton\s*\{\s*grid-column: 1 / -1;", STYLE_CSS)


def test_広い画面の指定は印刷に効かせない():
    # 引き継ぎシートの印刷の見た目を変えない（1-4）
    assert "@media (min-width: 1024px) {\n  .tabbar" not in STYLE_CSS
    assert "@media screen and (min-width: 1024px)" in STYLE_CSS


def test_入力の画面は読みやすい幅に止める():
    for name in ("login", "survivor-new", "visit-new", "consultation-new", "profile", "family-new"):
        assert re.search(rf'<section id="screen-{name}" class="screen screen-narrow"', INDEX_HTML), name
    # 一覧の画面は広く使う
    for name in ("survivors", "facilities", "programs", "family-list"):
        assert f'<section id="screen-{name}" class="screen" hidden>' in INDEX_HTML, name
    assert ".screen-narrow {\n  max-width: 640px;" in STYLE_CSS


# --- 3章：本人のホームの緑のタイル ---


def test_本人のホームには色で目立たせるタイルを置かない():
    start = APP_JS.index("const SURVIVOR_ACTIONS = [")
    end = APP_JS.index("\n];", start)
    assert "primary: true" not in APP_JS[start:end]


# --- 4章：戻るボタン ---


def test_戻る操作はすべて枠付きの戻るボタンにする():
    buttons = re.findall(r"<button([^>]*)>(.*?)</button>", INDEX_HTML, flags=re.S)
    back = [(attrs, body) for attrs, body in buttons if re.search(r"に戻る\s*(</span>)?\s*$", body.strip())]
    # 「居場所を変える（記録に戻る）」は戻る操作ではなく、記録の画面で居場所を変えるためのボタン
    back = [(attrs, body) for attrs, body in back if "居場所を変える" not in body]
    assert len(back) >= 25
    for attrs, body in back:
        assert 'class="back-button' in attrs, body.strip()
        assert 'data-icon="arrow-left"' in body, body.strip()
        assert "link-button" not in attrs, body.strip()


def test_戻るボタンはログアウトと同じ枠付きの角の丸い形():
    rule = re.search(r"\n\.back-button \{([^}]*)\}", STYLE_CSS).group(1)
    assert "border: 1px solid var(--accent)" in rule
    assert "border-radius: 999px" in rule
    assert "min-height: 44px" in rule


# --- 6章：本人は相談をそのまま送る ---

F8_JS = (FRONTEND / "f8.js").read_text(encoding="utf-8")


def _section(screen_id: str) -> str:
    start = INDEX_HTML.index(f'<section id="{screen_id}"')
    return INDEX_HTML[start : INDEX_HTML.index("</section>", start)]


def test_本人の相談の画面に整えるボタンと確かめる段が無い():
    section = _section("screen-consultation-new")
    assert "AIで項目に整える" not in section
    assert "consultation-confirm-card" not in section
    assert "consultation-items" not in section
    # 文章と種類を書いて、そのまま送る
    assert 'id="consultation-raw-text"' in section
    assert 'id="consultation-category"' in section
    assert "この内容で送る" in section


def test_本人の画面は整える処理を呼ばない():
    assert "/consultations/structure" not in F8_JS


def test_相談員には本人の文章を先に出しAIの案を添える():
    assert "AIが整えた案" in F8_JS
    # ご本人には案を見せない
    assert "showDraft: !isSurvivor()" in F8_JS


# --- 5章：別の利用者になっている電話番号を、消してつなぎ直す（案C） ---

AUTH_JS = (FRONTEND / "auth.js").read_text(encoding="utf-8")
PROFILE_JS = (FRONTEND / "profile.js").read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    start = source.index(f"export async function {name}(")
    return source[start : source.index("\n}\n", start)]


def test_登録があるか確かめてから電話番号の利用者を消す():
    body = _function(AUTH_JS, "releaseConflictingPhone")
    # SMS で確かめた資格で入り、/api/me の登録を見てから消す
    assert body.index("signInWithCredential") < body.index('apiFetch("/api/me")')
    assert body.index("me.registered") < body.index("deleteUser")
    # 登録があれば消さずにログアウトする
    registered_branch = body[body.index("if (me.registered)") : body.index("deleteUser")]
    assert "signOut(auth)" in registered_branch


def test_サーバーに利用者を消す処理を足していない():
    backend = Path(__file__).resolve().parents[1] / "02_app" / "backend"
    for path in backend.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "accounts:delete" not in text, path
        assert "firebase_admin" not in text, path


def test_つなぎ直す途中で覚えるのは段階の名前だけ():
    # 電話番号は覚えない（使いやすさの改修の計画.md 1章と同じ決まり）
    assert "sessionStorage.setItem(MERGE_KEY, value)" in AUTH_JS
    assert re.search(r'export const MERGE_RELINK = "relink";', AUTH_JS)
    assert re.search(r'export const MERGE_REGISTERED = "registered";', AUTH_JS)
    assert "sessionStorage.setItem(MERGE_KEY, phone" not in AUTH_JS


def test_プロフィールで消してよいかを聞いてから進める():
    body = PROFILE_JS[PROFILE_JS.index("async function offerPhoneRelease(") :]
    assert body.index("confirmDialog(") < body.index("releaseConflictingPhone()")
