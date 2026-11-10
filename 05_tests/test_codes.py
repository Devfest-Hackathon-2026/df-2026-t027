"""services/codes.py のテスト（要件定義書10-3、開発手順書 Step 3-1）。

Google Cloud には接続しない。コードの正規化・ハッシュ・照合だけを確かめる。
"""

from __future__ import annotations

import pytest

from backend.services import codes

SALT = "org_wajima"


# --- 正規化（スマートフォンで全角や区切りが入ることがある） ---


@pytest.mark.parametrize(
    "given",
    [
        "ABCD2345",
        "abcd2345",
        "ABCD-2345",
        "abcd 2345",
        "ABCD_2345",
        "  ABCD2345  ",
        "ＡＢＣＤ２３４５",  # 全角
        "ＡＢＣＤ－２３４５",  # 全角のハイフン
    ],
)
def test_normalize_code_makes_the_same_value(given: str) -> None:
    assert codes.normalize_code(given) == "ABCD2345"


@pytest.mark.parametrize("given", ["", "   ", "-", "ABC12", "あいうえおか", "ABCD234!", "ABCD 234@"])
def test_normalize_code_rejects_bad_shapes(given: str) -> None:
    with pytest.raises(codes.InvalidCode):
        codes.normalize_code(given)


@pytest.mark.parametrize("given", [None, 12345678, b"ABCD2345", ["ABCD2345"]])
def test_normalize_code_rejects_non_string(given: object) -> None:
    with pytest.raises(codes.InvalidCode):
        codes.normalize_code(given)  # type: ignore[arg-type]


# --- ハッシュ ---


def test_hash_code_is_the_same_every_time() -> None:
    assert codes.hash_code("ABCD2345", salt=SALT) == codes.hash_code("ABCD2345", salt=SALT)


def test_hash_code_starts_with_the_scheme() -> None:
    # 方式を後から見分けられるようにしておく
    value = codes.hash_code("ABCD2345", salt=SALT)
    assert value.startswith("sha256$")
    assert len(value) == len("sha256$") + 64


def test_hash_code_differs_by_salt() -> None:
    # 同じコードでも組織ごとに別のハッシュになる（1つ漏れても他に使い回せない）
    assert codes.hash_code("ABCD2345", salt="org_wajima") != codes.hash_code(
        "ABCD2345", salt="org_suzu"
    )


def test_hash_code_normalizes_first() -> None:
    assert codes.hash_code("abcd-2345", salt=SALT) == codes.hash_code("ＡＢＣＤ２３４５", salt=SALT)


def test_hash_code_does_not_contain_the_code() -> None:
    # 元の値を復元できる形で残さない
    assert "ABCD2345" not in codes.hash_code("ABCD2345", salt=SALT)


def test_hash_code_differs_by_code() -> None:
    assert codes.hash_code("ABCD2345", salt=SALT) != codes.hash_code("ABCD2346", salt=SALT)


@pytest.mark.parametrize("salt", ["", "   ", None])
def test_hash_code_rejects_empty_salt(salt: object) -> None:
    with pytest.raises(codes.InvalidCode):
        codes.hash_code("ABCD2345", salt=salt)  # type: ignore[arg-type]


def test_hash_code_rejects_bad_code() -> None:
    with pytest.raises(codes.InvalidCode):
        codes.hash_code("ABC", salt=SALT)


# --- 照合 ---


def test_verify_code_accepts_the_right_code() -> None:
    stored = codes.hash_code("ABCD2345", salt=SALT)
    assert codes.verify_code("ABCD2345", salt=SALT, code_hash=stored) is True
    assert codes.verify_code("abcd-2345", salt=SALT, code_hash=stored) is True


def test_verify_code_rejects_the_wrong_code() -> None:
    stored = codes.hash_code("ABCD2345", salt=SALT)
    assert codes.verify_code("ABCD2346", salt=SALT, code_hash=stored) is False


def test_verify_code_rejects_another_organization() -> None:
    # 別の組織の参加コードを入れても通らない（要件定義書10-3）
    stored = codes.hash_code("ABCD2345", salt="org_wajima")
    assert codes.verify_code("ABCD2345", salt="org_suzu", code_hash=stored) is False


@pytest.mark.parametrize("stored", [None, "", "sha256$", "でたらめ"])
def test_verify_code_rejects_missing_or_broken_hash(stored: object) -> None:
    assert codes.verify_code("ABCD2345", salt=SALT, code_hash=stored) is False  # type: ignore[arg-type]


@pytest.mark.parametrize("given", ["", "ABC", "あいうえおか", None, 12345678])
def test_verify_code_returns_false_instead_of_raising(given: object) -> None:
    # 画面から来た値をそのまま渡せるようにする（例外で500にしない）
    stored = codes.hash_code("ABCD2345", salt=SALT)
    assert codes.verify_code(given, salt=SALT, code_hash=stored) is False  # type: ignore[arg-type]


# --- 発行 ---


def test_generate_code_has_the_expected_shape() -> None:
    code = codes.generate_code()
    assert len(code) == codes.CODE_LENGTH
    assert set(code) <= set(codes.CODE_ALPHABET)


def test_generate_code_avoids_confusable_characters() -> None:
    # 紙に書いて口頭で伝えるため、0・O・1・I は使わない
    for character in "01OI":
        assert character not in codes.CODE_ALPHABET


def test_generate_code_is_not_predictable() -> None:
    assert len({codes.generate_code() for _ in range(200)}) > 190


def test_generate_code_result_passes_normalize() -> None:
    # 発行した値がそのまま照合に使える（正規化で形が変わらない）
    code = codes.generate_code()
    assert codes.normalize_code(code) == code


def test_generate_code_rejects_too_short_length() -> None:
    with pytest.raises(codes.InvalidCode):
        codes.generate_code(codes.MIN_CODE_LENGTH - 1)
