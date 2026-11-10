"""Gemini の共通処理のテスト（要件定義書10-2の4・10-6、開発手順書 Step 1・Step 6）。

Gemini API には接続しない。クライアントを差し替えて、次のことを確かめる。

- クライアントを AI Studio の API キーで作ること。キーが空なら断ること

- 「考える時間」を 0 にして渡せること（F2 の整える処理。速さが大きく変わる）
- **「考える時間」を指定できないモデルだったとき、付けずにやり直すこと**
  （`.env` の `GEMINI_MODEL` を変えても落ちないようにするため）
- やり直す状態（429・5xx）と、やり直さない状態の区別
"""

from __future__ import annotations

import pytest
from google.genai import errors as genai_errors
from pydantic import BaseModel

from backend.config import get_settings
from backend.services import gemini


class Answer(BaseModel):
    text: str


ANSWER = Answer(text="はい")


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gdgoc-test-project")
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeResponse:
    def __init__(self, parsed):
        self.parsed = parsed


class FakeModels:
    """渡された設定を覚えて、決めた順に答える（例外も返せる）。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.configs = []

    def generate_content(self, *, model, contents, config):
        self.configs.append(config)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer)


class FakeClient:
    def __init__(self, answers):
        self.models = FakeModels(answers)


def use_client(monkeypatch, answers) -> FakeClient:
    client = FakeClient(answers)
    monkeypatch.setattr(gemini, "get_client", lambda: client)
    monkeypatch.setattr(gemini.time, "sleep", lambda seconds: None)
    return client


def api_error(status: int) -> genai_errors.APIError:
    return genai_errors.APIError(status, {"error": {"message": "テスト"}})


def ask(**kwargs):
    return gemini.generate_structured(schema=Answer, contents="テスト", **kwargs)


def thinking_budget_of(config):
    return getattr(getattr(config, "thinking_config", None), "thinking_budget", None)


# --- クライアントの作り方（無料枠に収める修正の手順4-1） ---


def test_クライアントはAPIキーで作る(monkeypatch):
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(gemini.genai, "Client", fake_client)

    gemini.build_client()

    assert captured["api_key"] == "test-api-key"
    assert "vertexai" not in captured


def test_APIキーが空ならクライアントを作らない(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(gemini.GeminiUnavailable):
        gemini.build_client()


# --- 考える時間 ---


def test_考える時間を渡さなければ指定しない(monkeypatch):
    client = use_client(monkeypatch, [ANSWER])

    ask()

    assert thinking_budget_of(client.models.configs[0]) is None


def test_考える時間を0で渡せる(monkeypatch):
    client = use_client(monkeypatch, [ANSWER])

    ask(thinking_budget=0)

    assert thinking_budget_of(client.models.configs[0]) == 0


def test_考える時間を指定できないモデルなら付けずにやり直す(monkeypatch):
    # 400（指定できない）で止めてしまうと、モデルを変えた瞬間に F2 が使えなくなる
    client = use_client(monkeypatch, [api_error(400), ANSWER])

    result = ask(thinking_budget=0)

    assert result == ANSWER
    assert thinking_budget_of(client.models.configs[0]) == 0
    assert thinking_budget_of(client.models.configs[1]) is None


def test_考える時間を外しても400なら断る(monkeypatch):
    use_client(monkeypatch, [api_error(400), api_error(400)])

    with pytest.raises(gemini.GeminiUnavailable):
        ask(thinking_budget=0)


def test_考える時間を渡していない400はやり直さない(monkeypatch):
    client = use_client(monkeypatch, [api_error(400), ANSWER])

    with pytest.raises(gemini.GeminiUnavailable):
        ask()

    assert len(client.models.configs) == 1


# --- やり直し（要件定義書10-2の4） ---


@pytest.mark.parametrize("status", sorted(gemini.RETRYABLE_STATUS))
def test_混み合いや一時的な失敗はやり直す(monkeypatch, status):
    client = use_client(monkeypatch, [api_error(status), ANSWER])

    assert ask() == ANSWER
    assert len(client.models.configs) == 2


def test_やり直しは上限で打ち切る(monkeypatch):
    settings = get_settings()
    client = use_client(monkeypatch, [api_error(503)] * (settings.gemini_max_retries + 1))

    with pytest.raises(gemini.GeminiUnavailable):
        ask()

    assert len(client.models.configs) == settings.gemini_max_retries + 1


def test_スキーマどおりでない返事は断る(monkeypatch):
    use_client(monkeypatch, [None])

    with pytest.raises(gemini.GeminiUnavailable):
        ask()


def test_モデルが設定されていなければ断る(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "")
    get_settings.cache_clear()

    with pytest.raises(gemini.GeminiUnavailable):
        ask()


# --- 指示文（要件定義書10-6「処理ごとにファイルに分ける」） ---


def test_F2の指示文が読める():
    text = gemini.load_prompt("visit_structure")

    assert "記載なし" in text


def test_無い指示文はすぐ分かる形で落ちる():
    with pytest.raises(FileNotFoundError):
        gemini.load_prompt("does_not_exist")
