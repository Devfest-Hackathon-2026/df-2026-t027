"""被災者の記録（F1。要件定義書7-3-1の raw_survivors・7-3-2・7-3-3、開発手順書 Step 5）。

- 上書きはしない。基本の情報・段階・居場所が変わるたびに版を1つ足す（要件定義書7-1・10-4）。
  居場所と段階の履歴は `stg_location_history` が版の差から作る。
- 本人が作る記録のIDは、**利用者のIDから決まる値**にする（uuid5）。保存の途中で失敗して
  やり直したときに、同じ人の記録が2件できないようにするため（BigQuery は2つの表を
  まとめて書き戻せないので、やり直しても同じIDになるほうが安全）。
  専門職が登録する記録は uuid4 にする（同じ人を2回登録することが起こりうるため、
  IDでは防げない。二重登録は「氏名と年齢が近い記録を登録の前に知らせる」で防ぐ）。
- 見える範囲は `can_view` が決める（要件定義書10-3）。**本人に返すデータからは、
  悪化の兆しの度合いと訪問・見守りの記録の本文を外す。**
- 一覧の並び順は F4 の訪ねる順番（`int_survivor_list` の `risk_rank` ほか。要件定義書7-3-3）。
- 読みは `int_survivor_list`（7-3-3）から行う。一覧と詳細で同じ1行を使えるため、
  読みの回数を増やさずに済む。
"""

from __future__ import annotations

import datetime as dt
import unicodedata
import uuid
from typing import Any

from backend.config import get_settings
from backend.services import assessments, bq, organizations

# 段階（要件定義書7-3-1）。③④⑤に対応する。
STAGES: dict[str, str] = {
    "evacuation": "③避難生活",
    "temporary": "④仮住まい",
    "rebuilding": "⑤生活再建",
}

# 居場所の種類（要件定義書F1の11種類）。値の正本はここ。画面の選択肢も同じ並びで出す。
LOCATION_TYPES: dict[str, str] = {
    "shelter": "避難所",
    "welfare_shelter": "福祉避難所",
    "home": "自宅",
    "car": "車中泊",
    "relatives": "親せき・知人の家",
    "hotel": "ホテルなどの2次避難先",
    "hospital": "病院",
    "care_facility": "介護施設",
    "temporary_built": "仮設住宅（建設型）",
    "temporary_rented": "賃貸型の仮設住宅",
    "permanent": "恒久住宅（自宅の再建・災害公営住宅など）",
}

# 誰が記録を作ったか（raw_survivors.registered_via）。
VIA_SELF = "self"
VIA_PROFESSIONAL = "professional"
# 家族などが本人の代わりに登録した記録（使いやすさの改修の計画.md 10章）
VIA_PROXY = "proxy"

# 組織がなくても作れる記録（本人の登録と、家族などによる代わりの登録）。
# 年齢・世帯の人数を後で入れてよいのも、この2つ（使いやすさの改修の計画.md 4-2の3・10章）
_ORGLESS_VIAS = (VIA_SELF, VIA_PROXY)

# 代わりの登録での本人の同意（raw_survivors.proxy_consent。使いやすさの改修の計画.md 10-3）。
# 健康・介護の情報は本人の同意なしに集めない決まりがあり、災害の時に命や体を守るため
# 同意を取りにくい場合だけ例外がある。そのため、どちらだったかを選んでもらい記録に残す
PROXY_CONSENTS: dict[str, str] = {
    "obtained": "ご本人の同意を得た",
    "unable": "ご本人が同意できない状態",
}

# 記録のIDを作るときの名前空間（uuid5。固定の文字列で、秘密ではない）。
_SELF_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "gdgoc:survivor:self")

# 入力の上限。間違えて長文や極端な値を入れたときに表に入れない。
FULL_NAME_MAX_LENGTH = 40
CONTACT_MAX_LENGTH = 100
LOCATION_DETAIL_MAX_LENGTH = 100
AGE_MAX = 120
HOUSEHOLD_SIZE_MAX = 30
# 住所（要望8）。今いる場所（location_detail）とは別で、住んでいる市区町村の下の番地など。
ADDRESS_MAX_LENGTH = 100

# 二重登録の知らせで「年齢が近い」とみなす差（歳）。要件定義書F1は数字を決めていないため
# ここで決める。年齢は聞き取りでずれることがあるので、ぴったり一致には絞らない。
SIMILAR_AGE_DIFF = 2

# 版を足すときに、前の版から引き継ぐ列（要件定義書7-3-1の raw_survivors）。
_CARRIED_COLUMNS = (
    "full_name",
    "age",
    "household_size",
    "contact",
    "stage",
    "location_type",
    "location_detail",
    "self_user_id",
    "registered_via",
    # 住んでいる市区町村と住所（要望8）
    "municipality_code",
    "address",
)

# 値があるときだけ引き継ぐ列（使いやすさの改修の計画.md 10章）。代わりの登録の記録にだけ入る。
# 値が空の記録の版には列ごと入れない（09_migrations.sql を流す前でも、ほかの記録の版を書けるように）
_CARRIED_IF_SET = ("proxy_consent",)

# 一覧の1行に出す列（連絡先は入れない。詳細だけで足りるため）。
_LIST_COLUMNS = (
    "survivor_id",
    "full_name",
    "age",
    "household_size",
    "stage",
    "location_type",
    "location_detail",
    "registered_via",
    "registered_at",
    "updated_at",
    "assignee_user_ids",
    "assignee_count",
    "last_visited_at",
    "days_since_last_visit",
    "open_consultations",
    "pending_proposals",
    "never_visited",
    # F6（受けられる支援）の知らせ。`int_survivor_list` が数えた値をそのまま使うので、
    # 一覧とホームで「期限の近い未申請」を出しても BigQuery の読みは増えない
    "suggested_count",
    "unapplied_count",
    "nearest_unapplied_deadline",
    "days_to_nearest_deadline",
)

# 一覧の並び順（要件定義書7-3-3 の `int_survivor_list` の定義と同じ）。
# 悪化の兆しの度合い（高→低）→ 未対応の相談の急ぎの度合い（高→低）→
# 最後の訪問からの日数（長い順。1度も訪問がない人が先）。
# 最後に登録の新しい順を足して、並びが毎回同じになるようにする（同じ度合いの人の順が
# 読むたびに変わると、画面を見比べたときに取り違える）。
_LIST_ORDER = (
    "ORDER BY risk_rank DESC, open_urgency_rank DESC, "
    "days_since_last_visit_sort DESC, registered_at DESC"
)


class SurvivorValidationError(ValueError):
    """被災者の記録の入力が正しくない。"""


def self_survivor_id(user_id: str) -> str:
    """本人が作る記録のID（同じ利用者なら毎回同じ値になる）。"""
    if not user_id:
        raise SurvivorValidationError("利用者のIDがありません")
    return f"sv_{uuid.uuid5(_SELF_ID_NAMESPACE, user_id).hex}"


def _validate_text(value: str | None, *, label: str, max_length: int, required: bool) -> str | None:
    text = (value or "").strip()
    if not text:
        if required:
            raise SurvivorValidationError(f"{label}を入れてください")
        return None
    if len(text) > max_length:
        raise SurvivorValidationError(f"{label}は{max_length}文字以内にしてください")
    return text


def _validate_int(value: Any, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SurvivorValidationError(f"{label}は数字で入れてください")
    if value < minimum or value > maximum:
        raise SurvivorValidationError(f"{label}は{minimum}から{maximum}の間で入れてください")
    return value


def validate_stage(stage: str | None) -> str:
    """段階を確かめて返す。"""
    if stage not in STAGES:
        raise SurvivorValidationError("今の段階を選んでください")
    return stage


def validate_location_type(location_type: str | None) -> str:
    """居場所の種類を確かめて返す。"""
    if location_type not in LOCATION_TYPES:
        raise SurvivorValidationError("今の居場所の種類を選んでください")
    return location_type


def _validate_optional_int(value: Any, *, label: str, minimum: int, maximum: int) -> int | None:
    """空（None）なら None。入っていれば範囲を確かめる（本人の登録の年齢・世帯の人数）。"""
    if value is None:
        return None
    return _validate_int(value, label=label, minimum=minimum, maximum=maximum)


def validate_household_size(household_size: Any) -> int:
    """世帯の人数を確かめて返す（1〜30）。"""
    return _validate_int(
        household_size, label="世帯の人数", minimum=1, maximum=HOUSEHOLD_SIZE_MAX
    )


def validate_address(address: str | None) -> str | None:
    """住所をそろえる（任意。要望8）。"""
    return _validate_text(address, label="住所", max_length=ADDRESS_MAX_LENGTH, required=False)


def validate_proxy_consent(proxy_consent: str | None) -> str:
    """代わりの登録での本人の同意を確かめる（必須。使いやすさの改修の計画.md 10-3）。"""
    value = (proxy_consent or "").strip()
    if value not in PROXY_CONSENTS:
        raise SurvivorValidationError("ご本人の同意について選んでください")
    return value


def validate_municipality_code(municipality_code: str | None, *, required: bool) -> str | None:
    """住んでいる市区町村の団体コードを確かめる（要望8）。

    形の検査は `services/organizations.py` に揃える（組織の受け持ちの検査と同じ規則）。
    専門職が被災者を登録するときは、組織がすでに決まっているため任意にする。
    """
    text = (municipality_code or "").strip()
    if not text:
        if required:
            raise SurvivorValidationError("お住まいの市区町村を選んでください")
        return None
    try:
        return organizations.validate_municipality_code(text)
    except organizations.OrganizationValidationError as exc:
        raise SurvivorValidationError(str(exc)) from exc


def build_survivor_row(
    *,
    survivor_id: str,
    org_id: str,
    created_by: str,
    created_role: str,
    full_name: str,
    age: Any,
    household_size: Any,
    stage: str | None,
    location_type: str | None,
    contact: str | None = None,
    location_detail: str | None = None,
    self_user_id: str | None = None,
    registered_via: str = VIA_SELF,
    municipality_code: str | None = None,
    address: str | None = None,
    proxy_consent: str | None = None,
) -> dict[str, Any]:
    """被災者の記録の版1の行を作る（入力を確かめるだけで、まだ書かない）。

    書く前に確かめられるように分けてある。専門職の登録では、**入力を確かめてから
    確認コードを発行する**（順番が逆だと、入力の誤りのたびに使われないコードが残る）。

    `municipality_code` は、本人の登録では組織を決めるのに使った値を**ここでも確かめて
    入れ直す**（受け取った値をそのまま信じない。要件定義書10-3と同じ考え方）。
    専門職の登録では組織がすでに決まっているため任意（要望8）。

    家族などによる代わりの登録（`VIA_PROXY`）は本人の登録と同じ扱いにし、
    加えて本人の同意（`proxy_consent`）を必須にする（使いやすさの改修の計画.md 10章）。
    """
    # 本人の登録と代わりの登録だけ、組織がなくてもよい（住んでいる市区町村にまだ組織がない。
    # 要望8・11章の要確認E-1・10章）。専門職の登録では必ず組織がある
    if not org_id and registered_via not in _ORGLESS_VIAS:
        raise SurvivorValidationError("組織が決まっていません")
    # 本人の登録・代わりの登録では、年齢・世帯の人数を後で入れてよい（使いやすさの改修の計画.md 4-2の3）。
    # 専門職の登録ではこれまでどおり必須
    check_count = _validate_optional_int if registered_via in _ORGLESS_VIAS else _validate_int

    row = {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "version": 1,
        "full_name": _validate_text(
            full_name, label="氏名", max_length=FULL_NAME_MAX_LENGTH, required=True
        ),
        "age": check_count(age, label="年齢", minimum=0, maximum=AGE_MAX),
        "household_size": check_count(
            household_size, label="世帯の人数", minimum=1, maximum=HOUSEHOLD_SIZE_MAX
        ),
        "contact": _validate_text(
            contact, label="連絡先", max_length=CONTACT_MAX_LENGTH, required=False
        ),
        "stage": validate_stage(stage),
        "location_type": validate_location_type(location_type),
        "location_detail": _validate_text(
            location_detail,
            label="具体的な場所",
            max_length=LOCATION_DETAIL_MAX_LENGTH,
            required=False,
        ),
        "self_user_id": self_user_id,
        "registered_via": registered_via,
        "municipality_code": validate_municipality_code(municipality_code, required=False),
        "address": validate_address(address),
    }
    if registered_via == VIA_PROXY:
        # 代わりの登録だけ列を入れる（ほかの記録には列ごと入れない。_CARRIED_IF_SET を参照）
        row["proxy_consent"] = validate_proxy_consent(proxy_consent)
    return row


def insert_survivor(row: dict[str, Any]) -> dict[str, Any]:
    """作った行を raw_survivors に追記する。"""
    bq.insert("raw_survivors", row)
    return row


def create_survivor(**kwargs: Any) -> dict[str, Any]:
    """版1を作って追記する（本人が自分の記録を作る経路。F0）。"""
    return insert_survivor(build_survivor_row(**kwargs))


def new_survivor_id() -> str:
    """専門職が登録する記録のID（毎回ちがう値）。"""
    return f"sv_{uuid.uuid4().hex}"


# --- 版を足す（居場所・段階の変更、本人とのつなぎ込み） ---


def _next_version(current: dict[str, Any]) -> int:
    version = current.get("version")
    if not isinstance(version, int) or version < 1:
        raise SurvivorValidationError("記録の版が読み取れません")
    return version + 1


def _carried_row(current: dict[str, Any], *, created_by: str, created_role: str) -> dict[str, Any]:
    """前の版を引き継いだ、新しい版の行を作る（変える列はあとで上書きする）。"""
    survivor_id = current.get("survivor_id")
    # 組織がない本人の記録（要望8・要確認E-1）も版を足せるように、組織は空でもよい
    org_id = current.get("org_id") or ""
    if not survivor_id:
        raise SurvivorValidationError("記録が決まっていません")
    row: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "version": _next_version(current),
    }
    for column in _CARRIED_COLUMNS:
        row[column] = current.get(column)
    for column in _CARRIED_IF_SET:
        if current.get(column):
            row[column] = current.get(column)
    return row


def change_location(
    *,
    current: dict[str, Any],
    stage: str | None,
    location_type: str | None,
    location_detail: str | None,
    created_by: str,
    created_role: str,
) -> dict[str, Any]:
    """居場所と段階を変える（前の内容は消さず、版を1つ足す。要件定義書F1）。"""
    row = _carried_row(current, created_by=created_by, created_role=created_role)
    row["stage"] = validate_stage(stage)
    row["location_type"] = validate_location_type(location_type)
    row["location_detail"] = _validate_text(
        location_detail, label="具体的な場所", max_length=LOCATION_DETAIL_MAX_LENGTH, required=False
    )
    bq.insert("raw_survivors", row)
    return row


def change_household_size(
    *,
    current: dict[str, Any],
    household_size: Any,
    created_by: str,
    created_role: str,
) -> dict[str, Any]:
    """世帯の人数を変える（前の内容は消さず、版を1つ足す。要件定義書F6）。

    F6 が「専門職か本人が世帯の人数を入力できるようにする」ことを求めているが、
    F1 にはこの値を変える経路が無いため、F6 から版を足す形にした
    （**要件定義書10-5の表には無い書き込み**。開発ログの要確認に記録する）。
    """
    row = _carried_row(current, created_by=created_by, created_role=created_role)
    row["household_size"] = _validate_int(
        household_size, label="世帯の人数", minimum=1, maximum=HOUSEHOLD_SIZE_MAX
    )
    bq.insert("raw_survivors", row)
    return row


def change_basic(
    *,
    current: dict[str, Any],
    full_name: str,
    age: Any,
    household_size: Any,
    contact: str | None,
    created_by: str,
    created_role: str,
    address: str | None = None,
    optional_counts: bool = False,
) -> dict[str, Any]:
    """氏名・年齢・世帯の人数・連絡先・住所を変える（プロフィールの画面。要望4・8）。

    `optional_counts` が True（本人が自分の記録を直すとき）は、年齢・世帯の人数を空にしてよい
    （本人の登録で後で入れてよいことにしたため。使いやすさの改修の計画.md 4-2の3）。

    前の内容は消さず、版を1つ足す（`change_location` と同じ形）。居場所・段階・
    **住んでいる市区町村はここでは変えない**（市区町村は組織を決めるのに使った値で、
    変えると見られる記録の範囲が変わるため。変えるのは居場所なら `change_location`）。
    """
    row = _carried_row(current, created_by=created_by, created_role=created_role)
    row["full_name"] = _validate_text(
        full_name, label="氏名", max_length=FULL_NAME_MAX_LENGTH, required=True
    )
    check_count = _validate_optional_int if optional_counts else _validate_int
    row["age"] = check_count(age, label="年齢", minimum=0, maximum=AGE_MAX)
    row["household_size"] = check_count(
        household_size, label="世帯の人数", minimum=1, maximum=HOUSEHOLD_SIZE_MAX
    )
    row["contact"] = _validate_text(
        contact, label="連絡先", max_length=CONTACT_MAX_LENGTH, required=False
    )
    row["address"] = validate_address(address)
    bq.insert("raw_survivors", row)
    return row


def change_organization(*, current: dict[str, Any], org_id: str, user_id: str) -> dict[str, Any]:
    """本人が、記録の組織を選び直す（版を1つ足し、組織のIDを入れ替える。追加修正⑦）。

    - 組織がない本人が組織につなぐ・組織を変える・組織を外す（`org_id` が空）のどれも、
      この1つで行う。**上書きはせず、版を足す**（要件定義書10-4）。
    - 見える範囲は `can_view` が**いまの版の組織**で決めるので、版を足した時点で、
      前の組織の専門職からは見えなくなり、新しい組織の専門職から見えるようになる。
    - 前の組織の担当を外すのは呼ぶ側（`routers/account.py`）が先に行う。
    - 組織が実在するかは呼ぶ側が `organizations.fetch_organization` で確かめる。
    """
    new_org_id = (org_id or "").strip()
    if (current.get("org_id") or "") == new_org_id:
        raise SurvivorValidationError("いまと同じ組織です")
    row = _carried_row(current, created_by=user_id, created_role="survivor")
    row["org_id"] = new_org_id
    bq.insert("raw_survivors", row)
    return row


def connect_organization(*, current: dict[str, Any], org_id: str, user_id: str) -> dict[str, Any]:
    """組織がない本人の記録を、組織につなぐ（`change_organization` の、つなぐときだけの形）。"""
    if not org_id:
        raise SurvivorValidationError("組織が決まっていません")
    if current.get("org_id"):
        raise SurvivorValidationError("この記録は、すでに支援の組織につながっています")
    return change_organization(current=current, org_id=org_id, user_id=user_id)


def link_self_user(*, current: dict[str, Any], user_id: str) -> dict[str, Any]:
    """確認コードで本人のアカウントを記録につなぐ（版を1つ足し、本人のIDを入れる）。"""
    if not user_id:
        raise SurvivorValidationError("利用者のIDがありません")
    # この経路を通るのは本人だけ。users を import すると循環するため文字列で持つ。
    row = _carried_row(current, created_by=user_id, created_role="survivor")
    row["self_user_id"] = user_id
    bq.insert("raw_survivors", row)
    return row


# --- 見える範囲（要件定義書10-3） ---


# 家族としてつながれる立場（使いやすさの改修の計画.md 10章）。画面の立場の名前も
# 「支援者（ボランティア・ご家族）」で、家族は一般の支援者として入る。
# users を import すると循環するため文字列で持つ
FAMILY_ROLE = "volunteer"


def is_family(row: dict[str, Any] | None, user_id: str) -> bool:
    """その利用者が、記録の家族としていまつながっているか（`int_survivor_list.family_user_ids`）。"""
    if not row or not user_id:
        return False
    return user_id in (row.get("family_user_ids") or [])


def can_view(
    row: dict[str, Any] | None,
    *,
    role: str | None,
    user_id: str,
    org_id: str | None,
    as_family: bool = False,
) -> bool:
    """その記録を、その立場の人が見てよいかを返す。

    - 本人：その記録の本人の利用者IDが、自分のIDと同じであること。
    - 専門職：その記録の組織のIDが、自分の組織のIDと同じであること。
    - 一般の支援者：組織が同じで、今有効な担当に自分が入っていること。

    `as_family=True` は家族の画面（`routers/family.py`）だけが使う（使いやすさの改修の計画.md 10章）。
    このときは**家族としてつながっているかだけ**で決める（組織は問わない）。
    家族として見られても、専門職や担当の支援者の権限は付かない。立場のAPI（健康・訪問・
    支援など）は今までどおり `as_family=False` で確かめるので、家族というだけでは 404 のまま。
    """
    if not row or not role:
        return False
    if as_family:
        return role == FAMILY_ROLE and is_family(row, user_id)
    if role == "survivor":
        # 本人は組織がなくても自分の記録は見られる（要望8・要確認E-1）
        return bool(row.get("self_user_id")) and row.get("self_user_id") == user_id
    if not org_id:
        return False
    if row.get("org_id") != org_id:
        return False
    if role == "professional":
        return True
    if role == "volunteer":
        return user_id in (row.get("assignee_user_ids") or [])
    return False


# --- 画面に返す形 ---


def _basic(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "survivor_id": row.get("survivor_id"),
        "full_name": row.get("full_name"),
        "age": row.get("age"),
        "household_size": row.get("household_size"),
        "contact": row.get("contact"),
        "stage": row.get("stage"),
        "stage_label": STAGES.get(row.get("stage") or "", ""),
        "location_type": row.get("location_type"),
        "location_type_label": LOCATION_TYPES.get(row.get("location_type") or "", ""),
        "location_detail": row.get("location_detail"),
        # 住んでいる市区町村と住所（要望8。今いる場所とは別）
        "municipality_code": row.get("municipality_code"),
        "address": row.get("address"),
        "registered_at": row.get("registered_at"),
        "updated_at": row.get("updated_at"),
        "version": row.get("version"),
        # 本人のアカウントがつながっているか（担当の同意の取り方が変わる。F1）
        "linked_to_self": bool(row.get("self_user_id")),
        # 家族などが代わりに登録した記録か（使いやすさの改修の計画.md 10章）。本人がつながったときに
        # 「家族が登録した内容」を確かめて直せるよう、本人にも返す
        "registered_by_family": row.get("registered_via") == VIA_PROXY,
    }


def detail_payload(
    row: dict[str, Any],
    *,
    role: str,
    user_id: str,
    health: dict[str, Any] | None = None,
    visits: list[dict[str, Any]] | None = None,
    consultations: list[dict[str, Any]] | None = None,
    handovers: list[dict[str, Any]] | None = None,
    supports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """詳細の画面に返す1件分。立場で範囲を変える（要件定義書F1・10-3）。

    F7 はまだ無いので、枠だけ空で返す（画面を先に作っておけるようにする）。

    `supports`（F6）は**本人と同じ組織の専門職にだけ**返す（要件定義書601行の
    「見られる人」。一般の支援者は支援制度を扱わない＝446行）。呼ぶ側が立場で
    読みそのものを分けるため、ここでは渡されたものをそのまま入れる。

    `health`（F3）と `consultations`（F8）は本人にも支援者にも返す。
    相談の中身は立場ごとに `services/consultations.py` が組み立てる（**本人には急ぎの
    度合いと根拠を入れない**）。`visits`（F2）は**本人には返さない**
    （訪問・見守りの記録の本文を本人向けのデータから取り除く。要件定義書F4）。

    `handovers`（F3の引き継ぎシート）も**本人に返す**（要件定義書7-1の「見られる人」は
    本人・同じ組織の専門職・担当の一般の支援者）。中身は要約と気をつけることで、
    悪化の兆しの度合いのように本人に見せない情報は入っていない。
    """
    payload = _basic(row)
    payload["role_view"] = role
    # 健康・支援・相談は、本人にも支援者にも見せる枠（F3・F6・F8）
    payload["health"] = health
    payload["supports"] = supports or []
    payload["consultations"] = consultations or []
    # 引き継ぎシート（F3）は本人も見られる（要件定義書7-1）
    payload["handovers"] = handovers or []

    if role == "survivor":
        # 悪化の兆しの度合いと、訪問・見守りの記録の本文は入れない（要件定義書10-3）
        return payload

    payload["org_id"] = row.get("org_id")
    payload["registered_via"] = row.get("registered_via")
    # 代わりの登録での本人の同意と、家族がつながっているか（使いやすさの改修の計画.md 10章）。
    # 家族の名前や連絡先そのものは返さない（専門職が今まで見られた範囲を広げない）
    payload["proxy_consent_label"] = PROXY_CONSENTS.get(row.get("proxy_consent") or "", "")
    payload["has_family"] = bool(row.get("family_user_ids"))
    # 悪化の兆し（F4）。根拠と断り書きも含めて `services/assessments.py` が組み立てる
    payload["risk"] = assessments.risk_payload(row)
    payload["visits"] = visits or []
    payload["watch"] = {
        "last_visited_at": row.get("last_visited_at"),
        "days_since_last_visit": row.get("days_since_last_visit"),
        "visits_30d": row.get("visits_30d"),
        "pending_proposals": row.get("pending_proposals"),
        "open_consultations": row.get("open_consultations"),
        "max_open_urgency": row.get("max_open_urgency"),
    }
    return payload


def list_item(row: dict[str, Any], *, role: str) -> dict[str, Any]:
    """一覧の1行（連絡先は入れない）。"""
    item = {column: row.get(column) for column in _LIST_COLUMNS}
    item["stage_label"] = STAGES.get(row.get("stage") or "", "")
    item["location_type_label"] = LOCATION_TYPES.get(row.get("location_type") or "", "")
    item["linked_to_self"] = bool(row.get("self_user_id"))
    item["registered_by_family"] = row.get("registered_via") == VIA_PROXY
    if role != "survivor":
        # 悪化の兆しは本人には入れない（要件定義書F4の最後・10-3）。
        # 根拠も一覧に入れる（訪ねる順番を決めるときに、詳細を開かずに理由が分かるようにする）
        item["risk_level"] = row.get("risk_level")
        item["risk_level_label"] = assessments.level_label(row.get("risk_level"))
        item["risk_evidence"] = assessments.evidence_items(row.get("risk_evidence"))
        item["risk_next_action"] = row.get("risk_next_action")
        item["risk_source"] = row.get("risk_source")
        item["max_open_urgency"] = row.get("max_open_urgency")
    return item


def attention(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """ホームに出す「対応が必要な人」（件数と名前。要件定義書F4）。

    **一覧の読みをそのまま使う**（ホーム専用のAPIを増やさず、BigQuery の読みも増やさない）。
    一般の支援者に渡す行は担当の人だけに絞られているので、ここで立場を見る必要はない。
    """
    items = [assessments.attention_item(row) for row in rows if assessments.needs_attention(row)]
    return {
        "count": len(items),
        "survivors": items,
        "disclaimer": assessments.DISCLAIMER,
    }


# --- 読み ---


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


def fetch_survivor(survivor_id: str) -> dict[str, Any] | None:
    """被災者の記録を1件読む（見える範囲の判定は `can_view` が行う）。

    組織で絞らずに読む。見てよいかを判定したあと、**見られないものは 404 にする**ため
    （存在するかどうかも分からないようにする。要件定義書10-3）。
    """
    if not survivor_id:
        return None
    sql = (
        "SELECT *\n"
        f"FROM {_staging_table('int_survivor_list')}\n"
        "WHERE survivor_id = @survivor_id"
    )
    return bq.query_one(sql, {"survivor_id": survivor_id})


def list_survivors(
    *,
    org_id: str,
    stage: str | None = None,
    location_type: str | None = None,
    assignee_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """被災者の一覧（組織で絞る。段階・居場所の種類・担当で絞り込める）。

    並び順は **F4 の訪ねる順番**（悪化の兆しの度合いが高い人から。要件定義書F1・F4）。
    順位の列（`risk_rank` ほか）は `int_survivor_list` が持っているので、ここでは並べるだけ。
    """
    where = ["org_id = @org_id"]
    params: dict[str, Any] = {"org_id": org_id}
    if stage:
        where.append("stage = @stage")
        params["stage"] = validate_stage(stage)
    if location_type:
        where.append("location_type = @location_type")
        params["location_type"] = validate_location_type(location_type)
    if assignee_user_id:
        where.append("@assignee_user_id IN UNNEST(assignee_user_ids)")
        params["assignee_user_id"] = assignee_user_id
    conditions = " AND ".join(where)
    sql = (
        "SELECT *\n"
        f"FROM {_staging_table('int_survivor_list')}\n"
        f"WHERE {conditions}\n"
        + _LIST_ORDER
    )
    return bq.query(sql, params)


def location_history(*, survivor_id: str, org_id: str | None = None) -> list[dict[str, Any]]:
    """居場所と段階の履歴（新しいものを先に返す）。

    **被災者のIDだけで読む**（追加修正⑦）。本人が組織を変えても、前の組織のときの履歴が
    消えないようにするため。見てよいかは、呼ぶ側が `shared.load_visible` で先に確かめる。
    `org_id` は前の呼び方との互換のために受け取るだけで、読みには使わない。
    """
    del org_id
    sql = (
        "SELECT version, changed_at, stage, location_type, location_detail,\n"
        "       prev_stage, prev_location_type, prev_location_detail, changed_role\n"
        f"FROM {_staging_table('stg_location_history')}\n"
        "WHERE survivor_id = @survivor_id\n"
        # 書いた時刻の新しい順（登録し直すと版の番号が1に戻るため。使いやすさの改修の計画.md 5章）
        "ORDER BY changed_at DESC, version DESC"
    )
    rows = bq.query(sql, {"survivor_id": survivor_id})
    for row in rows:
        row["stage_label"] = STAGES.get(row.get("stage") or "", "")
        row["location_type_label"] = LOCATION_TYPES.get(row.get("location_type") or "", "")
        row["prev_location_type_label"] = LOCATION_TYPES.get(
            row.get("prev_location_type") or "", ""
        )
    return rows


# --- 二重登録の知らせ（要件定義書F1の最後） ---


def normalize_name(name: str | None) -> str:
    """氏名を比べられる形にそろえる（全角と半角・空白の違いを無くす）。"""
    text = unicodedata.normalize("NFKC", name or "")
    return "".join(text.split()).casefold()


def find_similar(*, org_id: str, full_name: str, age: Any) -> list[dict[str, Any]]:
    """氏名が同じで年齢が近い記録を探す（登録の前に知らせるため）。

    年齢の幅は SQL で絞り、氏名は Python で比べる（全角と半角・空白の違いを無くすため）。

    家族などが代わりに登録した記録は、年齢が空でも候補に入れる（代わりの登録では年齢を
    後で入れてよいため。使いやすさの改修の計画.md 10章の「訪問者と家族をつなぐ」）。
    候補には `registered_by_family` を付け、画面で「家族が登録した記録です」と出す。
    """
    if not org_id or isinstance(age, bool) or not isinstance(age, int):
        return []
    target = normalize_name(full_name)
    if not target:
        return []
    sql = (
        "SELECT survivor_id, full_name, age, household_size, stage, location_type,\n"
        "       location_detail, registered_at, registered_via\n"
        f"FROM {_staging_table('stg_survivors')}\n"
        "WHERE org_id = @org_id\n"
        "  AND (age BETWEEN @age_min AND @age_max\n"
        "       OR (age IS NULL AND registered_via = @via_proxy))\n"
        "ORDER BY registered_at DESC"
    )
    rows = bq.query(
        sql,
        {
            "org_id": org_id,
            "age_min": age - SIMILAR_AGE_DIFF,
            "age_max": age + SIMILAR_AGE_DIFF,
            "via_proxy": VIA_PROXY,
        },
    )
    similar: list[dict[str, Any]] = []
    for row in rows:
        if normalize_name(row.get("full_name")) != target:
            continue
        similar.append(
            {
                "survivor_id": row.get("survivor_id"),
                "full_name": row.get("full_name"),
                "age": row.get("age"),
                "stage": row.get("stage"),
                "stage_label": STAGES.get(row.get("stage") or "", ""),
                "location_type_label": LOCATION_TYPES.get(row.get("location_type") or "", ""),
                "location_detail": row.get("location_detail"),
                "registered_at": row.get("registered_at"),
                "registered_via": row.get("registered_via"),
                "registered_by_family": row.get("registered_via") == VIA_PROXY,
            }
        )
    return similar
