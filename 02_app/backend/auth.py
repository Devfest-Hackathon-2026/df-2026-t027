"""IDトークンの確認と立場の決定（要件定義書F0・10-3、開発手順書 Step 4-2・4-5）。

守ること：

- バックエンドへのすべての要求で `Authorization: Bearer <IDトークン>` を確かめ、
  確かめられないものは 401 で断る（要件定義書F0）。
- **立場と組織は毎回 `stg_users` から決める。要求の中に書かれた立場や組織のIDは使わない**
  （要件定義書10-3）。キャッシュは入れていない。入れると立場が古いまま通る経路ができるため。
- ログに記録の本文・健康の情報・電話番号・トークンを出さない（要件定義書10-2の6）。

`verify_firebase_token` は呼ぶたびに Google の証明書を HTTP で取り直す。
要求ごとに外へ出ると遅いので、取得だけを1時間キャッシュする transport をはさむ
（`_CachedCertsRequest`）。確認そのものは google-auth に任せる。

この依存関数は **同期の `def`** にしてある。中で同期の HTTP（`requests`）を使うため、
async にするとイベントループを塞ぐ。FastAPI は同期の依存を別のスレッドで動かす。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from fastapi import Depends, Header, HTTPException, Request, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from backend import request_log
from backend.config import get_settings
from backend.services import bq, users

logger = logging.getLogger(__name__)

# 証明書を持っておく時間（Google の証明書は数日単位で入れ替わるため1時間で十分）。
CERTS_TTL_SECONDS = 3600

# 証明書の取得の打ち切り（既定の120秒だと、取れないときに要求が長く止まる）。
CERTS_TIMEOUT_SECONDS = 10

# Firebase の IDトークンの発行者。audience はプロジェクトIDになる。
ISSUER_PREFIX = "https://securetoken.google.com/"

# 時計のずれを許す秒数。
# 発行されたばかりのトークンは、`iat`（発行時刻）がこちらの時計より数秒先になることがある。
# google-auth の既定は 0 秒で、1秒先でも `Token used too early` で弾く。そのため
# **ログインした直後だけ 401 になり、数秒待つと通る**という分かりにくい形で出る
# （実機で発生。`iat` がこの端末の時計より1秒先だった）。
# 長くしすぎると期限切れのトークンも通るので、1分にとどめる。
CLOCK_SKEW_SECONDS = 60

# Firebase の sign_in_provider を、raw_users の login_method の値に対応づける。
# 電話番号そのものは保存しない（要件定義書7-2）。
LOGIN_METHODS = {"google.com": "google", "phone": "phone"}


class _CachedResponse:
    """google-auth の transport が期待する応答の形（status・headers・data）。"""

    def __init__(self, status_code: int, data: bytes) -> None:
        self.status = status_code
        self.headers: dict[str, str] = {}
        self.data = data


class _CachedCertsRequest:
    """GET の応答を一定時間持っておく transport（証明書の取得が要求ごとに走るのを防ぐ）。"""

    def __init__(self) -> None:
        self._inner = google_requests.Request()
        self._cache: dict[str, tuple[float, int, bytes]] = {}
        self._lock = threading.Lock()

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> _CachedResponse:
        cacheable = method == "GET" and body is None
        now = time.monotonic()
        if cacheable:
            with self._lock:
                cached = self._cache.get(url)
            if cached is not None and now - cached[0] < CERTS_TTL_SECONDS:
                return _CachedResponse(cached[1], cached[2])

        response = self._inner(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=timeout if timeout is not None else CERTS_TIMEOUT_SECONDS,
            **kwargs,
        )
        data = response.data
        if cacheable and response.status == 200:
            with self._lock:
                self._cache[url] = (time.monotonic(), response.status, data)
        return _CachedResponse(response.status, data)


_certs_request: _CachedCertsRequest | None = None


def get_certs_request() -> _CachedCertsRequest:
    """証明書を取る transport（プロセスで1つだけ作る）。"""
    global _certs_request
    if _certs_request is None:
        _certs_request = _CachedCertsRequest()
    return _certs_request


@dataclass(frozen=True)
class AuthenticatedUser:
    """IDトークンを確かめられた利用者（まだ立場は分からない）。"""

    uid: str
    login_method: str


@dataclass(frozen=True)
class CurrentUser:
    """立場と組織まで決まった利用者（stg_users から毎回読む）。"""

    uid: str
    login_method: str
    role: str | None = None
    org_id: str | None = None
    org_name: str | None = None
    profession: str | None = None
    display_name: str | None = None
    linked_survivor_id: str | None = None
    # 支援者の連絡先・活動する市区町村・住所（追加修正④）。新しい版の行を足すときに
    # 引き継ぐためと、本人の `/api/me` に返すために持つ。連絡先と住所は個人の情報なので、
    # 誤って利用者の値をログに出しても混ざらないよう repr に出さない。
    contact: str | None = field(default=None, repr=False)
    municipality_code: str | None = None
    address: str | None = field(default=None, repr=False)
    # 組織の中の権限（追加修正⑥）。今は owner か空
    org_permission: str | None = None
    # 施設の職員の、自分の施設のID（使いやすさの改修の計画.md 9章）。ほかの立場では空
    facility_id: str | None = None

    @property
    def is_owner(self) -> bool:
        """組織のオーナーか（追加修正⑥）。組織に入っている専門職で、権限が owner のときだけ。"""
        return users.is_owner(
            {"org_id": self.org_id, "role": self.role, "org_permission": self.org_permission}
        )

    @property
    def has_role(self) -> bool:
        """立場（本人・専門職・一般の支援者）を選んでいるか（要望1）。

        組織に入っていなくても true になる。画面はこれで「ホームに進めるか」を決める
        （`/api/me` の `registered`）。組織がない一般の支援者は、ホーム・プロフィール・
        組織に入る・組織を作るだけができる。
        """
        return bool(self.role)

    @property
    def has_org(self) -> bool:
        """組織に入っているか（要望1）。"""
        return bool(self.org_id)

    @property
    def is_registered(self) -> bool:
        """初回の登録（F0のオンボーディング）が済み、組織にも入っているか。

        記録を読み書きするAPI（`registered_user`・`require_roles`）はここを使う。
        組織がない支援者はここが false になり、記録には触れない（要望1）。
        """
        return bool(self.role and self.org_id)


def _unauthorized(detail: str) -> HTTPException:
    # WWW-Authenticate を付ける（画面はこの 401 を見てログインし直す）
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def extract_bearer_token(authorization: str | None) -> str:
    """Authorization ヘッダ（Bearer <トークン>）からトークンだけを取り出す。"""
    if not authorization:
        raise _unauthorized("ログインの証明（IDトークン）がありません")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise _unauthorized("ログインの証明の形が正しくありません")
    return parts[1].strip()


def verify_id_token(token: str) -> AuthenticatedUser:
    """Firebase の IDトークンを確かめ、利用者のIDとログインの方法を返す。

    確かめられないときは 401。トークンの中身はログに出さない。
    """
    settings = get_settings()
    if not settings.project_id:
        # 設定漏れ。利用者に 401 を返すのは誤解を招くため 500 にする。
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GOOGLE_CLOUD_PROJECT が設定されていません",
        )
    try:
        claims = id_token.verify_firebase_token(
            token,
            get_certs_request(),
            audience=settings.project_id,
            clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
        )
    except Exception as exc:  # 期限切れ・署名の誤り・audience 違い・証明書が取れない
        logger.info("auth_rejected: reason=%s", type(exc).__name__)
        raise _unauthorized("ログインの証明を確かめられませんでした") from exc

    if not isinstance(claims, dict):
        raise _unauthorized("ログインの証明を確かめられませんでした")

    # verify_firebase_token は発行者（iss）を見ない。別のプロジェクトのトークンを
    # 弾くために、ここで自分のプロジェクト宛てであることを確かめる。
    if claims.get("iss") != f"{ISSUER_PREFIX}{settings.project_id}":
        logger.info("auth_rejected: reason=issuer")
        raise _unauthorized("ログインの証明の発行元が違います")

    uid = claims.get("sub") or claims.get("user_id") or ""
    if not isinstance(uid, str) or not uid.strip():
        raise _unauthorized("ログインの証明に利用者のIDがありません")

    provider = (claims.get("firebase") or {}).get("sign_in_provider")
    login_method = LOGIN_METHODS.get(provider, "other")
    return AuthenticatedUser(uid=uid.strip(), login_method=login_method)


# --- FastAPI の依存 ---


def authenticated_user(
    request: Request, authorization: str | None = Header(default=None)
) -> AuthenticatedUser:
    """IDトークンを確かめるだけ（初回の登録がまだの利用者も通る）。"""
    user = verify_id_token(extract_bearer_token(authorization))
    # ログに出す利用者のIDを覚えておく（開発手順書 Step 12-2）
    request_log.remember_user(request, user_id=user.uid)
    return user


def current_user(
    request: Request, user: AuthenticatedUser = Depends(authenticated_user)
) -> CurrentUser:
    """立場と組織を stg_users から決める（要求に書かれた値は使わない）。"""
    try:
        row = users.fetch_user(user.uid)
    except bq.BigQueryUnavailable as exc:
        logger.warning("auth_user_lookup_failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="利用者の情報を読めませんでした。少し待ってやり直してください",
        ) from exc
    if row is None:
        return CurrentUser(uid=user.uid, login_method=user.login_method)
    # ログに出す立場と組織を覚えておく（開発手順書 Step 12-2）
    request_log.remember_user(request, role=row.get("role"), org_id=row.get("org_id"))
    return CurrentUser(
        uid=user.uid,
        login_method=user.login_method,
        role=row.get("role"),
        org_id=row.get("org_id"),
        org_name=row.get("org_name"),
        profession=row.get("profession"),
        display_name=row.get("display_name"),
        linked_survivor_id=row.get("linked_survivor_id"),
        contact=row.get("contact"),
        municipality_code=row.get("municipality_code"),
        address=row.get("address"),
        org_permission=row.get("org_permission"),
        facility_id=row.get("facility_id"),
    )


def _refuse_facility_staff(user: CurrentUser) -> None:
    """施設の職員を、被災者の記録に触れるAPIから締め出す（使いやすさの改修の計画.md 9章）。

    施設の職員は組織（施設を登録した組織）を持つので、`is_registered` だけで判断すると
    記録のAPIの入口を通ってしまう。立場の表（`require_roles`）に載せ忘れても通らないよう、
    記録に触れる依存の根元（`registered_user`・`registered_or_self`）でまとめて断る。
    """
    if user.role == users.ROLE_FACILITY_STAFF:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="この操作は、いまの立場では行えません",
        )


def registered_user(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """初回の登録が済んだ利用者だけを通す（済んでいなければ 403）。

    施設の職員は、ここで 403 にする（被災者の記録には触れない。9章）。
    """
    if not user.is_registered:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="初回の登録が済んでいません",
        )
    _refuse_facility_staff(user)
    return user


def require_roles(*allowed: str) -> Callable[[CurrentUser], CurrentUser]:
    """使える立場を限る依存を作る（使えない立場は 403。要件定義書10-3）。

    組織に入っていることも求める（`registered_user`）。組織がなくても使えるAPI
    （プロフィール・組織に入る・組織を作る。要望1）は `require_roles_without_org` を使う。
    """
    permitted: Iterable[str] = tuple(allowed)

    def dependency(user: CurrentUser = Depends(registered_user)) -> CurrentUser:
        if user.role not in permitted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="この操作は、いまの立場では行えません",
            )
        return user

    return dependency


# 組織がなくても、自分の記録だけは見られる立場（要望8・11章の要確認E-1）。
# 住んでいる市区町村にまだ組織がない本人。見られる範囲は `survivors.can_view` が本人の
# 記録だけに絞る（組織で絞る支援者とは違い、組織が空でも他人の記録は見えない）。
SELF_RECORD_ROLE = "survivor"


def registered_or_self(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """`registered_user` と同じだが、組織がない本人も通す（自分の記録を見る・直すAPIだけに使う）。"""
    _refuse_facility_staff(user)
    if user.is_registered or (user.has_role and user.role == SELF_RECORD_ROLE):
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="初回の登録が済んでいません",
    )


def require_roles_or_self(*allowed: str) -> Callable[[CurrentUser], CurrentUser]:
    """`require_roles` と同じだが、組織がない本人も通す（`registered_or_self`）。"""
    permitted: Iterable[str] = tuple(allowed)

    def dependency(user: CurrentUser = Depends(registered_or_self)) -> CurrentUser:
        if user.role not in permitted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="この操作は、いまの立場では行えません",
            )
        return user

    return dependency


def require_roles_without_org(*allowed: str) -> Callable[[CurrentUser], CurrentUser]:
    """使える立場を限るが、組織が無くても通す依存（要望1）。

    組織がない支援者が使えるAPI（プロフィールの変更・組織に入る・組織を作る）だけに使う。
    記録に触れるAPIは、今までどおり `require_roles` を使う（組織を求める）。
    """
    permitted: Iterable[str] = tuple(allowed)

    def dependency(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not user.has_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="初回の登録が済んでいません",
            )
        if user.role not in permitted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="この操作は、いまの立場では行えません",
            )
        return user

    return dependency


def require_owner(
    user: CurrentUser = Depends(require_roles(users.ROLE_PROFESSIONAL)),
) -> CurrentUser:
    """組織のオーナーだけを通す（追加修正⑥。参加コードを見る・発行し直す・オーナーを付ける）。

    立場と同じく、権限も毎回 `stg_users` から読んだ値で決める（要求に書かれた値は使わない）。
    操作できるのは**自分の組織だけ**。組織のIDは要求から受け取らず、この利用者の組織を使う。
    """
    if not user.is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="この操作は、組織のオーナーだけが行えます",
        )
    return user


def require_facility_staff(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """施設の職員だけを通す（使いやすさの改修の計画.md 9章）。

    立場・組織・施設のIDは、ほかの依存と同じく毎回 `stg_users` から読んだ値で決める
    （要求に書かれた施設のIDは使わない）。施設か組織が空の行は、登録の途中で壊れたものと
    みなして通さない。直せるのは**この利用者の施設だけ**で、施設のIDを要求から受け取る
    方法はない（`/api/my-facility`）。
    """
    if not user.has_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="初回の登録が済んでいません",
        )
    if user.role != users.ROLE_FACILITY_STAFF or not user.facility_id or not user.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="この操作は、施設の職員だけが行えます",
        )
    return user
