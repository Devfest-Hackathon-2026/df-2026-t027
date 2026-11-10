"""`/__/auth/*` を Firebase へ中継する（iPhone の Safari 対策。要件定義書10-2の5）。

なぜ必要か：Firebase のログイン用のページ（`/__/auth/handler` ほか）が
`〜.firebaseapp.com` にあると、**ログインの途中の状態をアプリのドメインから読めない**。
スマートフォンのブラウザはサイトごとに保存領域を分けるため、戻ってきたときに
「missing initial state」でログインが失敗する（練習で発生）。

そこで本番では `authDomain` をアプリのドメインにし（`/api/public-config`）、
`https://アプリのドメイン/__/auth/*` への要求をアプリがそのまま Firebase へ中継して、
応答を返す。Firebase 公式の signInWithRedirect のベストプラクティスの方法3。

安全のために守ること：

- 中継先は `.env` の `FIREBASE_AUTH_DOMAIN`（`〜.firebaseapp.com` か `〜.web.app`）**だけ**。
  要求の中のホスト名は使わない（任意のURLへ中継できると、サーバーを踏み台にされる）。
- パスに `..` が入っているものは中継しない。
- 中継のログに Cookie・トークン・クエリの中身を出さない（パスと状態の番号だけ）。
"""

from __future__ import annotations

import logging
import re

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status

from backend.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()

# 中継してよい中継先（Firebase が配るドメインだけ）。
ALLOWED_AUTH_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9.\-]*\.(firebaseapp\.com|web\.app)$")

# 中継の打ち切り。ログインの画面が出ないまま長く待たせない。
PROXY_TIMEOUT_SECONDS = 20.0

# 中継のときに送らない要求のヘッダ（接続ごとのもの。中継先で付け直される）。
SKIP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "accept-encoding",
    "upgrade",
    "proxy-connection",
    "te",
    "trailer",
}

# 返すときに通さない応答のヘッダ（中身の長さと圧縮はこちらで付け直す）。
SKIP_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "alt-svc",
    "trailer",
}

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """中継に使う HTTP クライアント（プロセスで1つだけ作る）。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=PROXY_TIMEOUT_SECONDS,
            follow_redirects=False,  # 3xx はブラウザにそのまま返す
        )
    return _client


async def close_client() -> None:
    """終了時に閉じる（main.py の lifespan から呼ぶ）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def upstream_base() -> str:
    """中継先のもと（`https://〜.firebaseapp.com`）。設定が怪しいときは中継しない。"""
    domain = (get_settings().firebase_auth_domain or "").strip().lower()
    if not ALLOWED_AUTH_DOMAIN.match(domain):
        logger.warning("auth_proxy_disabled: auth_domain_not_allowed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ログインの中継先が設定されていません",
        )
    return f"https://{domain}"


@router.api_route(
    "/__/auth/{path:path}",
    methods=["GET", "POST", "HEAD", "OPTIONS"],
    # アプリのAPIではないので /docs には出さない（出すと操作IDが重なって警告が出る）
    include_in_schema=False,
)
async def proxy_auth(path: str, request: Request) -> Response:
    """`/__/auth/*` をそのまま Firebase へ中継し、応答をそのまま返す。"""
    if ".." in path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="パスが正しくありません")

    url = f"{upstream_base()}/__/auth/{path}"
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in SKIP_REQUEST_HEADERS
    }
    body = await request.body()

    try:
        upstream = await get_client().request(
            request.method,
            url,
            params=request.query_params,
            headers=headers,
            content=body or None,
        )
    except httpx.HTTPError as exc:
        logger.warning("auth_proxy_failed: path=%s error=%s", path, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ログインの画面を読み込めませんでした。もう一度お試しください",
        ) from exc

    response = Response(content=upstream.content, status_code=upstream.status_code)
    # Set-Cookie は複数くることがあるので、1つずつ足す（辞書にすると1つに潰れる）。
    for name, value in upstream.headers.multi_items():
        if name.lower() in SKIP_RESPONSE_HEADERS:
            continue
        response.headers.append(name, value)
    logger.info("auth_proxy: path=%s status=%d", path, upstream.status_code)
    return response
