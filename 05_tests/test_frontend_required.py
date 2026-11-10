"""必須・任意の表示（要望6。追加改修の計画.md 7章）。

- ラベルに「（任意）」を手で書かない。印は CSS が `required` から自動で出す
  （[style.css](../02_app/frontend/style.css) の `.field:not(.field-check):has(:required)`）。
  手で書くと、印と実際の入力チェックがずれる。
- `index.html` の `required` と、サーバー側の必須の判定が合っているかを、
  踏んだ食い違い（[f8.js](../02_app/frontend/f8.js) の返事の欄）を踏まえて固定する。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "02_app" / "frontend"

INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")
F8_JS = (FRONTEND / "f8.js").read_text(encoding="utf-8")


def test_ラベルに任意と手で書かない():
    # 「（任意）」の印は CSS が required の有無から自動で出す。手で書くと二重になり、
    # required を直し忘れたときにずれる
    assert "任意" not in INDEX_HTML, "index.html のラベルに「任意」と書かないでください（CSSが自動で出す）"
    assert "（任意）" not in F8_JS, "f8.js のラベルに「（任意）」と書かないでください（CSSが自動で出す）"


def test_必須任意の印がcssにある():
    assert ".field:not(.field-check) > .field-label::after" in STYLE_CSS
    assert ".field:not(.field-check):has(:required) > .field-label::after" in STYLE_CSS
    assert 'content: "任意"' in STYLE_CSS
    assert 'content: "必須"' in STYLE_CSS


# 欄のID→サーバーが必須にしているか。サーバー側の根拠は括弧の中に書く。
REQUIRED_FIELD_IDS = {
    # services/survivors.py validate_stage・validate_location_type・_validate_text(required=True)
    # 住んでいる都道府県・市区町村（要望8。組織を決めるのに使うため必須）
    "survivor-prefecture": True,
    "survivor-municipality": True,
    "survivor-name": True,
    # 本人の登録では後で入れてよい（使いやすさの改修の計画.md 4-2の3。
    # services/survivors.py build_survivor_row の registered_via == VIA_SELF）
    "survivor-age": False,
    "survivor-household": False,
    "survivor-contact": False,
    "survivor-address": False,
    "survivor-stage": True,
    "survivor-location-type": True,
    "survivor-location-detail": False,
    # 支援の組織は任意（追加修正⑦。空なら組織なしで登録する。routers/account.py _chosen_organization）
    "survivor-org": False,
    "survivor-org-filter": False,
    "link-code": True,
    # services/organizations.py resolve_join_code／users.py validate_display_name。
    # 参加コードは空でもよい（要望1。組織なしの一般の支援者として登録する）
    "supporter-code": False,
    "supporter-name": True,
    # 職種は必ず選ばせる。専門職でない方は「該当なし」を選ぶ（追加修正③）。
    # services/users.py validate_profession は「該当なし」と空を、どちらも職種なしにする
    "supporter-profession": True,
    # 追加修正④・使いやすさの改修の計画.md 3章。services/users.py validate_contact・
    # validate_activity_municipality・validate_supporter_address はどれも必須
    "supporter-contact": True,
    "supporter-prefecture": True,
    "supporter-municipality": True,
    "supporter-address": True,
    "new-name": True,
    "new-age": True,
    "new-household": True,
    "new-contact": False,
    # 専門職の登録では組織がすでに決まっているので、市区町村・住所は任意（要望8）
    "new-prefecture": False,
    "new-municipality": False,
    "new-address": False,
    "new-stage": True,
    "new-location-type": True,
    "new-location-detail": False,
    "location-stage": True,
    "location-type": True,
    "location-detail": False,
    "assign-supporter": True,
    "risk-level": True,
    "risk-reason": True,
    # services/visits.py clean_raw_text(required=True)。「AIを使わずに手で入れる」は
    # type=button で、この required は submit を通さない（novalidate のため実害はない）
    "visit-raw-text": True,
    # services/consultations.py clean_raw_text は常に必須
    "consultation-raw-text": True,
    "facility-name": True,
    "facility-type": True,
    "facility-address": False,
    "facility-vacancies": True,
    "placement-facility": True,
    "placement-reason": True,
    "handover-destination": True,
    "handover-summary": True,
    "handover-cautions": False,
    "support-housing-damage": True,
    "support-rebuild-intent": True,
    # プロフィールの画面（要望4）。services/users.py validate_display_name・
    # services/survivors.py change_basic（_validate_text・_validate_int）
    "profile-display-name": True,
    # 専門職のときだけ必須（条件付き）。常には必須にできないので任意のまま
    "profile-profession": False,
    "profile-name": True,
    # 本人が自分で直すときは空でもよい（routers/survivors.py change_basic の optional_counts）
    "profile-age": False,
    "profile-household": False,
    "profile-contact": False,
    "profile-address": False,
    # 支援者のプロフィール（追加修正④）。支援者の登録と同じ判定（PATCH /api/me）
    "profile-supporter-contact": True,
    "profile-supporter-prefecture": True,
    "profile-supporter-municipality": True,
    "profile-supporter-address": True,
    # ご本人が支援の組織を選び直す（追加修正⑦。空なら組織を外す）
    "profile-survivor-org": False,
    "profile-org-filter": False,
    # 支援の一覧の名前で探す欄（使いやすさの改修の計画.md 6-2。探すだけで送らない）
    "programs-search": False,
    # 組織に入る・組織を作る（要望1・2）。services/organizations.py resolve_join_code・
    # validate_org_name・validate_municipality_code
    "join-org-code": True,
    # 支援者の登録と同じく、必ず選ばせる（「該当なし」を選べる。追加修正③）
    "join-org-profession": True,
    "create-org-name": True,
    "create-org-prefecture": True,
    "create-org-municipality": True,
    # 組織を作った人は必ず専門職になるので、職種は必須
    "create-org-profession": True,
    # 周りの地域の状況（使いやすさの改修の計画.md 8章）。services/disaster_status.py clean_fields。
    # 市区町村・災害の名前・項目・状態・出典のURLは必須。期限と補足は任意
    "disaster-prefecture": True,
    "disaster-municipality": True,
    "disaster-prefecture-filter": False,
    "disaster-municipality-filter": False,
    "disaster-name": True,
    "disaster-kind": True,
    "disaster-state": True,
    "disaster-deadline": False,
    "disaster-source-url": True,
    "disaster-note": False,
    # 家族などによる代わりの登録（使いやすさの改修の計画.md 10章）。本人の登録と同じ判定
    # （services/survivors.py build_survivor_row の _ORGLESS_VIAS）に、本人の同意を足す
    # （validate_proxy_consent。必須）
    "family-consent": True,
    "family-prefecture": True,
    "family-municipality": True,
    "family-name": True,
    "family-stage": True,
    "family-location-type": True,
    "family-location-detail": False,
    "family-age": False,
    "family-household": False,
    "family-contact": False,
    "family-address": False,
    "family-org": False,
    "family-org-filter": False,
    # 招待コード（services/link_codes.py resolve_family_code。空では照合しない）
    "family-join-code": True,
    # 家族が居場所・基本の情報を直す（routers/family.py。change_location・
    # change_basic(optional_counts=True)）
    "family-edit-stage": True,
    "family-edit-location-type": True,
    "family-edit-location-detail": False,
    "family-edit-name": True,
    "family-edit-age": False,
    "family-edit-household": False,
    "family-edit-contact": False,
    "family-edit-address": False,
}


def _has_required(field_id: str) -> bool:
    match = re.search(
        rf'id="{re.escape(field_id)}"[^>]*?(/?)>',
        INDEX_HTML,
        re.DOTALL,
    )
    assert match, f"index.html に id={field_id} の欄が見つかりません"
    tag = match.group(0)
    return bool(re.search(r"(^|\s)required(\s|=|/|>)", tag))


def test_静的な欄のrequiredがサーバーの必須と合っている():
    mismatched = [
        field_id
        for field_id, must_be_required in REQUIRED_FIELD_IDS.items()
        if _has_required(field_id) != must_be_required
    ]
    assert mismatched == [], f"required の有無がサーバーの判定と合っていません: {mismatched}"


def test_f8の返事の欄はrequiredにしてある():
    # services/consultations.py clean_reply_text は空では送れない（返事は必須）。
    # 次の対応（next_action）は任意（画面でも required を付けない）
    assert "reply.required = true" in F8_JS, "f8.js の返事の欄に required を付けてください"
