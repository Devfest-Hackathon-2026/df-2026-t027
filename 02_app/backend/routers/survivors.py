"""F1 被災者の記録と担当の API（要件定義書F1・10-3・10-5、開発手順書 Step 5-1）。

- `GET  /api/survivors`                                一覧（F4の訪ねる順番。一般は担当の人だけ）
- `POST /api/survivors`                                登録（確認コードを発行する。専門職だけ）
- `GET  /api/survivors/{id}`                           詳細（立場に応じた範囲）
- `POST /api/survivors/{id}/location`                  居場所・段階を変える（履歴を残す）
- `POST /api/survivors/{id}/assignments`               一般の支援者を担当にする（専門職だけ）
- `POST /api/survivors/{id}/assignments/{aid}/approve` 担当を認める（本人だけ）
- `POST /api/survivors/{id}/assignments/{aid}/remove`  担当を外す（本人・専門職）

守ること（要件定義書10-3）：

- 立場と組織は `stg_users` から決める。**要求に書かれた組織のIDや立場は使わない。**
  一覧の `assignee` も、一般の支援者のときは自分のIDに固定する。
- **見る権限のない記録は、存在しないときと同じ 404** を返す（記録の有無も分からないようにする）。
  立場で使えない操作は 403（この2つは混ぜない）。
- 氏名・連絡先・確認コードの値をログに出さない。

### BigQuery の読み書きは、すべて `asyncio.to_thread` で別のスレッドに出す

`google-cloud-bigquery` は同期で、1回の読みに1秒前後かかる。`async def` の中から
そのまま呼ぶと**イベントループを塞ぎ**、ほかの人の要求まで待たされる（デモでは4人が同時に使う）。

さらに、**互いに関係のない読みは `asyncio.gather` で同時に走らせる**。詳細の画面は
記録・担当・居場所の履歴・支援者の名前で5回読み、順に読むと合計およそ6秒かかっていた
（実測：`int_survivor_list` の1件読み 2.6秒、ほかは 0.4〜1.0秒）。

ただし**記録そのものの読みだけは先に単独で行う**。見てよいかを確かめる前に
関連する情報を読みに行くと、見えない記録のために読みが走る（無駄なうえ、
存在しないIDを並べられると読みが増える経路になる）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from backend import auth
from backend.routers import shared
from backend.services import (
    assignments,
    bq,
    consultations,
    handovers,
    health,
    link_codes,
    supports,
    survivors,
    users,
    visits,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/survivors", tags=["F1"])


# --- 要求の形 ---


class SurvivorCreateRequest(BaseModel):
    """専門職が被災者を登録する。"""

    full_name: str = Field(default="", description="氏名")
    age: int | None = Field(default=None, description="年齢")
    household_size: int | None = Field(default=None, description="世帯の人数")
    contact: str | None = Field(default=None, description="連絡先（任意）")
    stage: str | None = Field(default=None, description="今の段階")
    location_type: str | None = Field(default=None, description="今の居場所の種類")
    location_detail: str | None = Field(default=None, description="具体的な場所（任意）")
    # 住んでいる市区町村と住所（要望8）。専門職の登録では組織がすでに決まっているため任意。
    municipality_code: str | None = Field(default=None, description="住んでいる市区町村の団体コード（任意）")
    address: str | None = Field(default=None, description="住所（任意）")
    # 氏名と年齢が近い記録を見たうえで「別の人です」と確かめたとき true にする（F1）
    acknowledged_similar: bool = Field(default=False, description="近い記録を確かめたか")


class LocationChangeRequest(BaseModel):
    """居場所と段階を変える。"""

    stage: str | None = Field(default=None, description="今の段階")
    location_type: str | None = Field(default=None, description="今の居場所の種類")
    location_detail: str | None = Field(default=None, description="具体的な場所（任意）")


class BasicUpdateRequest(BaseModel):
    """氏名・年齢・世帯の人数・連絡先・住所を変える（プロフィールの画面。要望4・8）。

    住んでいる市区町村はここでは変えない（組織を決めた値のため）。
    """

    full_name: str = Field(default="", description="氏名")
    age: int | None = Field(default=None, description="年齢")
    household_size: int | None = Field(default=None, description="世帯の人数")
    contact: str | None = Field(default=None, description="連絡先（任意）")
    address: str | None = Field(default=None, description="住所（任意）")


class AssignmentCreateRequest(BaseModel):
    """一般の支援者を担当に割り当てる。"""

    supporter_user_id: str = Field(default="", description="担当にする一般の支援者のID")
    # 本人がアカウントを持っていないときだけ使う（口頭の同意を得たことの記録。F1）
    verbal_consent: bool = Field(default=False, description="本人の同意を口頭で得たか")


# --- 共通 ---


# 断り方と見える範囲の確認は `routers/shared.py` が正本（F2・F3 と同じ規則にするため）
_bad_request = shared.bad_request
_not_found = shared.not_found
_unavailable = shared.unavailable
_load_visible = shared.load_visible
_supporter_names = shared.supporter_names
_member_rows = shared.member_rows
_names_for_ids = shared.names_for_ids


def _possible_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """氏名が同じで年齢が近い組を探す（本人が登録した記録の取り違えに気づくため。F1）。"""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        age = row.get("age")
        if not isinstance(age, int) or isinstance(age, bool):
            continue
        by_name.setdefault(survivors.normalize_name(row.get("full_name")), []).append(row)

    groups: list[dict[str, Any]] = []
    for members in by_name.values():
        if len(members) < 2:
            continue
        # 年齢の近いものだけを1つの組にする（同姓同名で年齢が離れていれば別人）
        cluster: list[dict[str, Any]] = []
        for member in sorted(members, key=lambda item: item["age"]):
            if cluster and member["age"] - cluster[0]["age"] > survivors.SIMILAR_AGE_DIFF:
                if len(cluster) >= 2:
                    groups.append(_duplicate_group(cluster))
                cluster = []
            cluster.append(member)
        if len(cluster) >= 2:
            groups.append(_duplicate_group(cluster))
    return groups


def _duplicate_group(members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "full_name": members[0].get("full_name"),
        "survivor_ids": [member.get("survivor_id") for member in members],
        "ages": [member.get("age") for member in members],
    }


# --- 一覧 ---


@router.get("")
async def list_survivors(
    stage: str | None = None,
    location_type: str | None = None,
    assignee: str | None = None,
    user: auth.CurrentUser = Depends(
        auth.require_roles(users.ROLE_PROFESSIONAL, users.ROLE_VOLUNTEER)
    ),
) -> dict[str, Any]:
    """被災者の一覧。一般の支援者には、自分が担当の人だけを返す（要件定義書F1）。"""
    assignee_user_id = assignee
    if user.role == users.ROLE_VOLUNTEER:
        # 要求に書かれた値では範囲を広げられない（要件定義書10-3）
        assignee_user_id = user.uid

    role = str(user.role)
    # 「担当で絞り込む」の選択肢に名前を出すために、専門職にだけ支援者の一覧を返す。
    # 読みが1回増えるが、一覧の画面で名前を出す別の手段がない（IDだけでは選べない）。
    # 一覧とは関係がないので、同時に読む。
    want_supporters = role == users.ROLE_PROFESSIONAL
    reads = [
        asyncio.to_thread(
            survivors.list_survivors,
            org_id=str(user.org_id),
            stage=stage,
            location_type=location_type,
            assignee_user_id=assignee_user_id,
        )
    ]
    if want_supporters:
        reads.append(asyncio.to_thread(_supporter_names, user.org_id))

    try:
        results = await asyncio.gather(*reads)
    except survivors.SurvivorValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    rows = results[0]
    supporters: list[dict[str, Any]] = []
    if want_supporters:
        supporters = [
            {"user_id": user_id, "display_name": name} for user_id, name in results[1].items()
        ]

    return {
        "survivors": [survivors.list_item(row, role=role) for row in rows],
        "count": len(rows),
        "filters": {
            "stage": stage,
            "location_type": location_type,
            "assignee": assignee_user_id,
        },
        "supporters": supporters,
        # ホームの「対応が必要な人」（F4）。**同じ読みから作る**ので読みは増えない
        "attention": survivors.attention(rows),
        # ホームと一覧の「期限の近い未申請の支援」（F6）。これも**同じ読みから作る**。
        # 知らせる先は本人の画面と専門職のホーム・一覧（要件定義書F6）で、一般の支援者は
        # 支援制度を扱わない（446行）ので**中身を渡さない**（形は変えずに空で返す）
        "support_alerts": (
            supports.alerts(rows if role == users.ROLE_PROFESSIONAL else [])
        ),
        # 二重登録のおそれは専門職にだけ知らせる（直せるのは専門職だけ）
        "possible_duplicates": (
            _possible_duplicates(rows) if role == users.ROLE_PROFESSIONAL else []
        ),
    }


# --- 登録 ---


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_survivor(
    body: SurvivorCreateRequest,
    user: auth.CurrentUser = Depends(auth.require_roles(users.ROLE_PROFESSIONAL)),
) -> dict[str, Any]:
    """被災者を登録し、本人用の確認コードを発行する（要件定義書F1）。

    確認コードは**記録を作る前に発行する**。順番を逆にすると、記録は作られたのに
    コードが出なかった場合に、本人をつなぐ手段が無い記録が残ってしまう。
    コードだけが残った場合は、どの記録も指さないコードになるだけで害がない
    （つなぎ込みのときに記録が無ければ断る）。
    """
    org_id = str(user.org_id)

    if not body.acknowledged_similar:
        try:
            similar = await asyncio.to_thread(
                survivors.find_similar,
                org_id=org_id,
                full_name=body.full_name,
                age=body.age,
            )
        except bq.BigQueryUnavailable as exc:
            raise _unavailable() from exc
        if similar:
            logger.info("survivor_similar_found: count=%d", len(similar))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                # HTTPException の本文は FastAPI の変換を通らず、素の json.dumps になる。
                # 候補には登録日時（datetime）が入るので、ここで自分で変換する
                # （変換しないと 500 になる。実機で踏んだ）
                detail=jsonable_encoder(
                    {
                        "message": "氏名と年齢が近い記録があります。同じ方でないか確かめてください",
                        "similar": similar,
                    }
                ),
            )

    survivor_id = survivors.new_survivor_id()
    # 入力を先に確かめる（誤りのたびに使われないコードを残さないため）
    try:
        row = survivors.build_survivor_row(
            survivor_id=survivor_id,
            org_id=org_id,
            created_by=user.uid,
            created_role=str(user.role),
            full_name=body.full_name,
            age=body.age,
            household_size=body.household_size,
            contact=body.contact,
            stage=body.stage,
            location_type=body.location_type,
            location_detail=body.location_detail,
            self_user_id=None,
            registered_via=survivors.VIA_PROFESSIONAL,
            municipality_code=body.municipality_code,
            address=body.address,
        )
    except survivors.SurvivorValidationError as exc:
        raise _bad_request(str(exc)) from exc

    try:
        issued = await asyncio.to_thread(
            link_codes.issue_code,
            survivor_id=survivor_id,
            org_id=org_id,
            created_by=user.uid,
            created_role=str(user.role),
        )
        record = await asyncio.to_thread(survivors.insert_survivor, row)
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    logger.info("survivor_created: via=%s", survivors.VIA_PROFESSIONAL)
    return {
        "survivor": survivors.detail_payload(
            {**record, "version": 1}, role=str(user.role), user_id=user.uid
        ),
        # 紙に書いて本人に渡す値。**この応答でしか返さない**（保存しているのはハッシュだけ）
        "link_code": {"code": issued.code, "expires_at": issued.expires_at},
    }


# --- 詳細 ---


@router.get("/{survivor_id}")
async def get_survivor(
    survivor_id: str,
    user: auth.CurrentUser = Depends(auth.registered_or_self),
) -> dict[str, Any]:
    """被災者の詳細（支援者には1画面分をまとめて、本人には自分の範囲だけ）。

    組織がない本人（要望8・要確認E-1）も自分の記録は読める。

    健康・相談・支援・引き継ぎシート・居場所の履歴は**被災者のIDで読む**（追加修正②⑦⑪）。
    組織がない本人が書いたものも、本人が組織を変える前のものも、記録の続きとして出す。
    誰が見てよいかは、この前の `_load_visible` が記録のいまの組織で決める。
    担当（F1）と訪問・見守りの記録（F2）は、今までどおり記録の組織で読む
    （担当は組織ごとのもので、組織を変えると外れる）。
    """
    # 見てよいかを確かめてから、残りを読む
    row = await _load_visible(survivor_id, user)
    role = str(user.role)
    org_id = str(row.get("org_id") or "")

    # 互いに関係のない読みを同時に走らせる（順に読むと1秒ずつ積み上がる）。
    # 名前で引くのは、読みが増えたときに取り違えないため（番号で取ると気づけない）
    # 専門職・本人は組織の一覧（専門職＋一般の支援者）を同時に読む。読みは1回。
    # 一般の支援者はほかの支援者の一覧を読まない方針なので、必要な人だけ後から引く。
    want_names = role in (users.ROLE_PROFESSIONAL, users.ROLE_SURVIVOR)
    # 本人には訪問・見守りの記録を返さない（本文を本人向けのデータから外す。要件定義書F4）
    want_visits = role != users.ROLE_SURVIVOR
    # 相談（F8）を見られるのは本人と同じ組織の専門職だけ（要件定義書7-1の「見られる人」）。
    # 一般の支援者には読みそのものを走らせない（読みを1回減らすことにもなる）
    want_consultations = role in (users.ROLE_PROFESSIONAL, users.ROLE_SURVIVOR)
    # 支援と申請の状況（F6）を見られるのは本人と同じ組織の専門職だけ（要件定義書601行）。
    # 一般の支援者は支援制度を扱わない（446行）ので、読みそのものを走らせない
    want_supports = role in (users.ROLE_PROFESSIONAL, users.ROLE_SURVIVOR)
    reads = {
        "assignments": asyncio.to_thread(
            assignments.list_for_survivor, survivor_id=survivor_id, org_id=org_id
        ),
        "history": asyncio.to_thread(
            survivors.location_history, survivor_id=survivor_id, org_id=org_id
        ),
        "health": asyncio.to_thread(
            health.fetch_latest, survivor_id=survivor_id, org_id=org_id
        ),
        # 引き継ぎシート（F3）は本人・専門職・担当の一般の支援者が見られる（要件定義書7-1）。
        # 見てよいかはこの時点で確かめ済みなので、立場で分けずに読む
        "handovers": asyncio.to_thread(
            handovers.list_for_survivor, survivor_id=survivor_id, org_id=org_id
        ),
    }
    if want_names:
        reads["members"] = asyncio.to_thread(_member_rows, org_id)
    if want_visits:
        reads["visits"] = asyncio.to_thread(
            visits.list_visits, survivor_id=survivor_id, org_id=org_id
        )
    if want_consultations:
        reads["consultations"] = asyncio.to_thread(
            consultations.list_for_survivor, survivor_id=survivor_id, org_id=org_id
        )
    if want_supports:
        # 制度の情報とつないだ1回の読みで取る（読みの回数を増やさない）
        reads["supports"] = asyncio.to_thread(
            supports.list_for_survivor, survivor_id=survivor_id, org_id=org_id
        )

    try:
        gathered = await asyncio.gather(*reads.values())
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    results = dict(zip(reads.keys(), gathered))

    assignment_rows = results["assignments"]
    history = results["history"]
    visit_rows = results.get("visits") or []
    member_list: list[dict[str, Any]] = results.get("members") or []
    if want_names:
        names: dict[str, str] = {
            str(item.get("user_id")): str(item.get("display_name") or "")
            for item in member_list
        }
    else:
        # 担当と、記録を書いた人の分だけを引く（組織の一覧は読まない）
        wanted_ids = [item.get("supporter_user_id") for item in assignment_rows]
        wanted_ids += [item.get("recorded_by") for item in visit_rows]
        wanted_ids += [item.get("created_by") for item in (results.get("handovers") or [])]
        names = await asyncio.to_thread(_names_for_ids, org_id, wanted_ids)
    for item in assignment_rows:
        item["supporter_name"] = names.get(str(item.get("supporter_user_id")), "")

    candidates: list[dict[str, Any]] = []
    if role == users.ROLE_PROFESSIONAL:
        assigned = {
            item.get("supporter_user_id")
            for item in assignment_rows
            if item.get("status") != assignments.STATUS_REMOVED
        }
        # 担当になれるのは一般の支援者だけ（専門職は候補に出さない）
        candidates = [
            {
                "user_id": str(item.get("user_id")),
                "display_name": str(item.get("display_name") or ""),
            }
            for item in member_list
            if item.get("role") != users.ROLE_PROFESSIONAL
            and item.get("user_id") not in assigned
        ]

    return {
        "survivor": survivors.detail_payload(
            row,
            role=role,
            user_id=user.uid,
            health=health.payload(results.get("health")),
            visits=[visits.visit_payload(item, supporter_names=names) for item in visit_rows],
            consultations=[
                consultations.payload(
                    item,
                    role=role,
                    # 公開の相談か（追加修正②）。本人の記録のいまの組織と合わせて決める
                    public=consultations.is_public(
                        consultation_org_id=item.get("org_id"), survivor_org_id=org_id
                    ),
                )
                for item in (results.get("consultations") or [])
            ],
            handovers=[
                handovers.payload(
                    item,
                    creator_names=names,
                    health_version_now=(results.get("health") or {}).get("version"),
                )
                for item in (results.get("handovers") or [])
            ],
            supports=[
                supports.payload(item, role=role) for item in (results.get("supports") or [])
            ],
        ),
        "assignments": assignment_rows,
        "location_history": history,
        "supporter_candidates": candidates,
    }


# --- 居場所と段階の変更 ---


@router.post("/{survivor_id}/location")
async def change_location(
    survivor_id: str,
    body: LocationChangeRequest,
    user: auth.CurrentUser = Depends(
        auth.require_roles_or_self(users.ROLE_PROFESSIONAL, users.ROLE_SURVIVOR)
    ),
) -> dict[str, Any]:
    """居場所・段階を変える（前の内容は消さず、版を1つ足す。要件定義書F1）。

    組織がない本人も自分の記録は変えられる（追加修正②）。
    """
    row = await _load_visible(survivor_id, user)
    try:
        record = await asyncio.to_thread(
            survivors.change_location,
            current=row,
            stage=body.stage,
            location_type=body.location_type,
            location_detail=body.location_detail,
            created_by=user.uid,
            created_role=str(user.role),
        )
    except survivors.SurvivorValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    logger.info("survivor_location_changed: role=%s", user.role)
    # 新しい版の値で返す（悪化の兆しなどは前の行から引き継ぐ）
    return {
        "survivor": survivors.detail_payload(
            {**row, **record}, role=str(user.role), user_id=user.uid
        )
    }


@router.patch("/{survivor_id}/basic")
async def change_basic(
    survivor_id: str,
    body: BasicUpdateRequest,
    user: auth.CurrentUser = Depends(
        auth.require_roles_or_self(users.ROLE_PROFESSIONAL, users.ROLE_SURVIVOR)
    ),
) -> dict[str, Any]:
    """氏名・年齢・世帯の人数・連絡先を変える（プロフィールの画面。要望4）。

    組織がない本人（要望8・要確認E-1）も自分の記録は直せる。

    居場所・段階はここでは変えない（`POST /{id}/location` を使う）。
    """
    row = await _load_visible(survivor_id, user)
    try:
        record = await asyncio.to_thread(
            survivors.change_basic,
            current=row,
            full_name=body.full_name,
            age=body.age,
            household_size=body.household_size,
            contact=body.contact,
            created_by=user.uid,
            created_role=str(user.role),
            address=body.address,
            # 本人は年齢・世帯の人数を空にしてよい（使いやすさの改修の計画.md 4-2の3）
            optional_counts=user.role == users.ROLE_SURVIVOR,
        )
    except survivors.SurvivorValidationError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    logger.info("survivor_basic_changed: role=%s", user.role)
    return {
        "survivor": survivors.detail_payload(
            {**row, **record}, role=str(user.role), user_id=user.uid
        )
    }


# --- 担当 ---


@router.post("/{survivor_id}/assignments", status_code=status.HTTP_201_CREATED)
async def create_assignment(
    survivor_id: str,
    body: AssignmentCreateRequest,
    user: auth.CurrentUser = Depends(auth.require_roles(users.ROLE_PROFESSIONAL)),
) -> dict[str, Any]:
    """一般の支援者を担当に割り当てる（要件定義書F1）。

    - 本人のアカウントがつながっている → **本人が認めるまで有効にしない**。
    - つながっていない → 「口頭で同意を得た」ことの記録を必須にする。
    """
    row = await _load_visible(survivor_id, user)
    org_id = str(row.get("org_id"))

    try:
        supporter = await asyncio.to_thread(
            users.fetch_supporter, body.supporter_user_id.strip()
        )
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    if supporter is None:
        raise _bad_request("その支援者が見つかりません。登録が済んでいるか確かめてください")
    if supporter.get("role") != users.ROLE_VOLUNTEER:
        raise _bad_request("担当にできるのは、一般の支援者（ボランティア・ご家族）だけです")
    if supporter.get("org_id") != org_id:
        raise _bad_request("同じ市町の支援者だけを担当にできます")

    linked_to_self = bool(row.get("self_user_id"))
    verbal_consent = body.verbal_consent
    if linked_to_self:
        # 本人が認める経路があるので、口頭の同意では有効にしない
        verbal_consent = False
    elif not verbal_consent:
        raise _bad_request(
            "ご本人のアカウントがまだつながっていません。"
            "口頭で同意を得たことを確かめてから割り当ててください"
        )

    try:
        created = await asyncio.to_thread(
            assignments.create,
            survivor_id=survivor_id,
            org_id=org_id,
            supporter_user_id=body.supporter_user_id,
            created_by=user.uid,
            created_role=str(user.role),
            verbal_consent=verbal_consent,
        )
    except assignments.AssignmentError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    return {
        "assignment": {
            "assignment_id": created.assignment_id,
            "supporter_user_id": created.supporter_user_id,
            "supporter_name": supporter.get("display_name"),
            "status": created.status,
        }
    }


async def _find_assignment(
    survivor_id: str, org_id: str, assignment_id: str
) -> dict[str, Any]:
    try:
        rows = await asyncio.to_thread(
            assignments.list_for_survivor, survivor_id=survivor_id, org_id=org_id
        )
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc
    found = assignments.find(rows, assignment_id)
    if found is None:
        raise _not_found()
    return found


@router.post("/{survivor_id}/assignments/{assignment_id}/approve")
async def approve_assignment(
    survivor_id: str,
    assignment_id: str,
    user: auth.CurrentUser = Depends(auth.require_roles(users.ROLE_SURVIVOR)),
) -> dict[str, Any]:
    """本人が担当を認める（認められるのは本人だけ。要件定義書10-5）。"""
    row = await _load_visible(survivor_id, user)
    org_id = str(row.get("org_id"))
    found = await _find_assignment(survivor_id, org_id, assignment_id)

    try:
        await asyncio.to_thread(
            assignments.approve,
            assignment_id=assignment_id,
            survivor_id=survivor_id,
            org_id=org_id,
            supporter_user_id=str(found.get("supporter_user_id") or ""),
            acted_by=user.uid,
            acted_role=str(user.role),
        )
    except assignments.AssignmentError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    return {"assignment": {**found, "status": assignments.STATUS_ACTIVE}}


@router.post("/{survivor_id}/assignments/{assignment_id}/remove")
async def remove_assignment(
    survivor_id: str,
    assignment_id: str,
    user: auth.CurrentUser = Depends(
        auth.require_roles(users.ROLE_SURVIVOR, users.ROLE_PROFESSIONAL)
    ),
) -> dict[str, Any]:
    """担当を外す（本人はいつでも外せる。専門職も外せる。要件定義書F1・10-5）。"""
    row = await _load_visible(survivor_id, user)
    org_id = str(row.get("org_id"))
    found = await _find_assignment(survivor_id, org_id, assignment_id)

    try:
        await asyncio.to_thread(
            assignments.remove,
            assignment_id=assignment_id,
            survivor_id=survivor_id,
            org_id=org_id,
            supporter_user_id=str(found.get("supporter_user_id") or ""),
            acted_by=user.uid,
            acted_role=str(user.role),
        )
    except assignments.AssignmentError as exc:
        raise _bad_request(str(exc)) from exc
    except bq.BigQueryUnavailable as exc:
        raise _unavailable() from exc

    return {"assignment": {**found, "status": assignments.STATUS_REMOVED}}
