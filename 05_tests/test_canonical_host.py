"""公開URLを1つにそろえる仕組みのテスト（要望5。追加改修の計画.md 6章）。

Cloud Runは1サービスに2つのURLを必ず発行する。ログインの状態はURLごとに
別に保存されるため、`CANONICAL_HOST` を設定したときは、もう一方のURLで
開かれた要求を301で決まったほうへ移す（`backend/main.py` の
`redirect_to_canonical_host`）。`/__/auth/*`（Firebaseへの中継）は移さない
（ログインの途中で移すと失敗するため）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import get_settings
from backend.main import app

CANONICAL = "gdgoc-app-128905595686.asia-northeast1.run.app"
OTHER = "gdgoc-app-vhfyexox3a-an.a.run.app"


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    return TestClient(app, base_url="http://testserver", follow_redirects=False)


def test_canonical_hostを設定していなければ移さない(monkeypatch, client):
    monkeypatch.delenv("CANONICAL_HOST", raising=False)
    get_settings.cache_clear()
    res = client.get("/api/health", headers={"host": OTHER})
    assert res.status_code == 200


def test_決まったURLで開けば移さない(monkeypatch, client):
    monkeypatch.setenv("CANONICAL_HOST", CANONICAL)
    get_settings.cache_clear()
    res = client.get("/api/health", headers={"host": CANONICAL})
    assert res.status_code == 200


def test_もう一方のURLで開くと301で移る(monkeypatch, client):
    monkeypatch.setenv("CANONICAL_HOST", CANONICAL)
    get_settings.cache_clear()
    res = client.get("/api/health?x=1", headers={"host": OTHER})
    assert res.status_code == 301
    assert res.headers["location"] == f"https://{CANONICAL}/api/health?x=1"


def test_ログインの中継は移さない(monkeypatch, client):
    monkeypatch.setenv("CANONICAL_HOST", CANONICAL)
    get_settings.cache_clear()
    res = client.get("/__/auth/handler?x=1", headers={"host": OTHER})
    assert res.status_code != 301
