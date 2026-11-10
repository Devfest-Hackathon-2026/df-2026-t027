"""F0 ログインと立場の API 5本（要件定義書10-5、開発手順書 Step 4-3）。

- `GET  /api/public-config`     Firebase のウェブ用の設定と画面の選択肢（ログイン前）
- `GET  /api/organizations`     本人が選ぶ市町（組織）の一覧
- `POST /api/onboarding/survivor`  本人として登録する
- `POST /api/onboarding/supporter` 参加コードで支援者として登録する
- `GET  /api/me`                立場・組織・（本人なら）自分の記録のID
- `POST /api/me/organization`   本人が支援の組織を選び直す（つなぐ・変える・外す。追加修正⑦）

組織のオーナーだけが使うAPI（追加修正⑥。`auth.require_owner`）：

- `GET  /api/organizations/join-codes`  自分の組織の参加コードを見る
- `POST /api/organizations/join-codes`  参加コードを発行し直す（前のコードは使えなくなる）
- `GET  /api/organizations/members`     自分の組織の専門職と、オーナーかどうか
- `POST /api/organizations/owners`      同じ組織の専門職に、オーナーの権限を付ける・外す

施設の職員（使いやすさの改修の計画.md 9章）：

- 支援者の登録（`POST /api/onboarding/supporter`）と、組織に入る（`POST /api/me/join`）は、
  組織の参加コードに合わなければ**施設の参加コード**として照合する。合えば施設の職員になる。
  施設と組織は、コードの照合でサーバーが決める（画面は立場も施設も送らない）。

守ること：

- 立場と組織は `stg_users` から決める。要求に書かれた立場や組織のIDは使わない（要件定義書10-3）。
  本人の登録で受け取る `org_id` だけは例外で、まだ立場がない人が**自分で選ぶ**値のため受け取る。
  受け取ったあとに `stg_organizations` に実在するかを確かめる。
- 参加コードと確認コードの値、電話番号、氏名はログに出さない（要件定義書10-2の6・11-3）。
  支援者の連絡先・住所（追加修正④）もログに出さず、本人の `/api/me` にだけ返す。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from backend import auth
from backend.config import get_settings
from backend.services import (
    assessments,
    assignments,
    bq,
    consultations,
    facilities,
    handovers,
    health,
    link_codes,
    organizations,
    supports,
    survivors,
    users,
    visits,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["F0"])

# ローカルで開いたと見なすホスト名。ここでは Google ログインを popup で行う
# （本番は redirect。iPhone の Safari 対策。要件定義書10-2の5）。
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


# --- 要求の形 ---


class SurvivorOnboardingRequest(BaseModel):
    """本人として登録する（住んでいる市区町村を選んで記録を作る／確認コードで既存の記録とつなぐ）。

    支援の組織は**本人が選ぶ（任意。追加修正⑦）**。画面は、住んでいる市区町村を受け持つ
    組織を上に出し、全国の組織から名前で探して選べるようにする。空なら組織なしで登録する
    （組織がなくても本人向けの機能は使える。追加修正②）。
    """

    # 住んでいる市区町村の団体コード（要望8。全国の一覧は02_app/frontend/data/municipalities.json）。
    municipality_code: str = Field(default="", description="住んでいる市区町村の団体コード（6桁）")
    # 本人が選んだ支援の組織のID（任意。追加修正⑦）。実在するかはサーバーで確かめる。
    org_id: str = Field(default="", description="選んだ支援の組織のID（空なら組織なし）")
    full_name: str = Field(default="", description="氏名")
    age: int | None = Field(default=None, description="年齢")
    household_size: int | None = Field(default=None, description="世帯の人数")
    contact: str | None = Field(default=None, description="連絡先（任意）")
    address: str | None = Field(default=None, description="住所（任意）")
    stage: str | None = Field(default=None, description="今の段階")
    location_type: str | None = Field(default=None, description="今の居場所の種類")
    location_detail: str | None = Field(default=None, description="具体的な場所（任意）")
    # 確認コードでつなぐ経路。照合は Step 5 で作る（開発手順書 Step 4-4）。
    link_code: str | None = Field(default=None, description="支援者から渡された確認コード")


class SupporterOnboardingRequest(BaseModel):
    """参加コードで支援者として登録する（コードで専門職か一般かを決める）。

    施設の参加コードなら、施設の職員として登録する（使いやすさの改修の計画.md 9章）。
    """

    join_code: str = Field(default="", description="組織の参加コード")
    display_name: str = Field(default="", description="表示名")
    profession: str | None = Field(default=None, description="職種（専門職だけ）")
    # 追加修正④。連絡先と活動する市区町村は必須、住所は任意
    contact: str | None = Field(default=None, description="連絡先（電話番号など）")
    municipality_code: str | None = Field(default=None, description="活動する市区町村の団体コード")
    address: str | None = Field(default=None, description="住所（任意）")


class ProfileUpdateRequest(BaseModel):
    """支援者が、表示名・職種・連絡先・活動する市区町村・住所を変える（要望4・追加修正④）。"""

    display_name: str = Field(default="", description="表示名")
    profession: str | None = Field(default=None, description="職種（専門職だけ）")
    contact: str | None = Field(default=None, description="連絡先（電話番号など）")
    municipality_code: str | None = Field(default=None, description="活動する市区町村の団体コード")
    address: str | None = Field(default=None, description="住所（任意）")


class JoinOrganizationRequest(BaseModel):
    """組織がない支援者が、参加コードで組織に入る（要望1）。"""

    join_code: str = Field(default="", description="組織の参加コード")
    profession: str | None = Field(default=None, description="職種（専門職用のコードのときだけ必須）")


class SurvivorOrganizationRequest(BaseModel):
    """本人が、支援の組織を選び直す（つなぐ・変える・外す。追加修正⑦）。"""

    # 空なら組織を外す。実在するかはサーバーで確かめる。
    org_id: str = Field(default="", description="選んだ支援の組織のID（空なら組織を外す）")


class OwnerChangeRequest(BaseModel):
    """オーナーの権限を付ける・外す（追加修正⑥）。相手は同じ組織の専門職だけ。"""

    user_id: str = Field(default="", description="権限を変える専門職の利用者のID")
    owner: bool = Field(default=True, description="true で付ける、false で外す")


class OrganizationCreateRequest(BaseModel):
    """組織がない支援者が、組織を作る（要望2）。作った人はその組織の専門職になる。"""

    org_name: str = Field(default="", description="組織の名前")
    prefecture: str = Field(default="", description="都道府県（市区町村の一覧から引いた名前）")
    municipality_code: str = Field(default="", description="受け持つ市区町村の団体コード")
    profession: str | None = Field(default=None, description="職種")


# --- 応答の組み立て ---


def _choices() -> dict[str, list[dict[str, str]]]:
    """画面の選択肢。値の正本はバックエンド側に置き、画面はこれを使う。

    要件定義書10-5 は `/api/public-config` を「Firebase のウェブ用の設定」としているが、
    段階・居場所の種類・職種（F0・F1）と、F2の9つの項目・F3の項目・要介護度を画面にも書くと
    二重管理になり、ずれたときにデモで気づく。APIを増やさず、ここに相乗りさせた
    （開発ログの要確認11に記録する）。
    """
    return {
        "stages": [{"value": value, "label": label} for value, label in survivors.STAGES.items()],
        "location_types": [
            {"value": value, "label": label} for value, label in survivors.LOCATION_TYPES.items()
        ],
        "professions": [{"value": name, "label": name} for name in users.PROFESSIONS],
        # 専門職でない支援者が選ぶ「該当なし」（追加修正③）。支援者の登録・組織に入る画面だけで使う
        "no_profession": {"value": users.NO_PROFESSION, "label": users.NO_PROFESSION},
        # F2 の9つの項目（画面の見出し。並びもここが正本。要件定義書F2）
        "visit_fields": [
            {"value": value, "label": label} for value, label in visits.VISIT_FIELDS.items()
        ],
        # F3 の項目と要介護度（要件定義書F3。要介護度の値は要件定義書が決めていないため、
        # 介護保険の区分に合わせて `services/health.py` で決めた）
        "health_fields": [
            {
                "value": name,
                "label": health.FIELD_LABELS[name],
                "multi": name in health.ARRAY_FIELDS,
            }
            for name in health.FIELD_ORDER
        ],
        "care_levels": [{"value": name, "label": name} for name in health.CARE_LEVELS],
        # F4 の悪化の兆しの度合い（専門職が変えるときの選択肢。要件定義書F4）
        "risk_levels": [
            {"value": value, "label": label} for value, label in assessments.LEVELS.items()
        ],
        # F8 の相談の種類・状況・急ぎの度合い（要件定義書F8。値の正本は
        # `services/consultations.py`。画面に書き写すとずれる）
        "consultation_categories": [
            {"value": value, "label": label} for value, label in consultations.CATEGORIES.items()
        ],
        "consultation_statuses": [
            {"value": value, "label": label} for value, label in consultations.STATUSES.items()
        ],
        # 送る前に必ず出す案内（文言の正本は services/consultations.py）
        "consultation_emergency_guide": consultations.EMERGENCY_GUIDE,
        # 組織がない本人の相談は公開になる。送る前に出す知らせ（追加修正②）
        "consultation_public_notice": consultations.PUBLIC_NOTICE,
        "consultation_urgency_levels": [
            {"value": value, "label": label}
            for value, label in consultations.URGENCY_LEVELS.items()
        ],
        # F5 の施設の種類と対応できる介護・医療（要件定義書F5。**要件定義書は値を決めて
        # いない**ため `services/facilities.py` で決めた。画面に書き写すとずれる）
        "facility_types": [
            {"value": value, "label": label} for value, label in facilities.FACILITY_TYPES.items()
        ],
        "facility_capabilities": [
            {"value": value, "label": label} for value, label in facilities.CAPABILITIES.items()
        ],
        # 最終更新から24時間以上たった施設に出す知らせ（文言の正本は services/facilities.py）
        "facility_stale_notice": facilities.STALE_NOTICE,
        # F3 の引き継ぎシートに必ず出す断り書き（文言の正本は services/handovers.py）
        "handover_ai_disclaimer": handovers.AI_DISCLAIMER,
        # F6 の住宅の被害の程度・再建の方法の意向・申請の状況（要件定義書F6。
        # **要件定義書は被害の程度と意向の値を決めていない**ため `services/supports.py`
        # で決めた。申請の状況4つは要件定義書が決めている値）
        "housing_damages": [
            {"value": value, "label": label} for value, label in supports.HOUSING_DAMAGES.items()
        ],
        "rebuild_intents": [
            {"value": value, "label": label} for value, label in supports.REBUILD_INTENTS.items()
        ],
        "support_statuses": [
            {"value": value, "label": label} for value, label in supports.STATUSES.items()
        ],
        # 本人の画面に出す「次にすること」と、必ず出す断り書き（文言の正本は
        # services/supports.py。画面に書き写すとずれる）
        "support_next_actions": [
            {"value": value, "label": label} for value, label in supports.NEXT_ACTIONS.items()
        ],
        "support_disclaimer": supports.DISCLAIMER,
        "support_deadline_soon_days": supports.DEADLINE_SOON_DAYS,
    }


def _me_payload(user: auth.CurrentUser) -> dict[str, object]:
    return {
        "user_id": user.uid,
        "login_method": user.login_method,
        # 立場を選んでいれば true（組織がなくても。要望1）。画面はこれでホームに進む
        "registered": user.has_role,
        # 組織に入っているか（要望1）。false なら、ホームでは「組織に入る」「組織を作る」だけができる
        "has_org": user.has_org,
        "role": user.role,
        "org_id": user.org_id,
        "org_name": user.org_name,
        "profession": user.profession,
        "display_name": user.display_name,
        "survivor_id": user.linked_survivor_id,
        # 支援者の連絡先・活動する市区町村・住所（追加修正④）。この応答は本人にだけ返る
        "contact": user.contact,
        "municipality_code": user.municipality_code,
        "address": user.address,
        # 必須の項目が空の支援者（追加修正④より前に登録した人）。ホームで入力を案内する
        # 組織のオーナーか（追加修正⑥）。true のときだけ、プロフィールに参加コードの欄を出す
        "is_owner": user.is_owner,
        # 施設の職員の、自分の施設のID（使いやすさの改修の計画.md 9章）。ほかの立場では空
        "facility_id": user.facility_id,
        "profile_incomplete": users.is_profile_incomplete(
            {
                "role": user.role,
                "contact": user.contact,
                "municipality_code": user.municipality_code,
                "address": user.address,
            }
        ),
    }


def _supporter_details(user: auth.CurrentUser) -> dict[str, str | None]:
    """支援者の連絡先・活動する市区町村・住所（追加修正④）。

    組織に入る・組織を作るときも raw_users に新しい版を足す。前の版の値を引き継がないと、
    最新の版で空に戻るため、ここで取り出して渡す。
    """
    return {
        "contact": user.contact,
        "municipality_code": user.municipality_code,
        "address": user.address,
    }


def _ensure_not_registered(user: auth.CurrentUser) -> None:
    # 立場だけ選んでいれば「登録済み」とする（組織がなくても。要望1）。
    # 組織に入る・組織を作るのは別のAPI（/api/me/join・/api/organizations）を使う。
    if user.has_role:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="すでに登録が済んでいます",
        )


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _resolve_any_join_code(join_code: str) -> tuple[str, str, str | None] | None:
    """参加コードから（組織のID, 立場, 施設のID）を決める。合わなければ None。

    先に組織の参加コード（専門職用・一般の支援者用）を照合し、合わなければ施設の参加コード
    （使いやすさの改修の計画.md 9章）を照合する。施設のコードに合えば、立場は施設の職員、
    組織はその施設を登録した組織になる。どれもサーバーが決める値で、要求からは受け取らない。

    組織のコードに合ったときは、施設のコードを照合しない（読みを1回減らす）。コードは
    8文字のランダムな値で、ソルトも別（組織のIDと施設のID）なので、両方に合うことは
    まず起きない（要確認として開発ログに書く）。
    """
    try:
        resolved = organizations.resolve_join_code(join_code)
        if resolved is not None:
            org_id, role = resolved
            return org_id, role, None
        facility_match = facilities.resolve_staff_join_code(join_code)
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    if facility_match is None:
        return None
    facility_id, org_id = facility_match
    return org_id, users.ROLE_FACILITY_STAFF, facility_id


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="保存できませんでした。通信を確かめて、もう一度お試しください",
    )


# --- API ---


@router.get("/public-config")
async def public_config(request: Request) -> dict[str, object]:
    """Firebase のウェブ用の設定を返す（ログイン前。ここに秘密情報はない）。

    `authDomain` とログインの方法は、開いているドメインで変える（要件定義書10-2の5）。

    - ローカル（localhost）：Firebase のドメインをそのまま使い、popup でログインする。
    - 本番：**アプリのドメイン**を `authDomain` にし、redirect でログインする。
      ログイン用のページを別ドメインに置くと、スマートフォンのブラウザが保存領域を
      分けるため「missing initial state」で失敗する（練習で発生）。
      `/__/auth/*` はアプリが Firebase へ中継する（`routers/auth_proxy.py`）。
    """
    settings = get_settings()
    hostname = (request.headers.get("host") or "").split(":")[0].lower()
    is_local = hostname in LOCAL_HOSTNAMES
    host_header = request.headers.get("host") or settings.firebase_auth_domain
    return {
        "app_name": settings.app_name,
        "login_mode": "popup" if is_local else "redirect",
        "firebase": {
            "apiKey": settings.firebase_api_key,
            "authDomain": settings.firebase_auth_domain if is_local else host_header,
            "projectId": settings.project_id,
            "appId": settings.firebase_app_id,
        },
        "choices": _choices(),
    }


@router.get("/organizations")
async def list_organizations(
    _user: auth.AuthenticatedUser = Depends(auth.authenticated_user),
) -> dict[str, object]:
    """組織の一覧を返す（参加コードのハッシュは返さない）。"""
    try:
        rows = organizations.list_organizations()
    except bq.BigQueryUnavailable as exc:
        logger.warning("organizations_failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="市町の一覧を読めませんでした。少し待ってやり直してください",
        ) from exc
    return {"organizations": rows}


@router.get("/organizations/by-municipality")
async def organizations_by_municipality(
    municipality_code: str,
    _user: auth.AuthenticatedUser = Depends(auth.authenticated_user),
) -> dict[str, object]:
    """その市区町村を受け持つ組織を返す（要望8）。本人の登録画面が、市区町村を選んだ
    あとにこれを呼び、0件・1件・複数件のどれかで案内を変える。
    """
    try:
        code = organizations.validate_municipality_code(municipality_code)
    except organizations.OrganizationValidationError as exc:
        raise _bad_request(str(exc)) from exc
    try:
        rows = organizations.fetch_by_municipality(code)
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    return {"organizations": rows}


def _chosen_organization(chosen_org_id: str) -> dict[str, object] | None:
    """本人が選んだ支援の組織を確かめる（追加修正⑦）。空なら None（組織なし）。

    受け取った `org_id` をそのまま信じない。`stg_organizations` に実在するかを確かめ、
    無ければ 400 にする（要件定義書10-3と同じ「要求に書かれた値は使わない」考え方）。
    全国のどの組織でも選べる（住んでいる市区町村を受け持つ組織に限らない）。
    """
    chosen_id = (chosen_org_id or "").strip()
    if not chosen_id:
        return None
    try:
        organization = organizations.fetch_organization(chosen_id)
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    if organization is None:
        raise _bad_request("選んだ支援の組織が見つかりません。一覧から選び直してください")
    return {**organization, "org_id": chosen_id}


def _link_with_code(link_code: str, user: auth.CurrentUser) -> dict[str, object]:
    """確認コードで、すでに作られている記録に本人のアカウントをつなぐ（F0・F1）。

    書く順番は「記録の版 → 利用者の行 → コードを使用済みにする」。使用済みにするのを
    最後にすると、途中で落ちてもコードがまだ使えるのでやり直せる。二重につながるのは、
    つなぐ前に `self_user_id` を確かめることで防ぐ（コードの無効化だけに頼らない）。
    """
    try:
        resolved = link_codes.resolve_code(link_code, user_id=user.uid)
    except link_codes.TooManyAttempts as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    if resolved is None:
        # どの記録のコードだったかは返さない（当てられる手がかりを増やさない）
        raise _bad_request("確認コードが違います。支援者から渡された紙を確かめてください")

    try:
        current = survivors.fetch_survivor(resolved.survivor_id)
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    if current is None:
        raise _bad_request(
            "このコードの記録が見つかりません。お手数ですが支援者にお知らせください"
        )
    if current.get("self_user_id"):
        raise _bad_request("この記録には、すでに別のアカウントがつながっています")

    org_id = resolved.org_id or str(current.get("org_id") or "")
    try:
        record = survivors.link_self_user(current=current, user_id=user.uid)
        users.register_user(
            user_id=user.uid,
            org_id=org_id,
            role=users.ROLE_SURVIVOR,
            display_name=str(record.get("full_name") or ""),
            login_method=user.login_method,
            linked_survivor_id=resolved.survivor_id,
        )
        link_codes.mark_used(resolved, used_by=user.uid)
    except survivors.SurvivorValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except users.UserValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    logger.info("onboarding_done: role=%s via=link_code", users.ROLE_SURVIVOR)
    return _me_payload(
        auth.CurrentUser(
            uid=user.uid,
            login_method=user.login_method,
            role=users.ROLE_SURVIVOR,
            org_id=org_id,
            # 組織の名前はここでは読まない（読みを1回増やさない）。画面は /api/me で補う
            org_name=None,
            display_name=str(record.get("full_name") or ""),
            linked_survivor_id=resolved.survivor_id,
        )
    )


@router.post("/onboarding/survivor", status_code=status.HTTP_201_CREATED)
async def onboard_survivor(
    body: SurvivorOnboardingRequest,
    user: auth.CurrentUser = Depends(auth.current_user),
) -> dict[str, object]:
    """本人として登録する。自分の記録（版1）を作る（要望8・追加修正⑦）。

    住んでいる市区町村は必ず選んでもらう。支援の組織は任意で、選ばなければ組織なしで
    登録する（組織なしでも本人向けの機能は使える。追加修正②）。あとからプロフィールで
    選び直せる（`POST /api/me/organization`）。
    """
    _ensure_not_registered(user)

    if body.link_code:
        return _link_with_code(body.link_code, user)

    try:
        organizations.validate_municipality_code(body.municipality_code)
    except organizations.OrganizationValidationError as exc:
        raise _bad_request(str(exc)) from exc
    organization = _chosen_organization(body.org_id)
    org_id = str(organization.get("org_id")) if organization else ""

    survivor_id = survivors.self_survivor_id(user.uid)
    try:
        record = survivors.create_survivor(
            survivor_id=survivor_id,
            org_id=org_id,
            created_by=user.uid,
            created_role=users.ROLE_SURVIVOR,
            full_name=body.full_name,
            age=body.age,
            household_size=body.household_size,
            contact=body.contact,
            stage=body.stage,
            location_type=body.location_type,
            location_detail=body.location_detail,
            self_user_id=user.uid,
            registered_via=survivors.VIA_SELF,
            municipality_code=body.municipality_code,
            address=body.address,
        )
    except survivors.SurvivorValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    try:
        users.register_user(
            user_id=user.uid,
            org_id=org_id,
            role=users.ROLE_SURVIVOR,
            # 本人には表示名を別に聞かない（F0は支援者にだけ聞く）。氏名をそのまま使う。
            display_name=str(record["full_name"]),
            login_method=user.login_method,
            linked_survivor_id=survivor_id,
        )
    except users.UserValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        # 記録は書けたが利用者の行が書けなかった。記録のIDは利用者のIDから決まるので、
        # やり直しても同じ記録になる（二重に作られない）。
        logger.warning("onboarding_survivor_user_insert_failed")
        raise _unavailable() from exc

    logger.info(
        "onboarding_done: role=%s via=%s has_org=%s",
        users.ROLE_SURVIVOR,
        survivors.VIA_SELF,
        bool(org_id),
    )
    return _me_payload(
        auth.CurrentUser(
            uid=user.uid,
            login_method=user.login_method,
            role=users.ROLE_SURVIVOR,
            org_id=org_id,
            org_name=(organization or {}).get("org_name"),
            display_name=str(record["full_name"]),
            linked_survivor_id=survivor_id,
        )
    )


@router.post("/onboarding/supporter", status_code=status.HTTP_201_CREATED)
async def onboard_supporter(
    body: SupporterOnboardingRequest,
    user: auth.CurrentUser = Depends(auth.current_user),
) -> dict[str, object]:
    """参加コードで支援者として登録する（コードで専門職か一般の支援者かを決める）。

    参加コードを入れなければ、**組織を持たない一般の支援者**として登録する（要望1）。
    組織がない支援者は記録に触れない（`auth.require_roles` が組織を求める）。
    できるのは、プロフィールの変更・参加コードで組織に入る・組織を作るだけ
    （`PATCH /api/me`・`POST /api/me/join`・`POST /api/organizations`）。
    """
    _ensure_not_registered(user)

    try:
        display_name = users.validate_display_name(body.display_name)
        details = {
            "contact": users.validate_contact(body.contact),
            "municipality_code": users.validate_activity_municipality(body.municipality_code),
            "address": users.validate_supporter_address(body.address),
        }
    except users.UserValidationError as exc:
        raise _bad_request(str(exc)) from exc

    join_code = (body.join_code or "").strip()
    if not join_code:
        try:
            users.register_user(
                user_id=user.uid,
                org_id="",
                role=users.ROLE_VOLUNTEER,
                display_name=display_name,
                login_method=user.login_method,
                **details,
            )
        except users.UserValidationError as exc:
            raise _bad_request(str(exc)) from exc
        except bq.BigQueryUnavailable as exc:
            raise _unavailable() from exc

        logger.info("onboarding_done: role=%s via=no_organization", users.ROLE_VOLUNTEER)
        return _me_payload(
            auth.CurrentUser(
                uid=user.uid,
                login_method=user.login_method,
                role=users.ROLE_VOLUNTEER,
                org_id="",
                display_name=display_name,
                **details,
            )
        )

    resolved = _resolve_any_join_code(join_code)
    if resolved is None:
        # 「どの組織のコードか」は返さない（当てられる手がかりを増やさない）。
        raise _bad_request("参加コードが違います。配られたコードを確かめてください")
    org_id, role, facility_id = resolved

    try:
        profession = users.validate_profession(role, body.profession)
    except users.UserValidationError as exc:
        raise _bad_request(str(exc)) from exc

    try:
        users.register_user(
            user_id=user.uid,
            org_id=org_id,
            role=role,
            display_name=display_name,
            login_method=user.login_method,
            profession=profession,
            facility_id=facility_id,
            **details,
        )
    except users.UserValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    try:
        organization = organizations.fetch_organization(org_id)
    except bq.BigQueryUnavailable:
        organization = None

    logger.info("onboarding_done: role=%s via=join_code", role)
    return _me_payload(
        auth.CurrentUser(
            uid=user.uid,
            login_method=user.login_method,
            role=role,
            org_id=org_id,
            org_name=(organization or {}).get("org_name"),
            profession=profession,
            display_name=display_name,
            facility_id=facility_id,
            **details,
        )
    )


@router.get("/me")
async def me(user: auth.CurrentUser = Depends(auth.current_user)) -> dict[str, object]:
    """利用者の立場・組織・（本人なら）自分の記録のIDを返す。

    初回の登録がまだのときも 200 を返し、`registered` を false にする
    （画面が「支援を受けたい／支援をする」の選択に進めるようにするため）。
    """
    return _me_payload(user)


@router.patch("/me")
async def update_me(
    body: ProfileUpdateRequest,
    user: auth.CurrentUser = Depends(
        auth.require_roles_without_org(
            users.ROLE_PROFESSIONAL, users.ROLE_VOLUNTEER, users.ROLE_FACILITY_STAFF
        )
    ),
) -> dict[str, object]:
    """支援者が、表示名・職種・連絡先・活動する市区町村・住所を変える（要望4・追加修正④）。

    組織が無くても使える（要望1。組織がない支援者もプロフィールは直せる）。
    施設の職員も、支援者と同じ項目を直せる（9章。施設はここでは変えない）。

    立場・組織・ログインの方法はここでは変えない（変えると見られる記録の範囲が変わる。
    要件定義書10-3）。本人（被災者）の基本の情報は `PATCH /api/survivors/{id}/basic` を使う。
    """
    try:
        row = await asyncio.to_thread(users.fetch_user, user.uid)
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="利用者の情報が見つかりません"
        )
    try:
        updated = users.update_profile(
            current=row,
            display_name=body.display_name,
            profession=body.profession,
            contact=body.contact,
            municipality_code=body.municipality_code,
            address=body.address,
        )
    except users.UserValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    logger.info("profile_updated: role=%s", user.role)
    return _me_payload(
        auth.CurrentUser(
            uid=user.uid,
            login_method=user.login_method,
            role=str(updated["role"]),
            org_id=str(updated["org_id"]),
            org_name=user.org_name,
            profession=updated.get("profession"),
            display_name=str(updated["display_name"]),
            linked_survivor_id=updated.get("linked_survivor_id"),
            contact=updated.get("contact"),
            municipality_code=updated.get("municipality_code"),
            address=updated.get("address"),
            org_permission=updated.get("org_permission"),
            facility_id=updated.get("facility_id"),
        )
    )


@router.post("/me/organization")
async def change_survivor_organization(
    body: SurvivorOrganizationRequest,
    user: auth.CurrentUser = Depends(auth.require_roles_without_org(users.ROLE_SURVIVOR)),
) -> dict[str, object]:
    """本人が、支援の組織を選び直す（つなぐ・変える・外す。追加修正⑦）。

    - 選んだ組織が実在するかを確かめる（無ければ 400）。空なら組織を外す。
    - 書く順番は「前の組織の担当を外す → 記録の版を足す → 利用者の行を書く」。
      - 担当を先に外す。記録の版を先に足すと、途中で落ちたときに担当が残ったまま、
        やり直しでも「いまと同じ組織」になって外す機会がなくなるため。
      - 記録の組織がすでに選んだ組織なら（前に途中で落ちた）、記録の版は足さずに
        利用者の行だけ書く（やり直せるようにする）。
    - 見える範囲は記録のいまの組織で決まるので（`survivors.can_view`）、版を足した時点で
      前の組織の専門職と担当だった支援者からは見えなくなり（404）、新しい組織の専門職から
      見えるようになる。
    - raw は追記だけ（UPDATE・DELETE は使わない）。担当を外すのも「外した」の行を足す。
    """
    if not user.linked_survivor_id:
        raise _bad_request("ご自分の記録が見つかりません。ログアウトしてお試しください")

    organization = _chosen_organization(body.org_id)
    new_org_id = str(organization.get("org_id")) if organization else ""
    if new_org_id == (user.org_id or ""):
        raise _bad_request("いまと同じ組織です")

    try:
        current = await asyncio.to_thread(survivors.fetch_survivor, user.linked_survivor_id)
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    if current is None or current.get("self_user_id") != user.uid:
        raise _bad_request("ご自分の記録が見つかりません。ログアウトしてお試しください")

    old_org_id = str(current.get("org_id") or "")
    survivor_id = str(current.get("survivor_id") or user.linked_survivor_id)
    try:
        ended = 0
        if old_org_id and old_org_id != new_org_id:
            ended = await asyncio.to_thread(
                _end_assignments, survivor_id=survivor_id, org_id=old_org_id, user=user
            )
        if old_org_id != new_org_id:
            await asyncio.to_thread(
                survivors.change_organization,
                current=current,
                org_id=new_org_id,
                user_id=user.uid,
            )
        await asyncio.to_thread(
            users.register_user,
            user_id=user.uid,
            org_id=new_org_id,
            role=users.ROLE_SURVIVOR,
            display_name=str(user.display_name or current.get("full_name") or ""),
            login_method=user.login_method,
            linked_survivor_id=user.linked_survivor_id,
        )
    except survivors.SurvivorValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except users.UserValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    logger.info(
        "survivor_organization_changed: had_org=%s has_org=%s ended_assignments=%d",
        bool(old_org_id),
        bool(new_org_id),
        ended,
    )
    return _me_payload(
        auth.CurrentUser(
            uid=user.uid,
            login_method=user.login_method,
            role=users.ROLE_SURVIVOR,
            org_id=new_org_id,
            org_name=(organization or {}).get("org_name"),
            display_name=user.display_name,
            linked_survivor_id=user.linked_survivor_id,
        )
    )


def _end_assignments(*, survivor_id: str, org_id: str, user: auth.CurrentUser) -> int:
    """前の組織の担当（有効・承認待ち）をすべて外す（追加修正⑦）。外した数を返す。

    担当は組織ごとのもので、本人が組織を変えたら続ける理由がない。外さずに残すと、
    本人がのちに同じ組織に戻ったときに、知らないうちに担当が復活してしまう。
    """
    rows = assignments.list_for_survivor(survivor_id=survivor_id, org_id=org_id)
    ended = 0
    for row in rows:
        if row.get("status") == assignments.STATUS_REMOVED:
            continue
        assignments.remove(
            assignment_id=str(row.get("assignment_id") or ""),
            survivor_id=survivor_id,
            org_id=org_id,
            supporter_user_id=str(row.get("supporter_user_id") or ""),
            acted_by=user.uid,
            acted_role=users.ROLE_SURVIVOR,
        )
        ended += 1
    return ended


@router.post("/me/join")
async def join_organization(
    body: JoinOrganizationRequest,
    user: auth.CurrentUser = Depends(auth.require_roles_without_org(users.ROLE_VOLUNTEER)),
) -> dict[str, object]:
    """組織がない一般の支援者が、参加コードで組織に入る（要望1）。

    参加コードが専門職用なら専門職になる（組織を作ったときに発行したコードと同じ仕組み。
    `services/organizations.py` の `resolve_join_code`）。施設の参加コードなら、施設の職員に
    なる（使いやすさの改修の計画.md 9章）。
    """
    if user.org_id:
        raise _bad_request("すでに組織に入っています")

    resolved = _resolve_any_join_code(body.join_code or "")
    if resolved is None:
        raise _bad_request("参加コードが違います。配られたコードを確かめてください")
    org_id, role, facility_id = resolved

    try:
        profession = users.validate_profession(role, body.profession)
        updated = users.register_user(
            user_id=user.uid,
            org_id=org_id,
            role=role,
            display_name=str(user.display_name or ""),
            login_method=user.login_method,
            profession=profession,
            facility_id=facility_id,
            **_supporter_details(user),
        )
    except users.UserValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    try:
        organization = organizations.fetch_organization(org_id)
    except bq.BigQueryUnavailable:
        organization = None

    logger.info("organization_joined: role=%s", role)
    return _me_payload(
        auth.CurrentUser(
            uid=user.uid,
            login_method=user.login_method,
            role=role,
            org_id=org_id,
            org_name=(organization or {}).get("org_name"),
            profession=updated.get("profession"),
            display_name=str(updated.get("display_name") or ""),
            facility_id=updated.get("facility_id"),
            **_supporter_details(user),
        )
    )


@router.post("/organizations", status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: OrganizationCreateRequest,
    user: auth.CurrentUser = Depends(auth.require_roles_without_org(users.ROLE_VOLUNTEER)),
) -> dict[str, object]:
    """組織がない一般の支援者が、組織を作る（要望2）。作った人はその組織の専門職になる。

    作った人は、その組織のオーナーにもなる（追加修正⑥）。発行した参加コードは、この応答で
    返すほか、オーナーがあとからプロフィールの画面で見られる（`GET /api/organizations/join-codes`）。
    """
    if user.org_id:
        raise _bad_request("すでに組織に入っています")

    try:
        created = organizations.create_organization(
            org_name=body.org_name,
            prefecture=body.prefecture,
            municipality_code=body.municipality_code,
            created_by=user.uid,
            created_role=str(user.role),
        )
    except organizations.OrganizationValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    org_id = str(created["org_id"])
    try:
        profession = users.validate_profession(users.ROLE_PROFESSIONAL, body.profession)
        updated = users.register_user(
            user_id=user.uid,
            org_id=org_id,
            role=users.ROLE_PROFESSIONAL,
            display_name=str(user.display_name or ""),
            login_method=user.login_method,
            profession=profession,
            **_supporter_details(user),
            # 作った人はオーナーになる（追加修正⑥）
            org_permission=users.ORG_PERMISSION_OWNER,
        )
    except users.UserValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        # 組織はすでに作られている。利用者の行だけが書けなかった場合、もう一度
        # /api/organizations/by-municipality で見つかるので、/api/me/join で入れる
        raise _unavailable() from exc

    logger.info("organization_created")
    return {
        "me": _me_payload(
            auth.CurrentUser(
                uid=user.uid,
                login_method=user.login_method,
                role=users.ROLE_PROFESSIONAL,
                org_id=org_id,
                org_name=str(created["org_name"]),
                profession=updated.get("profession"),
                display_name=str(updated.get("display_name") or ""),
                **_supporter_details(user),
                org_permission=updated.get("org_permission"),
            )
        ),
        "organization": {
            "org_id": org_id,
            "org_name": created["org_name"],
            "prefecture": created["prefecture"],
            "municipality_code": created["municipality_code"],
        },
        "pro_join_code": created["pro_join_code"],
        "vol_join_code": created["vol_join_code"],
    }


# --- 組織のオーナー（追加修正⑥） ---
#
# どれも `auth.require_owner` を通った人だけが使える。組織のIDは要求から受け取らず、
# `stg_users` から読んだ**この利用者の組織**だけを扱う（別の組織は指定する方法がない）。
# 参加コードの値はログに出さない。


def _join_codes_payload(row: dict[str, object] | None) -> dict[str, object]:
    row = row or {}
    pro_code = row.get("pro_join_code") or None
    vol_code = row.get("vol_join_code") or None
    return {
        "org_id": row.get("org_id"),
        "org_name": row.get("org_name"),
        "pro_join_code": pro_code,
        "vol_join_code": vol_code,
        # 元のコードを保存する前に作った組織（初期データなど）は false。発行し直してもらう
        "available": bool(pro_code and vol_code),
    }


def _member_payload(row: dict[str, object]) -> dict[str, object]:
    # 連絡先・住所は返さない（追加修正④。オーナーにも見せない）
    return {
        "user_id": row.get("user_id"),
        "display_name": row.get("display_name"),
        "profession": row.get("profession"),
        "is_owner": users.is_owner(row),
    }


@router.get("/organizations/join-codes")
async def get_join_codes(
    user: auth.CurrentUser = Depends(auth.require_owner),
) -> dict[str, object]:
    """自分の組織の参加コードを返す（オーナーだけ）。"""
    try:
        row = await asyncio.to_thread(organizations.fetch_join_codes, str(user.org_id))
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="組織が見つかりません")
    return _join_codes_payload(row)


@router.post("/organizations/join-codes")
async def reissue_join_codes(
    user: auth.CurrentUser = Depends(auth.require_owner),
) -> dict[str, object]:
    """参加コードを発行し直す（オーナーだけ）。前のコードでは、もう組織に入れない。"""
    try:
        row = await asyncio.to_thread(
            lambda: organizations.reissue_join_codes(
                org_id=str(user.org_id), created_by=user.uid, created_role=str(user.role)
            )
        )
    except organizations.OrganizationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    return _join_codes_payload(row)


@router.get("/organizations/members")
async def list_members(
    user: auth.CurrentUser = Depends(auth.require_owner),
) -> dict[str, object]:
    """自分の組織の専門職と、オーナーかどうか（オーナーだけ）。"""
    try:
        rows = await asyncio.to_thread(users.list_org_professionals, str(user.org_id))
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    return {"members": [_member_payload(row) for row in rows]}


@router.post("/organizations/owners")
async def change_owner(
    body: OwnerChangeRequest,
    user: auth.CurrentUser = Depends(auth.require_owner),
) -> dict[str, object]:
    """同じ組織の専門職に、オーナーの権限を付ける・外す（オーナーだけ）。

    - 相手は同じ組織の専門職だけ。別の組織の人・見つからない人は 404、
      同じ組織でも専門職でない人（一般の支援者・本人）は 400。
    - 最後の1人のオーナーは外せない（参加コードを見られる人がいなくなるため）。
      数えるのは、外す前に読んだ組織の専門職の一覧の中のオーナー。
    - すでにその状態なら、行を足さずにそのまま返す。
    """
    target_id = (body.user_id or "").strip()
    if not target_id:
        raise _bad_request("権限を変える人を選んでください")

    try:
        target = await asyncio.to_thread(users.fetch_user, target_id)
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    if target is None or target.get("org_id") != user.org_id:
        # 別の組織の人がいるかどうかも分からないようにする（10-3と同じ考え方）
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="同じ組織の専門職が見つかりません"
        )
    if target.get("role") != users.ROLE_PROFESSIONAL:
        raise _bad_request("オーナーの権限は、同じ組織の専門職にだけ付けられます")

    try:
        members = await asyncio.to_thread(users.list_org_professionals, str(user.org_id))
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    already = users.is_owner(target)
    if already != body.owner:
        if not body.owner:
            owners = [row for row in members if users.is_owner(row)]
            if len(owners) <= 1:
                raise _bad_request(
                    "最後の1人のオーナーは外せません。先にほかの専門職をオーナーにしてください"
                )
        try:
            await asyncio.to_thread(
                lambda: users.set_org_owner(
                    target=target,
                    owner=body.owner,
                    actor_id=user.uid,
                    actor_role=str(user.role),
                )
            )
        except users.UserValidationError as exc:
            raise _bad_request(str(exc)) from exc
        except bq.BigQueryUnavailable as exc:
            raise _unavailable() from exc
        logger.info("org_owner_changed: owner=%s self=%s", body.owner, target_id == user.uid)
        permission = users.ORG_PERMISSION_OWNER if body.owner else None
        members = [
            {**row, "org_permission": permission} if row.get("user_id") == target_id else row
            for row in members
        ]

    me_is_owner = any(
        row.get("user_id") == user.uid and users.is_owner(row) for row in members
    )
    return {
        "members": [_member_payload(row) for row in members],
        # 自分を外したときは false。画面はオーナーの欄を閉じる
        "me_is_owner": me_is_owner,
    }
