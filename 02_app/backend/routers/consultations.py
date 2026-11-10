"""F8 被災者からの相談の API（要件定義書F8・10-5、開発手順書 Step 8）。

- `POST  /api/survivors/{id}/consultations/structure`        文章を種類と項目に整えた案を返す（保存しない。
                                                             古い画面のために1回のデプロイの間だけ残す）
- `POST  /api/survivors/{id}/consultations`                  本人が書いた文章をそのまま送る（送ったときに
                                                             整えて、急ぎの度合いを付けて保存する）
- `GET   /api/consultations`                                 組織の相談の一覧（未対応から・急ぎの順）
- `POST  /api/survivors/{id}/consultations/{cid}/replies`    返事を書く（専門職だけ）
- `PATCH /api/survivors/{id}/consultations/{cid}`            相談の状況を変える（専門職だけ）
- `GET   /api/consultations/public`                          公開の相談の一覧（支援者全員。追加修正②）
- `GET   /api/consultations/public/{cid}/messages`           公開の相談のやり取り（支援者全員）
- `POST  /api/consultations/public/{cid}/replies`            公開の相談に返事を書く（専門職だけ。組織は問わない）

### 組織につながっていない本人の相談（追加修正②）

組織がない本人も相談を送れる。その相談は**公開**になり、組織を問わずログインした支援者
全員が一覧で見られる。一覧には氏名を出さない（市区町村まで）。返事を書けるのは専門職だけ
（組織は問わない）。本人がのちに組織につなぐと、前の相談は公開の一覧から消え、その組織の
相談になる。raw は書き換えないので、**誰に見せるかは本人の記録のいまの組織で決める**
（`services/consultations.py` の `is_public` の上の説明）。

相談を保存したあとは、**応答を返す前に F4 の悪化の兆しの判断を行う**（`services/risk_ai.py`。
要件定義書10-5の注。画面から呼ぶAPIにはしない。本人に判断の結果を返さないため）。

守ること（要件定義書F8・10-3・10-5）：

- **本人は相談をそのまま送る**（画面の改修の計画.md 6章）。送ったときに、整える処理と急ぎの
  度合いを1回の問い合わせ（`consultation_ai.triage`）で行い、保存する。整えた9つの項目は
  専門職が読むときの案で、本人には見せない。
- **送れるのは本人だけ。** 返事と状況を変えられるのは専門職だけ（10-5の「使える人」）。
- **Gemini を呼べないときも送れる。** 9つの項目は「記載なし」、急ぎの度合いは既定の値に
  落として保存する（本人が書いた相談を捨てない。`ai_used` は false）。
- **送る前の画面に、命に関わるときの119番の案内を必ず出す**（要件定義書F8）。文言は
  `services/consultations.py` が正本で、APIが返す（画面に書き写さない）。
- **急ぎの度合いと根拠は本人に返さない**（要件定義書10-3。`consultations.payload` が立場で分ける）。
- 立場と組織は `stg_users` から決める。要求に書かれた立場・組織のIDは使わない（10-3）。
- 見る権限のない記録は 404（`shared.load_visible`）。
- 相談の本文・氏名・健康の情報をログに出さない（10-2の6・11-3）。

BigQuery は同期なので、読み書きはすべて `asyncio.to_thread` で別のスレッドに出す。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from backend import auth
from backend.routers import shared
from backend.services import (
    bq,
    consultation_ai,
    consultations,
    gemini,
    health,
    risk_ai,
    users,
    visits,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["F8"])

# Gemini を呼べなかったときに画面へ出す案内（手入力に切り替えてもらう）。
AI_UNAVAILABLE_NOTICE = (
    "AIで項目に整えられませんでした。下の欄にご自分で書き入れて送ってください"
    "（書いた内容はそのまま相談員に届きます）"
)


# --- 要求の形 ---


class ConsultationStructureRequest(BaseModel):
    """相談の文章を整える（保存はしない）。"""

    raw_text: str = Field(default="", description="相談の内容（文章）")


class ConsultationSendRequest(BaseModel):
    """本人が書いた相談をそのまま送る（画面の改修の計画.md 6章）。

    前は、整えた9つの項目（`items`）・急を要するか・AIを使ったかも受け取っていた。
    古い画面がそれらを送ってきても使わない（送ったときに文章から整え直す）。
    """

    raw_text: str = Field(default="", description="ご本人が書いた文章（必須）")
    category: str | None = Field(
        default=None, description="ご本人が選んだ相談の種類（選ばなければ AI の案を使う）"
    )


class ReplyRequest(BaseModel):
    """専門職が相談に返事を書く。"""

    reply_text: str = Field(default="", description="返事（必須）")
    next_action: str | None = Field(default=None, description="次の対応（任意）")
    status: str | None = Field(default=None, description="相談の状況（変えるときだけ）")


class StatusRequest(BaseModel):
    """専門職が相談の状況を変える。"""

    status: str = Field(default="", description="open・in_progress・closed")


class MessageRequest(BaseModel):
    """本人が、同じ相談に書き足す（要望7）。"""

    message: str = Field(default="", description="書き足す内容（必須）")


# --- 共通 ---


async def _load_consultation(*, survivor_id: str, consultation_id: str) -> dict[str, Any]:
    """その人の相談を1件読む（無いもの・ほかの人のものは同じ 404）。

    記録を見てよいかは、先に `shared.load_visible` で確かめておく（ほかの組織の記録は
    そこで 404 になる）。相談は被災者のIDで読む（追加修正②⑦）。
    """
    try:
        row = await asyncio.to_thread(
            consultations.fetch_consultation,
            consultation_id=consultation_id,
            survivor_id=survivor_id,
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if not row:
        raise shared.not_found()
    return row


def _survivor_org(survivor: dict[str, Any]) -> str:
    """本人の記録のいまの組織（組織がなければ空。`str(None)` で "None" にしない）。"""
    return str(survivor.get("org_id") or "")


# 本人だけが使うAPI。組織がない本人も通す（追加修正②）。
survivor_only = auth.require_roles_or_self(users.ROLE_SURVIVOR)


# --- F8-1. 相談の文章を整える（保存しない） ---


@router.post("/survivors/{survivor_id}/consultations/structure")
async def structure_consultation(
    survivor_id: str,
    body: ConsultationStructureRequest,
    user: auth.CurrentUser = Depends(survivor_only),
) -> dict[str, Any]:
    """本人が書いた文章を、相談の種類と9つの項目に整えた案にして返す（要件定義書F8）。

    **保存はしない。** `ai_used` が false のときは、画面が手入力の欄を出す。
    命に関わるときの案内（119番）は、AIの判断にかかわらず**必ず**返す。
    組織がない本人には、公開になることの知らせも返す（追加修正②）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = _survivor_org(survivor)
    try:
        raw_text = consultations.clean_raw_text(body.raw_text)
    except consultations.ConsultationValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        health_row = await asyncio.to_thread(
            health.fetch_latest, survivor_id=survivor_id, org_id=org_id
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    payload: dict[str, Any] = {
        "survivor_id": survivor_id,
        # 送る前に必ず出す案内（要件定義書F8）。文言の正本は services/consultations.py
        "emergency_guide": consultations.EMERGENCY_GUIDE,
        # 組織がない本人の相談は公開になる（追加修正②）。送る前に知らせる
        "public": not org_id,
        "public_notice": consultations.PUBLIC_NOTICE if not org_id else None,
    }

    try:
        structured = await asyncio.to_thread(
            consultation_ai.structure,
            raw_text=raw_text,
            survivor=survivor,
            health_row=health_row,
        )
    except gemini.GeminiUnavailable as exc:
        # 相談そのものを送れるようにする（整えられないだけで止めない）
        logger.warning("consultation_structure_unavailable: %s", type(exc).__name__)
        return {
            **payload,
            **consultation_ai.empty_payload(),
            "emergency_notice": None,
            "ai_used": False,
            "notice": AI_UNAVAILABLE_NOTICE,
        }
    except consultations.ConsultationValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    return {
        **payload,
        **structured,
        # AIが「急を要する」と見たときは、本人の画面にすぐ出す（要件定義書F8）
        "emergency_notice": (
            consultations.EMERGENCY_NOTICE if structured.get("emergency_flag") else None
        ),
        "ai_used": True,
        "notice": None,
    }


# --- F8-2. 確かめた相談を送る ---


@router.post("/survivors/{survivor_id}/consultations", status_code=status.HTTP_201_CREATED)
async def send_consultation(
    survivor_id: str,
    body: ConsultationSendRequest,
    user: auth.CurrentUser = Depends(survivor_only),
) -> dict[str, Any]:
    """本人が書いた相談をそのまま送る（要件定義書F8、画面の改修の計画.md 6章）。

    送ったときに、**整える処理と急ぎの度合いの判断を1回の問い合わせで行い**、保存する
    （専門職のホームの並び順になる）。呼べなくても既定の度合いに落として保存する（送信を止めない）。

    保存できたら、**応答を返す前に F4 の悪化の兆しの判断を行う**（要件定義書10-5の注）。
    Cloud Run は応答のあと CPU を止める設定のため、裏では走らせない（無料枠に収める修正の手順4-2）。

    組織がない本人の相談は**公開**になる（追加修正②。組織のIDを空にして残す）。
    悪化の兆しは組織の専門職が訪ねる順番を決めるためのもので、組織がないと使う人が
    いないため判断しない（Gemini の回数も使わない）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = _survivor_org(survivor)

    try:
        raw_text = consultations.clean_raw_text(body.raw_text)
    except consultations.ConsultationValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    chosen_category = (
        consultations.normalize_category(body.category) if (body.category or "").strip() else None
    )

    # 整える処理には健康・介護の情報を、急ぎの度合いには直近の記録を渡す（要件定義書10-6）。
    # 読めなくても判断そのものは行う（記録が無いこともある）
    async def _read(fn: Any, label: str, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, **kwargs)
        except bq.BigQueryUnavailable:
            logger.warning("consultation_read_failed: %s", label)
            return None

    health_row, recent_visits = await asyncio.gather(
        _read(health.fetch_latest, "health", survivor_id=survivor_id, org_id=org_id),
        _read(visits.list_visits, "visits", survivor_id=survivor_id, org_id=org_id, limit=3),
    )

    judged = await asyncio.to_thread(
        consultation_ai.triage,
        raw_text=raw_text,
        chosen_category=chosen_category,
        survivor=survivor,
        health_row=health_row,
        recent_visits=recent_visits or [],
    )

    try:
        row = consultations.build_row(
            survivor_id=survivor_id,
            org_id=org_id,
            created_by=user.uid,
            created_role=str(user.role),
            raw_text=raw_text,
            category=judged["category"],
            items=judged["items"],
            emergency_flag=bool(judged["emergency_flag"]),
            urgency=judged["urgency"],
            urgency_evidence=judged.get("urgency_evidence"),
            ai_used=bool(judged["ai_used"]),
        )
    except consultations.ConsultationValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(consultations.save, row)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    consultation_id = str(saved.get("consultation_id"))
    logger.info(
        "consultation_saved: urgency=%s emergency=%s ai_used=%s",
        saved.get("urgency"),
        saved.get("emergency_flag"),
        saved.get("ai_used"),
    )

    # F4 の判断は、**保存の後、応答を返す前に行う**（要件定義書10-5の注）。
    # 失敗しても相談の送信は成功のままにする（`risk_ai.assess_and_save` が例外を外に出さない）。
    # 保存した相談そのものを渡す（`stg_consultations` を読み直さずに済ませる。
    # 読み直すと、追記したばかりの行が入っていないことがある）
    if org_id:
        await asyncio.to_thread(
            risk_ai.assess_and_save,
            survivor=survivor,
            created_by=user.uid,
            created_role=str(user.role),
            trigger_id=consultation_id,
            recent_consultations=consultations.recent_for_ai([_as_stg_row(saved)]),
        )

    return {
        "consultation": consultations.payload(
            _as_stg_row(saved), role=str(user.role), public=not org_id
        ),
        # 送ったあとも案内は出し続ける（返事を待つ間に悪くなることがある）
        "emergency_guide": consultations.EMERGENCY_GUIDE,
        # AIが「急を要する」と見たときは、送れたという画面にも出す（要件定義書F8）
        "emergency_notice": (
            consultations.EMERGENCY_NOTICE if saved.get("emergency_flag") else None
        ),
    }


def _as_stg_row(saved: dict[str, Any]) -> dict[str, Any]:
    """保存した行を `stg_consultations` の1行と同じ形にする（読み直さずに返すため）。"""
    return {
        **saved,
        "consulted_at": saved.get("created_at"),
        "consulted_by": saved.get("created_by"),
        "consulted_role": saved.get("created_role"),
        "status": consultations.STATUS_OPEN,
        "reply_count": 0,
        "is_unanswered": True,
    }


# --- F8-3. 組織の相談の一覧（専門職のホーム） ---


@router.get("/consultations")
async def list_consultations(
    user: auth.CurrentUser = Depends(auth.require_roles(users.ROLE_PROFESSIONAL)),
) -> dict[str, Any]:
    """組織の相談を、未対応から・急ぎの順に返す（要件定義書F8・10-5）。

    **担当の専門職という考え方はこのアプリに無い**（F1 で担当に割り当てられるのは一般の
    支援者だけ）。専門職は組織の記録をすべて見られるため、組織の専門職全員に同じ一覧を出す
    （開発手順書 Step 8-3 と噛み合わない点として、鈴木さんへの確認事項に入れた）。
    """
    try:
        rows = await asyncio.to_thread(consultations.list_for_org, org_id=str(user.org_id))
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    items = [consultations.payload(row, role=str(user.role)) for row in rows]
    return {
        "consultations": items,
        "unanswered_count": sum(1 for item in items if item["is_unanswered"]),
        "statuses": [
            {"value": value, "label": label} for value, label in consultations.STATUSES.items()
        ],
    }


# --- F8-4. 返事と相談の状況 ---


@router.post(
    "/survivors/{survivor_id}/consultations/{consultation_id}/replies",
    status_code=status.HTTP_201_CREATED,
)
async def reply_consultation(
    survivor_id: str,
    consultation_id: str,
    body: ReplyRequest,
    user: auth.CurrentUser = Depends(auth.require_roles(users.ROLE_PROFESSIONAL)),
) -> dict[str, Any]:
    """専門職が相談に返事と次の対応を書く（本人の画面に出る。要件定義書F8）。

    返事の行の組織は、本人の記録の**いまの**組織にする（組織がないときに送った相談でも、
    本人がこの組織につないだあとはこの組織の相談として扱う。追加修正②⑦）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = _survivor_org(survivor)
    current = await _load_consultation(survivor_id=survivor_id, consultation_id=consultation_id)
    return await _save_reply(
        survivor_id=survivor_id,
        consultation_id=consultation_id,
        org_id=org_id,
        current=current,
        body=body,
        user=user,
        payload=lambda row: consultations.payload(row, role=str(user.role)),
    )


async def _save_reply(
    *,
    survivor_id: str,
    consultation_id: str,
    org_id: str,
    current: dict[str, Any],
    body: ReplyRequest,
    user: auth.CurrentUser,
    payload: Any,
) -> dict[str, Any]:
    """返事を1行足し、画面に返す形を作る（組織の相談と公開の相談で同じ形にする）。"""
    try:
        event = consultations.build_event_row(
            survivor_id=survivor_id,
            org_id=org_id,
            consultation_id=consultation_id,
            created_by=user.uid,
            created_role=str(user.role),
            action=consultations.ACTION_REPLIED,
            reply_text=body.reply_text,
            next_action=body.next_action,
            status=body.status,
        )
    except consultations.ConsultationValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(consultations.save_event, event)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("consultation_replied: status=%s public=%s", saved.get("status"), not org_id)
    return {
        "consultation": payload(
            {
                **current,
                "status": saved.get("status") or current.get("status"),
                "reply_count": int(current.get("reply_count") or 0) + 1,
                "last_reply_text": saved.get("reply_text"),
                "last_next_action": saved.get("next_action"),
                "last_replied_at": saved.get("created_at"),
                "is_unanswered": False,
            }
        )
    }


@router.patch("/survivors/{survivor_id}/consultations/{consultation_id}")
async def change_consultation_status(
    survivor_id: str,
    consultation_id: str,
    body: StatusRequest,
    user: auth.CurrentUser = Depends(auth.require_roles(users.ROLE_PROFESSIONAL)),
) -> dict[str, Any]:
    """専門職が相談の状況（未対応／対応中／対応済み）を変える（要件定義書F8）。

    raw は追記だけなので、前の状況は消さず1行足す（`stg_consultations` が最新を計算する）。
    """
    survivor = await shared.load_visible(survivor_id, user)
    org_id = _survivor_org(survivor)
    current = await _load_consultation(survivor_id=survivor_id, consultation_id=consultation_id)

    try:
        event = consultations.build_event_row(
            survivor_id=survivor_id,
            org_id=org_id,
            consultation_id=consultation_id,
            created_by=user.uid,
            created_role=str(user.role),
            action=consultations.ACTION_STATUS_CHANGED,
            status=body.status,
        )
    except consultations.ConsultationValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(consultations.save_event, event)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("consultation_status_changed: status=%s", saved.get("status"))
    return {
        "consultation": consultations.payload(
            {**current, "status": saved.get("status")}, role=str(user.role)
        )
    }


# --- F8-5. 相談のやり取り（要望7。要件定義書には無い追加の機能） ---

# 本人と、同じ組織の専門職が見られる（担当の一般の支援者は、相談そのものを扱わない。
# 要件定義書446行「施設の情報（F5）と支援制度の候補（F6）は扱わない」と同じ考え方で、
# 相談（F8）も一般の支援者の立場には出していない）。組織がない本人も自分の相談は見られる
# （追加修正②）。
can_view_thread = auth.require_roles_or_self(users.ROLE_SURVIVOR, users.ROLE_PROFESSIONAL)


@router.get("/survivors/{survivor_id}/consultations/{consultation_id}/messages")
async def list_messages(
    survivor_id: str,
    consultation_id: str,
    user: auth.CurrentUser = Depends(can_view_thread),
) -> dict[str, Any]:
    """相談のやり取り（返事・本人の書き足し）を古い順に返す（要望7）。"""
    await shared.load_visible(survivor_id, user)
    # 実在し、その人の相談であることを確かめる（無いものは404。要件定義書10-3と同じ考え方）
    await _load_consultation(survivor_id=survivor_id, consultation_id=consultation_id)
    return await _list_messages(survivor_id=survivor_id, consultation_id=consultation_id)


async def _list_messages(*, survivor_id: str, consultation_id: str) -> dict[str, Any]:
    try:
        rows = await asyncio.to_thread(
            consultations.list_events,
            consultation_id=consultation_id,
            survivor_id=survivor_id,
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    return {"messages": [consultations.event_payload(row) for row in rows]}


@router.post(
    "/survivors/{survivor_id}/consultations/{consultation_id}/messages",
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    survivor_id: str,
    consultation_id: str,
    body: MessageRequest,
    user: auth.CurrentUser = Depends(survivor_only),
) -> dict[str, Any]:
    """本人が、同じ相談に書き足す（要望7。続けて伝えたいときに新しい相談を送らずに済む）。"""
    survivor = await shared.load_visible(survivor_id, user)
    org_id = _survivor_org(survivor)
    await _load_consultation(survivor_id=survivor_id, consultation_id=consultation_id)

    try:
        event = consultations.build_event_row(
            survivor_id=survivor_id,
            org_id=org_id,
            consultation_id=consultation_id,
            created_by=user.uid,
            created_role=str(user.role),
            action=consultations.ACTION_MESSAGE,
            reply_text=body.message,
        )
    except consultations.ConsultationValidationError as exc:
        raise shared.bad_request(str(exc)) from exc

    try:
        saved = await asyncio.to_thread(consultations.save_event, event)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    logger.info("consultation_message_sent")
    return {"message": consultations.event_payload(saved)}


# --- F8-6. 公開の相談（追加修正②。組織につながっていない本人の相談） ---

# 一覧とやり取りは、ログインした支援者全員（組織に入っていない支援者も含む）が見られる。
public_readers = auth.require_roles_without_org(users.ROLE_PROFESSIONAL, users.ROLE_VOLUNTEER)
# 返事を書けるのは専門職だけ（組織は問わない）。健康のことに一般の支援者が答えるのを避ける
public_repliers = auth.require_roles_without_org(users.ROLE_PROFESSIONAL)


async def _load_public(consultation_id: str) -> dict[str, Any]:
    """公開の相談を1件読む（無いもの・公開でないものは同じ 404）。"""
    try:
        row = await asyncio.to_thread(
            consultations.fetch_public, consultation_id=consultation_id
        )
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc
    if not row:
        raise shared.not_found()
    return row


@router.get("/consultations/public")
async def list_public_consultations(
    user: auth.CurrentUser = Depends(public_readers),
) -> dict[str, Any]:
    """公開の相談を、未対応から・急ぎの順に返す（追加修正②）。

    **氏名・年齢・被災者のIDは返さない。** 出すのは住んでいる市区町村の団体コードまで。
    """
    try:
        rows = await asyncio.to_thread(consultations.list_public)
    except bq.BigQueryUnavailable as exc:
        raise shared.unavailable() from exc

    items = [consultations.public_payload(row) for row in rows]
    return {
        "consultations": items,
        "unanswered_count": sum(1 for item in items if item["is_unanswered"]),
        # 返事を書けるのは専門職だけ（画面はこれで返事の欄を出すか決める）
        "can_reply": user.role == users.ROLE_PROFESSIONAL,
    }


@router.get("/consultations/public/{consultation_id}/messages")
async def list_public_messages(
    consultation_id: str,
    _user: auth.CurrentUser = Depends(public_readers),
) -> dict[str, Any]:
    """公開の相談のやり取りを古い順に返す（追加修正②）。"""
    current = await _load_public(consultation_id)
    return await _list_messages(
        survivor_id=str(current.get("survivor_id") or ""), consultation_id=consultation_id
    )


@router.post(
    "/consultations/public/{consultation_id}/replies",
    status_code=status.HTTP_201_CREATED,
)
async def reply_public_consultation(
    consultation_id: str,
    body: ReplyRequest,
    user: auth.CurrentUser = Depends(public_repliers),
) -> dict[str, Any]:
    """専門職が公開の相談に返事を書く（組織は問わない。追加修正②）。

    返事の行は組織のIDを空にして残す（公開の相談のやり取りのため）。本人がのちに組織に
    つないだあとも、やり取りは相談のIDで読むので、本人と新しい組織の専門職に見える。
    """
    current = await _load_public(consultation_id)
    return await _save_reply(
        survivor_id=str(current.get("survivor_id") or ""),
        consultation_id=consultation_id,
        org_id="",
        current=current,
        body=body,
        user=user,
        payload=consultations.public_payload,
    )
