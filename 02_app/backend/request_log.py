"""要求ごとのログ（開発手順書 Step 12-2、要件定義書10-2の6・11-3）。

Cloud Run の標準出力に、**1行1つの JSON** で出す。Cloud Logging は1行の JSON を
そのまま構造のあるログとして読み、`severity` を重大度として扱う。

出すもの（開発手順書 Step 12-2 が挙げている6つ）：

| 出すもの | 鍵 |
|---|---|
| 時刻 | `time`（UTC・ISO 8601） |
| 処理の名前 | `operation`（`POST /api/survivors/{survivor_id}/visits` の形） |
| 利用者のIDと立場 | `user_id`・`role` |
| 組織のID | `org_id` |
| かかった時間 | `duration_ms` |
| エラーの内容 | `error`（種類だけ。本文は入れない） |

**出さないもの**（要件定義書10-2の6）：

- 記録の本文・健康の情報・電話番号・氏名・IDトークン
- そのため、**実際のURLではなく道の形（`{survivor_id}` のまま）を出す**。クエリ文字列も出さない。
  被災者のIDも出さない（誰の記録を見たかはログの目的ではない）。
- 誤りの詳しい内容（422 のときに送られた値が入る）は出さない。種類だけを出す。

利用者のIDと立場は `backend/auth.py` が `request.state` に書く。ログイン前に断られた要求
（401）では空になる。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request, Response

# このロガーだけは、整形せずに message をそのまま標準出力に出す（JSON を壊さないため）。
LOGGER_NAME = "app.request"

logger = logging.getLogger(LOGGER_NAME)

# ログに出さない処理（Cloud Run の生存確認が毎分出ると、ほかのログが埋もれる）。
SKIP_OPERATIONS = frozenset({"GET /api/health"})

# ログを出す道。画面・画像・Firebase への中継は出さない。
LOG_PATH_PREFIX = "/api/"


def setup_logging() -> None:
    """`app.request` を、標準出力へ1行1つの JSON で出す形にする。"""
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # 親（root）にも渡すと、main.py の basicConfig が付ける接頭辞つきで二重に出る。
    logger.propagate = False
    quiet_url_loggers()


# 送った先のURLを丸ごと INFO で出す部品。Google ログインの中継（routers/auth_proxy.py）で、
# URLの中の1回だけ使える確認用のコード（code・state）がログに残っていた（2026-11-10）。
URL_LOGGERS = ("httpx", "httpcore")


def quiet_url_loggers() -> None:
    """URLを出す部品のログを、警告より上だけにする（問い合わせの部分をログに残さない）。"""
    for name in URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def operation_name(request: Request) -> str:
    """処理の名前。道は**形のまま**にして、IDを含めない。"""
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return f"{request.method} {path}"


def _error_kind(status_code: int, exc: BaseException | None) -> str | None:
    """エラーの種類だけを返す（送られた値や本文は入れない）。"""
    if exc is not None:
        return f"unhandled:{type(exc).__name__}"
    if status_code == 422:
        # 422 の詳細には送られた値が入るため、種類だけにする
        return "validation_error"
    if status_code >= 500:
        return f"server_error:{status_code}"
    if status_code >= 400:
        return f"client_error:{status_code}"
    return None


def build_entry(
    *,
    operation: str,
    status_code: int,
    duration_ms: float,
    user_id: str | None,
    role: str | None,
    org_id: str | None,
    error: str | None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """ログの1行を組み立てる（出す鍵をここ1か所に決める）。"""
    moment = now or dt.datetime.now(dt.timezone.utc)
    return {
        "severity": "ERROR" if status_code >= 500 else ("WARNING" if error else "INFO"),
        "time": moment.isoformat(),
        "message": f"{operation} {status_code}",
        "operation": operation,
        "status": status_code,
        "duration_ms": round(duration_ms, 1),
        "user_id": user_id,
        "role": role,
        "org_id": org_id,
        "error": error,
    }


def emit(entry: dict[str, Any]) -> None:
    """1行1つの JSON で出す。"""
    logger.info(json.dumps(entry, ensure_ascii=False))


def install(app: FastAPI) -> None:
    """要求ごとのログを出す middleware を付ける。"""

    @app.middleware("http")
    async def log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not request.url.path.startswith(LOG_PATH_PREFIX):
            return await call_next(request)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except BaseException as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            operation = operation_name(request)
            if operation not in SKIP_OPERATIONS:
                emit(
                    build_entry(
                        operation=operation,
                        status_code=500,
                        duration_ms=duration_ms,
                        user_id=getattr(request.state, "log_user_id", None),
                        role=getattr(request.state, "log_role", None),
                        org_id=getattr(request.state, "log_org_id", None),
                        error=_error_kind(500, exc),
                    )
                )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        operation = operation_name(request)
        if operation in SKIP_OPERATIONS:
            return response
        emit(
            build_entry(
                operation=operation,
                status_code=response.status_code,
                duration_ms=duration_ms,
                user_id=getattr(request.state, "log_user_id", None),
                role=getattr(request.state, "log_role", None),
                org_id=getattr(request.state, "log_org_id", None),
                error=_error_kind(response.status_code, None),
            )
        )
        return response


def remember_user(
    request: Request | None,
    *,
    user_id: str | None = None,
    role: str | None = None,
    org_id: str | None = None,
) -> None:
    """ログに出す利用者のIDと立場を覚えておく（`backend/auth.py` から呼ぶ）。

    `request.state` は scope と同じ辞書に入るため、依存の中で入れた値を
    middleware から読める。立場が決まる前（401）では空のままになる。
    """
    if request is None:
        return
    if user_id is not None:
        request.state.log_user_id = user_id
    if role is not None:
        request.state.log_role = role
    if org_id is not None:
        request.state.log_org_id = org_id
