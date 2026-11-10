"""利用者の立場と組織（F0。要件定義書7-3-1の raw_users・7-3-2の stg_users）。

- 立場を決めるのは**このモジュールだけ**。要求の中に書かれた立場や組織のIDは使わない（要件定義書10-3）。
- 書き込みは raw_users への追記だけ。立場や表示名を変えるときも、新しい行を足して版を上げる
  （`stg_users` が user_id ごとに最新の1行を返す）。
- 電話番号そのものは保存しない。保存するのはログインの方法（google・phone）だけ（要件定義書7-2）。
- 支援者の連絡先・活動する市区町村・住所（追加修正④）は、支援者が自分で入れる値。
  連絡先と住所は個人の情報なので、ログに出さず、本人の `/api/me` にだけ返す
  （他の支援者が読む一覧 `list_by_role`・`names_by_ids`・`fetch_supporter` には入れない）。
- 組織の中の権限（`org_permission`。追加修正⑥）は立場とは別に持つ。今あるのは「オーナー」
  だけで、組織を作った人と、オーナーが付けた同じ組織の専門職がなる。付ける・外すときも
  新しい版の行を足す（上書きしない）。
- 施設の職員（`facility_staff`。使いやすさの改修の計画.md 9章）は、施設の参加コードで入る。
  自分の施設（`facility_id`）と、その施設を登録した組織（`org_id`）を持つ。見て直せるのは
  自分の施設の空きなどだけで、被災者の記録には触れない（`auth.registered_user` が断る）。
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Sequence
from typing import Any

from backend.config import get_settings
from backend.services import bq

# 立場（raw_users.role）。要件定義書F0の3つと、施設の職員（使いやすさの改修の計画.md 9章）。
ROLE_SURVIVOR = "survivor"
ROLE_PROFESSIONAL = "professional"
ROLE_VOLUNTEER = "volunteer"
# 施設の職員。施設の参加コードで入り、自分の施設の空き・断水・停電・対応できる介護と医療だけを直す
ROLE_FACILITY_STAFF = "facility_staff"
ROLES = (ROLE_SURVIVOR, ROLE_PROFESSIONAL, ROLE_VOLUNTEER, ROLE_FACILITY_STAFF)

# 支援者の立場（参加コードで決まるもの。本人は参加コードを使わない）。
SUPPORTER_ROLES = (ROLE_PROFESSIONAL, ROLE_VOLUNTEER)

# 職種（専門職だけ。要件定義書F0）。
PROFESSIONS = ("保健師", "ケアマネジャー", "相談員", "その他")

# 専門職でない支援者が選ぶ値（追加修正③）。保存するときは職種なし（None）にする。
# 画面では職種の欄を必ず選ばせ、空のまま送られたとき（古い画面）も同じ扱いにする。
NO_PROFESSION = "該当なし"

# 表示名の長さの上限（画面の崩れと、間違えて長文を貼ったときの保険）。
DISPLAY_NAME_MAX_LENGTH = 40

# 組織の中の権限（raw_users.org_permission。追加修正⑥）。空ならなし。
# オーナーは、自分の組織の参加コードを見る・発行し直す、ほかの専門職をオーナーにする・外すができる。
ORG_PERMISSION_OWNER = "owner"

# 支援者の連絡先・住所の長さの上限（追加修正④。本人の記録の同じ項目と合わせる）。
CONTACT_MAX_LENGTH = 100
ADDRESS_MAX_LENGTH = 100

# 市区町村の団体コード（6桁の数字）。`services/organizations.py` と同じ形。
# organizations が users を読み込むため、ここでは同じ決まりを書き直している（循環を避ける）。
MUNICIPALITY_CODE_PATTERN = re.compile(r"^\d{6}$")

# stg_users から読む列。`SELECT *` にしない（列が増えたときに意図しない値を外へ出さないため）。
_USER_COLUMNS = (
    "u.user_id",
    "u.org_id",
    "u.role",
    "u.profession",
    "u.display_name",
    "u.login_method",
    "u.linked_survivor_id",
    # 支援者の連絡先・活動する市区町村・住所（追加修正④）。本人の `/api/me` にだけ返す
    "u.contact",
    "u.municipality_code",
    "u.address",
    # 組織の中の権限（追加修正⑥）
    "u.org_permission",
    # 施設の職員の、自分の施設のID（使いやすさの改修の計画.md 9章）
    "u.facility_id",
    # 組織の名前も一緒に取る。画面の見出しに出すのに毎回もう1回読むと、要求ごとに
    # BigQuery の往復が2回になるため（1回あたり1秒ほどかかる）。
    "o.org_name",
)


class UserValidationError(ValueError):
    """立場・職種・表示名の値が正しくない。"""


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


def fetch_user(user_id: str) -> dict[str, Any] | None:
    """利用者を1人読む（なければ None）。立場と組織はここで決まる。"""
    if not user_id:
        return None
    sql = (
        f"SELECT {', '.join(_USER_COLUMNS)}\n"
        f"FROM {_staging_table('stg_users')} AS u\n"
        f"LEFT JOIN {_staging_table('stg_organizations')} AS o\n"
        "  ON o.org_id = u.org_id\n"
        "WHERE u.user_id = @user_id"
    )
    return bq.query_one(sql, {"user_id": user_id})


def validate_display_name(display_name: str) -> str:
    """表示名を確かめて返す（前後の空白は取る）。"""
    name = (display_name or "").strip()
    if not name:
        raise UserValidationError("表示名を入れてください")
    if len(name) > DISPLAY_NAME_MAX_LENGTH:
        raise UserValidationError(f"表示名は{DISPLAY_NAME_MAX_LENGTH}文字以内にしてください")
    return name


def validate_profession(role: str, profession: str | None) -> str | None:
    """職種を確かめて返す。専門職のときだけ必要で、ほかの立場では空にする。"""
    if role != ROLE_PROFESSIONAL:
        return None
    value = (profession or "").strip()
    if value == NO_PROFESSION:
        value = ""
    if value not in PROFESSIONS:
        # 「入れたコードが専門職用だった」ことまで伝える。実際に、一般の支援者用の
        # つもりで専門職用のコードを入れ、なぜ職種が必須になるのか分からなくなった。
        # この文面はコードの照合が通ったあとにしか出ないので、手がかりは増えない。
        raise UserValidationError(
            "このコードは専門職用です。職種を選んでください（"
            + "・".join(PROFESSIONS)
            + "）。ボランティアやご家族の方は、一般の支援者用のコードをお使いください"
        )
    return value


def _clean_text(value: str | None, *, label: str, max_length: int) -> str:
    text = (value or "").strip()
    if len(text) > max_length:
        raise UserValidationError(f"{label}は{max_length}文字以内にしてください")
    return text


def validate_contact(contact: str | None) -> str:
    """支援者の連絡先（必須。追加修正④）。担当の相談員から連絡するのに使う。"""
    text = _clean_text(contact, label="連絡先", max_length=CONTACT_MAX_LENGTH)
    if not text:
        raise UserValidationError("連絡先（電話番号など）を入れてください")
    return text


def validate_activity_municipality(municipality_code: str | None) -> str:
    """活動する市区町村の団体コード（必須。追加修正④）。形（6桁の数字）だけを確かめる。"""
    code = (municipality_code or "").strip()
    if not MUNICIPALITY_CODE_PATTERN.match(code):
        raise UserValidationError("活動する都道府県・市区町村を一覧から選んでください")
    return code


def validate_supporter_address(address: str | None) -> str:
    """支援者の住所（必須。使いやすさの改修の計画.md 3章。前は任意だった）。"""
    text = _clean_text(address, label="住所", max_length=ADDRESS_MAX_LENGTH)
    if not text:
        raise UserValidationError("住所（町名・番地など）を入れてください")
    return text


def is_profile_incomplete(row: dict[str, Any] | None) -> bool:
    """支援者の必須の項目（連絡先・活動する市区町村・住所）が空か（追加修正④・使いやすさの改修の計画.md 3章）。

    必須にする前に登録した支援者は、その項目が空のまま。ホームで
    プロフィールを入れるよう案内するのに使う（使うのを止めはしない）。
    施設の職員も、支援者の登録と同じ項目を入れて登録するので、同じ判定にする（9章）。
    """
    if not row or row.get("role") not in (*SUPPORTER_ROLES, ROLE_FACILITY_STAFF):
        return False
    return not (row.get("contact") and row.get("municipality_code") and row.get("address"))


def register_user(
    *,
    user_id: str,
    org_id: str,
    role: str,
    display_name: str,
    login_method: str,
    profession: str | None = None,
    linked_survivor_id: str | None = None,
    contact: str | None = None,
    municipality_code: str | None = None,
    address: str | None = None,
    org_permission: str | None = None,
    facility_id: str | None = None,
    created_by: str | None = None,
    created_role: str | None = None,
) -> dict[str, Any]:
    """利用者を raw_users に追記する（初回の登録・立場や表示名の変更に使う）。

    `org_id` は、**一般の支援者（volunteer）と本人（survivor）に限って**空でよい。
    - 一般の支援者：組織を持たずに登録し、あとから参加コードで組織に入るか、組織を作る（要望1）
    - 本人：住んでいる市区町村にまだ組織がないとき。組織ができたら本人が「支援の組織に
      つなぐ」を押す（要望8・11章の要確認E-1）
    専門職は、必ず組織が決まった状態で呼ばれる（呼ぶ側で組織を決めてから渡す）。

    支援者の連絡先・活動する市区町村・住所（追加修正④）は、確かめてから渡す
    （`validate_contact` など）。新しい版の行を足すときは、前の版の値を引き継いで渡す
    （渡し忘れると、最新の版で空に戻る）。組織の中の権限（`org_permission`。追加修正⑥）も同じ。

    `created_by`・`created_role` は、本人以外（オーナー）が行を足すときだけ渡す
    （渡さなければ本人が足したことにする）。

    施設の職員（使いやすさの改修の計画.md 9章）は、組織と施設の両方が要る。施設のIDは
    施設の参加コードの照合でサーバーが決めた値だけを渡す（要求に書かれた値は渡さない）。
    ほかの立場では施設のIDを持たせない（渡されても空にする）。
    """
    if org_permission not in (None, "", ORG_PERMISSION_OWNER):
        raise UserValidationError(f"組織の権限の値が正しくありません: {org_permission!r}")
    if role not in ROLES:
        raise UserValidationError(f"立場の値が正しくありません: {role!r}")
    if not org_id and role not in (ROLE_VOLUNTEER, ROLE_SURVIVOR):
        raise UserValidationError("組織が決まっていません")
    if role == ROLE_FACILITY_STAFF:
        if not facility_id:
            raise UserValidationError("施設が決まっていません")
    else:
        facility_id = None

    row = {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id or "",
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by or user_id,
        "created_role": created_role or role,
        "user_id": user_id,
        "role": role,
        "profession": profession,
        "display_name": display_name,
        "login_method": login_method,
        "linked_survivor_id": linked_survivor_id,
        "contact": contact or None,
        "municipality_code": municipality_code or None,
        "address": address or None,
        "org_permission": org_permission or None,
        "facility_id": facility_id or None,
    }
    bq.insert("raw_users", row)
    return row


def update_profile(
    *,
    current: dict[str, Any],
    display_name: str,
    profession: str | None,
    contact: str | None,
    municipality_code: str | None,
    address: str | None = None,
) -> dict[str, Any]:
    """表示名・職種・連絡先・活動する市区町村・住所を変える（プロフィールの画面。要望4・追加修正④）。

    立場・組織・ログインの方法は、登録のときの値のまま引き継ぐ（ここでは変えられない。
    変えると見られる記録の範囲が変わるため。要件定義書10-3）。`register_user` と同じ
    「新しい行を足して版を上げる」形にする。
    """
    role = str(current.get("role") or "")
    name = validate_display_name(display_name)
    checked_profession = validate_profession(role, profession)
    checked_contact = validate_contact(contact)
    checked_municipality = validate_activity_municipality(municipality_code)
    checked_address = validate_supporter_address(address)
    return register_user(
        user_id=str(current.get("user_id")),
        org_id=str(current.get("org_id")),
        role=role,
        display_name=name,
        login_method=str(current.get("login_method") or ""),
        profession=checked_profession,
        linked_survivor_id=current.get("linked_survivor_id"),
        contact=checked_contact,
        municipality_code=checked_municipality,
        address=checked_address,
        # 組織の中の権限（追加修正⑥）は、プロフィールでは変えない
        org_permission=current.get("org_permission"),
        # 施設の職員の施設（9章）も、プロフィールでは変えない
        facility_id=current.get("facility_id"),
    )


# 担当の割り当てで読む列（表示名まで。電話番号は保存していない）。
_SUPPORTER_COLUMNS = ("user_id", "org_id", "role", "profession", "display_name")


def fetch_supporter(user_id: str) -> dict[str, Any] | None:
    """支援者を1人読む（担当に割り当てる相手の立場と組織を確かめるため）。

    `fetch_user` と分けてある。あちらは「要求してきた本人」を読むためのもので、
    こちらは**他人**を読む。混ぜると、相手の確認に自分の行を使ってしまう事故が起きる。
    """
    if not user_id:
        return None
    sql = (
        f"SELECT {', '.join(_SUPPORTER_COLUMNS)}\n"
        f"FROM {_staging_table('stg_users')}\n"
        "WHERE user_id = @user_id"
    )
    return bq.query_one(sql, {"user_id": user_id})


def list_by_role(*, org_id: str, role: str | Sequence[str]) -> list[dict[str, Any]]:
    """組織の中の、その立場の利用者の一覧（担当の候補と、記録した人の表示名に使う）。

    `role` は1つでも複数でも渡せる。**複数渡しても読みは1回**（専門職と一般の支援者の
    両方の名前が要る画面で、読みを増やさないため）。`role` の列も返すので、呼ぶ側が
    「担当の候補は一般の支援者だけ」といった選び分けをできる。
    """
    wanted = [role] if isinstance(role, str) else list(role)
    wanted = [item for item in wanted if item in ROLES]
    if not org_id or not wanted:
        return []
    sql = (
        "SELECT user_id, display_name, profession, role\n"
        f"FROM {_staging_table('stg_users')}\n"
        "WHERE org_id = @org_id AND role IN UNNEST(@roles)\n"
        "ORDER BY display_name"
    )
    return bq.query(sql, {"org_id": org_id, "roles": wanted})


def is_owner(row: dict[str, Any] | None) -> bool:
    """組織のオーナーか（追加修正⑥）。組織に入っている専門職で、権限が owner のときだけ。"""
    if not row:
        return False
    return bool(
        row.get("org_id")
        and row.get("role") == ROLE_PROFESSIONAL
        and row.get("org_permission") == ORG_PERMISSION_OWNER
    )


def list_org_professionals(org_id: str) -> list[dict[str, Any]]:
    """組織の専門職の一覧と、オーナーかどうか（追加修正⑥。オーナーの画面だけで使う）。

    連絡先・住所は読まない（オーナーにも返さない。追加修正④）。
    """
    if not org_id:
        return []
    sql = (
        "SELECT user_id, display_name, profession, role, org_permission\n"
        f"FROM {_staging_table('stg_users')}\n"
        "WHERE org_id = @org_id AND role = @role\n"
        "ORDER BY display_name, user_id"
    )
    return bq.query(sql, {"org_id": org_id, "role": ROLE_PROFESSIONAL})


def set_org_owner(
    *,
    target: dict[str, Any],
    owner: bool,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    """オーナーの権限を付ける・外す（追加修正⑥）。新しい版の行を足す（上書きしない）。

    `target` は `fetch_user` で読んだ相手の最新の行。立場・組織・表示名・連絡先などは
    そのまま引き継ぎ、権限だけを変える。呼ぶ側が、同じ組織の専門職であることと、
    最後の1人のオーナーを外さないことを確かめてから呼ぶ。
    """
    if target.get("role") != ROLE_PROFESSIONAL or not target.get("org_id"):
        raise UserValidationError("オーナーの権限は、組織の専門職にだけ付けられます")
    return register_user(
        user_id=str(target.get("user_id")),
        org_id=str(target.get("org_id")),
        role=ROLE_PROFESSIONAL,
        display_name=str(target.get("display_name") or ""),
        login_method=str(target.get("login_method") or ""),
        profession=target.get("profession"),
        linked_survivor_id=target.get("linked_survivor_id"),
        contact=target.get("contact"),
        municipality_code=target.get("municipality_code"),
        address=target.get("address"),
        org_permission=ORG_PERMISSION_OWNER if owner else None,
        created_by=actor_id,
        created_role=actor_role,
    )


def names_by_ids(*, org_id: str, user_ids: Sequence[str]) -> list[dict[str, Any]]:
    """指定した利用者だけの表示名を引く。

    一般の支援者の画面で使う。**組織の利用者の一覧は読まない**（自分の担当だけを見る立場に
    ほかの支援者の一覧を読ませない方針。一覧の絞り込みを返さないのと同じ考え）。
    """
    ids = sorted({str(item) for item in user_ids if item})
    if not org_id or not ids:
        return []
    sql = (
        "SELECT user_id, display_name, role\n"
        f"FROM {_staging_table('stg_users')}\n"
        "WHERE org_id = @org_id AND user_id IN UNNEST(@user_ids)"
    )
    return bq.query(sql, {"org_id": org_id, "user_ids": ids})
