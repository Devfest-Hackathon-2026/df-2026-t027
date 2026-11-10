"""都道府県も文字で絞り込む（追加修正の計画.md 5章）。

- 一覧のデータの47都道府県すべてに読み（ひらがな）がある
- 都道府県を選ぶ3つの画面に、絞り込みの欄があり、部品に渡している
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import build_municipalities

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"
DATA = json.loads((FRONTEND / "data" / "municipalities.json").read_text(encoding="utf-8"))
INDEX_HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")

# 都道府県の欄の id → 部品（bindMunicipalityPicker）を呼んでいるファイル
PREFECTURE_FIELDS = {
    "survivor-prefecture": "app.js",
    "new-prefecture": "f1.js",
    "create-org-prefecture": "organizations.js",
    # 支援者の活動する都道府県（追加修正④）
    "supporter-prefecture": "app.js",
    "profile-supporter-prefecture": "profile.js",
    # 周りの地域の状況を入れる画面（使いやすさの改修の計画.md 8章）
    "disaster-prefecture": "disaster.js",
}


def test_47都道府県すべてに読みがある():
    prefectures = DATA["prefectures"]
    assert len(prefectures) == 47
    for pref in prefectures:
        assert re.fullmatch(r"[ぁ-ん]+", pref["kana"]), pref["name"]


def test_読みで東京都と石川県が見つかる():
    by_kana = {pref["kana"]: pref["name"] for pref in DATA["prefectures"]}
    assert [name for kana, name in by_kana.items() if "とうきょう" in kana] == ["東京都"]
    assert [name for kana, name in by_kana.items() if "いしかわ" in kana] == ["石川県"]


def test_作り直すスクリプトにも47都道府県の読みがある():
    names = {pref["name"] for pref in DATA["prefectures"]}
    assert set(build_municipalities.PREFECTURE_KANA) == names


def test_都道府県を選ぶ画面に絞り込みの欄がある():
    for field_id, script in PREFECTURE_FIELDS.items():
        assert f'id="{field_id}-filter"' in INDEX_HTML, field_id
        code = (FRONTEND / script).read_text(encoding="utf-8")
        assert f'prefectureFilterInput: el("{field_id}-filter")' in code, script


def test_部品は名前と読みの両方で都道府県を絞り込む():
    code = (FRONTEND / "municipalities.js").read_text(encoding="utf-8")
    assert "prefectureFilterInput" in code
    assert 'item.name.includes(text) || (item.kana || "").includes(text)' in code
