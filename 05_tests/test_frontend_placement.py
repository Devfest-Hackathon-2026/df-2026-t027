"""施設を決めたあと、居場所の初期値が施設の種類から入ること（Step 9・要確認43）。

2026-11-09 の実機確認で、**画面に初期値が入らない**ことが分かった。
バックエンドは `POST /placements` の応答に `next_steps.location_type` を返していたが、
**画面がそれを捨てていた**（`f5.js` の `showDecided` が `handover_destination` だけを拾い、
「居場所を変える（記録に戻る）」は被災者のIDだけで記録を開き直していた）。

JavaScript を走らせる仕組みを持たないので、**受け渡しの経路が繋がっていること**を
文字列で確かめる。値が実際に入るかはブラウザで見る（Step 9 の確認3）。
"""

from __future__ import annotations

from pathlib import Path

from backend.routers.facilities import _LOCATION_TYPE_BY_FACILITY_TYPE
from backend.services.facilities import FACILITY_TYPES
from backend.services.survivors import LOCATION_TYPES

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"


def source(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


# --- 対応づけそのもの（ここがずれると、画面は黙って空のままになる） ---


def test_対応づけの元はすべて実在する施設の種類():
    unknown = sorted(set(_LOCATION_TYPE_BY_FACILITY_TYPE) - set(FACILITY_TYPES))
    assert not unknown, f"施設の種類に無いものが対応づけの元になっています: {unknown}"


def test_対応づけの先はすべて実在する居場所の種類():
    """`<select>` に無い値を入れても**何も起きない**（選択は空のまま）ので、ここで止める。"""
    unknown = sorted(set(_LOCATION_TYPE_BY_FACILITY_TYPE.values()) - set(LOCATION_TYPES))
    assert not unknown, f"居場所の種類に無いものが初期値になります: {unknown}"


def test_候補に出る施設の種類はすべて対応づけを持つ():
    """対応づけが無い種類の施設を決めると、初期値が入らないまま画面に進む。"""
    missing = sorted(set(FACILITY_TYPES) - set(_LOCATION_TYPE_BY_FACILITY_TYPE))
    # 「その他」は対応づけを決められない（専門職が選ぶ）
    assert missing == ["other"], f"対応づけの無い施設の種類があります: {missing}"


# --- 画面への受け渡しの経路 ---


def test_f5は応答のlocation_typeを受け取って持っておく():
    text = source("f5.js")
    assert "next_steps?.location_type" in text, "応答の location_type を拾っていません"
    assert "next_steps?.location_detail" in text, "応答の location_detail を拾っていません"
    assert "export function suggestedLocation" in text, "居場所の初期値を渡す関数がありません"


def test_appはf5の初期値をf1の記録の画面へ渡す():
    text = source("app.js")
    assert "f5.suggestedLocation()" in text, "f5 の初期値を記録の画面へ渡していません"


def test_f1は渡された初期値を居場所のフォームに入れる():
    text = source("f1.js")
    assert "export async function openDetail(survivorId, { location }" in text, (
        "openDetail が居場所の初期値を受け取っていません"
    )
    # 現在の値を入れたあとに、渡された初期値で上書きする
    assert "pendingLocation" in text, "渡された初期値を使っていません"
    current = text.index('el("location-type").value = survivor.location_type')
    override = text.index("pendingLocation.location_type")
    assert current < override, "現在の値が、渡された初期値を上書きしてしまいます"


def test_初期値は1回だけ使う():
    """記録を開き直すたびに上書きすると、専門職が直した値が戻ってしまう。"""
    assert "pendingLocation = null" in source("f1.js"), "使ったあとに捨てていません"
    assert "suggestedLocationValue = null" in source("f5.js"), "渡したあとに捨てていません"
