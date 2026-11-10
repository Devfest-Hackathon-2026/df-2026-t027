"""組織につながっていないご本人と、支援の組織を選ぶ画面（追加修正②⑦）。

画面の動きは自動テストで確かめにくいので、**作りのほうを固定する**。

- 「まだ支援の組織が登録されていません」「相談などはまだ使えません」をもう出さない（②）
- 組織がないご本人のホームにも、本人向けの機能の入口がある（②）
- 公開の相談の知らせは API が返す文言を使い、画面に書き写さない（②）
- 登録の画面の「複数あるときに選ぶ欄」（survivor-org-choice）は、「支援の組織」の欄に
  置き換えた（⑦）。プロフィールで組織を選び直せる（⑦）
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.services import consultations

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"

APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
F8_JS = (FRONTEND / "f8.js").read_text(encoding="utf-8")
PROFILE_JS = (FRONTEND / "profile.js").read_text(encoding="utf-8")
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")


def _survivor_no_org_guide() -> str:
    match = re.search(r"\[SURVIVOR_NO_ORG_KEY\]: \{.*?\n  \},", APP_JS, re.S)
    assert match, "HOME_GUIDE に組織がないご本人の案内が見つかりません"
    return match.group(0)


def test_組織がない本人のホームに組織がないという文を出さない():
    for text in ("まだ支援の組織が登録されていません", "相談などはまだ使えません"):
        assert text not in APP_JS, f"app.js に「{text}」が残っています"
        assert text not in INDEX_HTML, f"index.html に「{text}」が残っています"


def test_組織がない本人のホームの案内は使える前提で書く():
    guide = _survivor_no_org_guide()
    assert "つながなくても使えます" in guide
    assert "相談" in guide


def test_組織がない本人のホームにも本人向けの入口がある():
    # 組織があってもなくても同じボタンを使う（SURVIVOR_ACTIONS）
    match = re.search(r"\[SURVIVOR_NO_ORG_KEY\]: \[(.*?)\n  \],", APP_JS, re.S)
    assert match, "HOME_ACTIONS に組織がないご本人のボタンが見つかりません"
    assert "...SURVIVOR_ACTIONS" in match.group(1)
    assert "支援の組織を選ぶ" in match.group(1)
    actions = re.search(r"const SURVIVOR_ACTIONS = \[(.*?)\n\];", APP_JS, re.S)
    assert actions, "SURVIVOR_ACTIONS が見つかりません"
    for label in ("ご自分の記録を見る", "相談する", "受けられる支援を見る"):
        assert label in actions.group(1)


def test_古い組織につなぐ経路を使わない():
    assert "/api/me/connect-organization" not in APP_JS
    assert "survivor-org-choice" not in INDEX_HTML
    assert "survivor-org-choice" not in APP_JS


def test_公開の相談の知らせはAPIの文言を使う():
    assert "consultation_public_notice" in F8_JS
    # 文言の正本は services/consultations.py。画面に書き写さない
    assert consultations.PUBLIC_NOTICE[:15] not in F8_JS
    assert consultations.PUBLIC_NOTICE[:15] not in INDEX_HTML
    assert 'id="consultation-public-notice"' in INDEX_HTML


def test_公開の相談の一覧をホームに出す():
    assert "/api/consultations/public" in F8_JS
    assert 'id="home-public-consultations-card"' in INDEX_HTML
    assert "loadPublicConsultations" in APP_JS


def test_登録の画面に支援の組織の欄がある():
    assert 'id="survivor-org"' in INDEX_HTML
    assert 'id="survivor-org-filter"' in INDEX_HTML
    # 選ぶ前に、記録（健康・介護の情報を含む）が見えるようになることを知らせる
    section = re.search(
        r'<section id="screen-survivor-self".*?</section>', INDEX_HTML, re.S
    ).group(0)
    assert "健康・介護の情報を含む" in section


def test_プロフィールで支援の組織を選び直せる():
    assert 'id="form-profile-organization"' in INDEX_HTML
    assert "/api/me/organization" in PROFILE_JS
    # 変える前に、担当が外れることと、見えなくなることを確かめる
    # 確かめは画面の中の枠で出す（使いやすさの改修の計画.md 4-2の6）
    assert "confirmDialog(organizationConfirmText" in PROFILE_JS
    assert "担当の支援者は外れます" in PROFILE_JS
