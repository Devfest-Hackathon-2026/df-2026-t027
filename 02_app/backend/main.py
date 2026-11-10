"""FastAPI のアプリ本体（開発手順書 Step 1-2・Step 4）。

- `GET /api/health` は `{"ok": true}` を返す。
- F0（ログインと立場）の API は `routers/account.py`。
- F1（被災者の記録と担当）の API は `routers/survivors.py`。
- F2（訪問・見守りの記録）と F3（健康・介護の情報）の API は `routers/visits.py`。
- F4（悪化の兆しの度合いの変更）の API は `routers/assessments.py`。
- F5（受け入れ先の施設と移る先の決定）の API は `routers/facilities.py`。
- F3（引き継ぎシート）の API は `routers/handovers.py`。
- F6（受けられる支援と申請の状況）の API は `routers/supports.py`。
- F7（組織全体の状況）の API は `routers/dashboard.py`。
- F8（被災者からの相談）の API は `routers/consultations.py`。
- 周りの地域の状況（使いやすさの改修の計画.md 8章）の API は `routers/disaster_status.py`。
- 家族などによる代わりの登録とつなぎの API は `routers/family.py`（使いやすさの改修の計画.md 10章）。
- `/__/auth/*` は `routers/auth_proxy.py` が Firebase へ中継する（iPhone の Safari 対策）。
- `/` で 02_app/frontend/index.html を配る。
- `/healthz` は Cloud Run で予約されているため使わない。
- 要求ごとのログは `request_log.py`（1行1つの JSON。Step 12-2）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import request_log
from backend.config import FRONTEND_DIR, get_settings
from backend.routers import account, auth_proxy
from backend.routers import assessments as assessments_router
from backend.routers import consultations as consultations_router
from backend.routers import dashboard as dashboard_router
from backend.routers import disaster_status as disaster_status_router
from backend.routers import facilities as facilities_router
from backend.routers import family as family_router
from backend.routers import handovers as handovers_router
from backend.routers import supports as supports_router
from backend.routers import survivors as survivors_router
from backend.routers import visits as visits_router
from backend.services import gemini

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
# 要求ごとのログは1行1つの JSON で出す（開発手順書 Step 12-2）
request_log.setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時に Gemini のクライアントを1回だけ作る（作るのに約4.5秒かかるため）。"""
    settings = get_settings()
    missing = settings.missing_keys()
    if missing:
        logger.warning("env_missing keys=%s", ",".join(missing))
    gemini.init_client()
    try:
        yield
    finally:
        # 中継に使った HTTP クライアントを閉じる
        await auth_proxy.close_client()


app = FastAPI(title=get_settings().app_name, lifespan=lifespan)

# 要求ごとのログ（Step 12-2）。記録の本文や健康の情報は出さない
request_log.install(app)


# Firebase への中継（iPhone の Safari 対策）。ログインの途中（リダイレクトで戻って
# きた直後）にホストを移すと、ブラウザが保存した状態と原点がずれて失敗するため、
# この道だけは次のミドルウェアで移さない。
AUTH_PROXY_PREFIX = "/__/auth/"


@app.middleware("http")
async def redirect_to_canonical_host(request: Request, call_next):
    """公開URLを1つにそろえる（要望5。要件定義書に無い追加の仕組み）。

    Cloud Runは1サービスに2つのURLを必ず発行する。Firebaseのログインの状態は
    開いたドメインごとに別に保存されるため、2つを行き来すると毎回ログインし
    直しになる。`CANONICAL_HOST`（決まったほうのURLのドメイン）を設定したときだけ、
    もう一方で開かれた要求をそちらへ301で移す。
    """
    settings = get_settings()
    host = (request.headers.get("host") or "").split(":")[0]
    if (
        settings.canonical_host
        and host
        and host != settings.canonical_host
        and not request.url.path.startswith(AUTH_PROXY_PREFIX)
    ):
        target = request.url.replace(scheme="https", hostname=settings.canonical_host)
        return RedirectResponse(str(target), status_code=301)
    return await call_next(request)


@app.get("/api/health")
async def health() -> dict[str, bool]:
    """動作の確認。"""
    return {"ok": True}


# API と中継は、画面の配信より先に登録する（先に登録したほうが優先して照合される）
app.include_router(account.router)
app.include_router(survivors_router.router)
app.include_router(visits_router.router)
app.include_router(assessments_router.router)
app.include_router(consultations_router.router)
app.include_router(facilities_router.router)
app.include_router(handovers_router.router)
app.include_router(supports_router.router)
app.include_router(dashboard_router.router)
# 周りの地域の状況（使いやすさの改修の計画.md 8章）
app.include_router(disaster_status_router.router)
# 家族などによる代わりの登録とつなぎ（使いやすさの改修の計画.md 10章）
app.include_router(family_router.router)
app.include_router(auth_proxy.router)

# 画面の配信は最後に登録する（先に登録した /api/* のほうが優先して照合されるため）
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
