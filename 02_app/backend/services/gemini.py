"""Gemini の共通処理（要件定義書10-2の4、開発手順書 Step 1-3）。

- Google AI Studio の API キー（無料枠）で呼ぶ。キーは環境変数 GEMINI_API_KEY だけから読む。
  無料枠では、送った内容が Google の製品の改善に使われるため、架空の人物のデータだけを入れる。
- クライアントは起動時に1回だけ作る。
- 構造化出力（Pydantic のモデルをスキーマとして渡し、JSON で受け取る）。
- 打ち切りは30秒。429・5xx・タイムアウトのときは最大2回やり直す。
"""

from __future__ import annotations

import logging
import time
from typing import Any, TypeVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from backend.config import PROMPTS_DIR, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# やり直す HTTP の状態（要件定義書10-2の4）
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class GeminiUnavailable(RuntimeError):
    """Gemini を呼べなかった。呼び出し側は手入力の欄を出して続けられるようにする（F2）。"""


_client: genai.Client | None = None


def build_client() -> genai.Client:
    """Gemini API のクライアントを AI Studio の API キーで作る。"""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiUnavailable("GEMINI_API_KEY が設定されていません（.env を確認してください）")
    return genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(timeout=settings.gemini_timeout_seconds * 1000),
    )


def init_client() -> genai.Client | None:
    """起動時に1回だけ呼ぶ。失敗してもアプリは起動させる（手入力で続けられるようにするため）。"""
    global _client
    if _client is not None:
        return _client
    started = time.monotonic()
    try:
        _client = build_client()
    except Exception as exc:  # 認証切れ・設定漏れなど
        logger.warning("gemini_client_init_failed: %s", exc)
        return None
    logger.info("gemini_client_ready elapsed_ms=%d", int((time.monotonic() - started) * 1000))
    return _client


def get_client() -> genai.Client:
    """作ってあるクライアントを返す。無ければ作り直す。"""
    client = _client or init_client()
    if client is None:
        raise GeminiUnavailable("Gemini のクライアントを作れませんでした（.env の GEMINI_API_KEY を確認してください）")
    return client


def load_prompt(name: str) -> str:
    """指示文を読む。処理ごとに 02_app/backend/prompts/<name>.md に置く（要件定義書10-6）。"""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"指示文が見つかりません: {path}")
    return path.read_text(encoding="utf-8")


def _status_of(exc: genai_errors.APIError) -> int | None:
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def generate_structured(
    *,
    schema: type[T],
    contents: str,
    system_instruction: str | None = None,
    temperature: float = 0.0,
    operation: str = "gemini",
    thinking_budget: int | None = None,
) -> T:
    """構造化出力で1回の問い合わせを行い、スキーマどおりのオブジェクトを返す。

    Args:
        schema: 返させる形（Pydantic のモデル）
        contents: 渡す本文
        system_instruction: 守らせること（指示文）
        temperature: 既定は0。案の揺れを小さくする
        operation: ログに出す処理の名前。記録の本文はログに出さない
        thinking_budget: 「考える時間」の上限。0 にすると考えずにすぐ答える。
            項目に振り分けるだけの処理では**速さが大きく変わる**（F2 で 6.5秒→1.3秒）。
            モデルによっては 0 を指定できないため、断られたら**付けずにやり直す**
            （`.env` のモデルを変えても落ちないようにするため）。
    """
    settings = get_settings()
    if not settings.gemini_model:
        raise GeminiUnavailable("GEMINI_MODEL が設定されていません（.env を確認してください）")

    client = get_client()

    def build_config(budget: int | None) -> types.GenerateContentConfig:
        options: dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_schema": schema,
            "temperature": temperature,
            "system_instruction": system_instruction,
        }
        if budget is not None:
            options["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        return types.GenerateContentConfig(**options)

    config = build_config(thinking_budget)
    thinking_requested = thinking_budget is not None

    last_error: Exception | None = None
    for attempt in range(settings.gemini_max_retries + 1):
        started = time.monotonic()
        try:
            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=contents,
                config=config,
            )
        except genai_errors.APIError as exc:
            status = _status_of(exc)
            last_error = exc
            if status == 400 and thinking_requested:
                # 「考える時間」を指定できないモデルだった。付けずにやり直す
                logger.warning(
                    "gemini_thinking_not_supported operation=%s model=%s",
                    operation, settings.gemini_model,
                )
                config = build_config(None)
                thinking_requested = False
                continue
            if status in RETRYABLE_STATUS and attempt < settings.gemini_max_retries:
                wait = 2**attempt
                logger.warning(
                    "gemini_retry operation=%s status=%s attempt=%d wait_s=%d",
                    operation, status, attempt + 1, wait,
                )
                time.sleep(wait)
                continue
            raise GeminiUnavailable(f"Gemini の呼び出しに失敗しました（status={status}）") from exc
        except Exception as exc:  # タイムアウト・接続断など
            last_error = exc
            if attempt < settings.gemini_max_retries:
                wait = 2**attempt
                logger.warning(
                    "gemini_retry operation=%s error=%s attempt=%d wait_s=%d",
                    operation, type(exc).__name__, attempt + 1, wait,
                )
                time.sleep(wait)
                continue
            raise GeminiUnavailable(f"Gemini の呼び出しに失敗しました（{type(exc).__name__}）") from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        parsed: Any = response.parsed
        if not isinstance(parsed, schema):
            logger.warning("gemini_parse_failed operation=%s elapsed_ms=%d", operation, elapsed_ms)
            raise GeminiUnavailable("Gemini の返事をスキーマどおりに読み取れませんでした")
        logger.info("gemini_ok operation=%s elapsed_ms=%d", operation, elapsed_ms)
        return parsed

    raise GeminiUnavailable(f"Gemini の呼び出しに失敗しました（{last_error}）")
