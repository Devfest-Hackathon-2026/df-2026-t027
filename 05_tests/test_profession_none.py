"""職種の「該当なし」（追加修正の計画.md 3章）。

専門職でない支援者は、職種の欄で「該当なし」を選ぶ。保存するときは職種なし（None）にする
（今までの「空のまま送る」と同じデータの形）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.routers import account
from backend.services import users

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"


def test_一般の支援者が該当なしを選ぶと職種なしで保存する():
    assert users.validate_profession(users.ROLE_VOLUNTEER, users.NO_PROFESSION) is None


def test_空のまま送られても職種なしにする():
    assert users.validate_profession(users.ROLE_VOLUNTEER, None) is None
    assert users.validate_profession(users.ROLE_VOLUNTEER, "") is None


def test_専門職用のコードで該当なしを選ぶと断る():
    with pytest.raises(users.UserValidationError):
        users.validate_profession(users.ROLE_PROFESSIONAL, users.NO_PROFESSION)


def test_専門職は職種をそのまま保存する():
    assert users.validate_profession(users.ROLE_PROFESSIONAL, "保健師") == "保健師"


def test_画面の選択肢に該当なしがある():
    choices = account._choices()
    assert choices["no_profession"] == {"value": "該当なし", "label": "該当なし"}
    # 職種の一覧そのものには入れない（組織を作る画面・専門職のプロフィールでは選ばせないため）
    assert all(item["value"] != "該当なし" for item in choices["professions"])


def test_画面に専門職以外の方は空のままが残っていない():
    for path in FRONTEND.glob("*.js"):
        assert "専門職以外の方は空のまま" not in path.read_text(encoding="utf-8"), path.name
