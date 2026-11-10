"""要求ごとのログのテスト（開発手順書 Step 12-2、要件定義書10-2の6）。

Step 12 の「完了の確認」が求めているのは2つ。

- ログが**1行1つの JSON**で出ること（Cloud Logging がそのまま読める形）。
- **記録の本文や健康の情報がログに出ていないこと**。

出ていないことは目で見ても確かめられないので、記録の本文を送ったうえで、
その文字がログの行に1つも入っていないことをテストで確かめる。
"""

from __future__ import annotations

import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from backend import request_log
from backend.config import get_settings
from backend.main import app

# 送ってみる「記録の本文」と「健康の情報」。ログに出てはいけない文字。
SECRET_TEXT = "水が飲めていない。右足に痛みがあり立てない"
SECRET_HEALTH = "インスリン注射が1日2回"
SECRET_PHONE = "09012345678"


class Collector(logging.Handler):
    """`app.request` が出した行をそのまま集める。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gdgoc-test-project")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def lines():
    """ログの行を集める（元の出し先はそのまま残す）。"""
    collector = Collector()
    request_log.logger.addHandler(collector)
    yield collector.lines
    request_log.logger.removeHandler(collector)


@pytest.fixture
def client():
    return TestClient(app)


def entries(lines: list[str]) -> list[dict]:
    """集めた行を JSON として読み戻す（1行1つの JSON であることの確認も兼ねる）。"""
    return [json.loads(line) for line in lines]


# --- 形 ---


def test_ログは1行1つのJSON(client, lines):
    client.get("/api/me")

    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert isinstance(json.loads(lines[0]), dict)


def test_Step12が挙げている6つが入っている(client, lines):
    client.get("/api/me")

    entry = entries(lines)[0]
    # 時刻・処理の名前・利用者のIDと立場・組織のID・かかった時間・エラーの内容
    for key in ("time", "operation", "user_id", "role", "org_id", "duration_ms", "error"):
        assert key in entry, f"{key} がログに入っていません"
    assert entry["severity"] in ("INFO", "WARNING", "ERROR")
    assert isinstance(entry["duration_ms"], (int, float))


def test_処理の名前は道の形で出す_IDを含めない(client, lines):
    client.get("/api/survivors/sv_0001")

    entry = entries(lines)[0]
    assert entry["operation"] == "GET /api/survivors/{survivor_id}"
    assert "sv_0001" not in lines[0]


def test_ログインしていない要求は401として出る(client, lines):
    client.get("/api/me")

    entry = entries(lines)[0]
    assert entry["status"] == 401
    assert entry["error"] == "client_error:401"
    # 立場が決まる前なので空になる
    assert entry["user_id"] is None
    assert entry["role"] is None


def test_生存確認と画面の配信はログに出さない(client, lines):
    client.get("/api/health")
    client.get("/")

    assert lines == []


# --- 出してはいけないもの ---


def test_記録の本文をログに出さない(client, lines):
    client.post(
        "/api/survivors/sv_0001/visits",
        json={"note": SECRET_TEXT, "health_note": SECRET_HEALTH},
    )

    assert lines, "ログが出ていません（テストの前提が崩れています）"
    joined = "\n".join(lines)
    assert SECRET_TEXT not in joined
    assert SECRET_HEALTH not in joined


def test_IDトークンと電話番号をログに出さない(client, lines):
    client.post(
        "/api/onboarding/survivor",
        json={"phone": SECRET_PHONE},
        headers={"Authorization": f"Bearer secret-token-{SECRET_PHONE}"},
    )

    joined = "\n".join(lines)
    assert SECRET_PHONE not in joined
    assert "secret-token" not in joined


def test_クエリ文字列をログに出さない(client, lines):
    client.get(f"/api/survivors?q={SECRET_TEXT}")

    joined = "\n".join(lines)
    assert SECRET_TEXT not in joined
    assert "?" not in entries(lines)[0]["operation"]


def test_誤りの詳しい内容はログに出さない():
    """422 の詳細には送られた値が入るため、種類だけを出す。"""
    assert request_log._error_kind(422, None) == "validation_error"


# --- 組み立てと設定 ---


def test_エラーの種類の決め方():
    assert request_log._error_kind(200, None) is None
    assert request_log._error_kind(201, None) is None
    assert request_log._error_kind(403, None) == "client_error:403"
    assert request_log._error_kind(503, None) == "server_error:503"
    assert request_log._error_kind(500, ValueError("x")) == "unhandled:ValueError"


def test_build_entryは500をERRORにする():
    entry = request_log.build_entry(
        operation="POST /api/survivors",
        status_code=500,
        duration_ms=12.34,
        user_id="uid-1",
        role="professional",
        org_id="org_wajima",
        error="unhandled:RuntimeError",
    )
    assert entry["severity"] == "ERROR"
    assert entry["duration_ms"] == 12.3
    assert entry["message"] == "POST /api/survivors 500"


def test_build_entryは断りをWARNINGにする():
    entry = request_log.build_entry(
        operation="GET /api/dashboard",
        status_code=403,
        duration_ms=1.0,
        user_id="uid-1",
        role="volunteer",
        org_id="org_wajima",
        error="client_error:403",
    )
    assert entry["severity"] == "WARNING"


def test_setup_loggingは標準出力に整形せずに出す():
    request_log.setup_logging()

    assert request_log.logger.propagate is False, "root にも渡すと接頭辞つきで二重に出る"
    assert len(request_log.logger.handlers) == 1
    handler = request_log.logger.handlers[0]
    assert handler.stream is sys.stdout
    assert handler.formatter._fmt == "%(message)s"


def test_remember_userはrequestが無くても落ちない():
    request_log.remember_user(None, user_id="uid-1")
