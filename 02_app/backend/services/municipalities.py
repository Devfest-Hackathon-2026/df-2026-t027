"""全国の都道府県・市区町村の一覧（要望8）。

正本は画面と同じ `02_app/frontend/data/municipalities.json`
（総務省「全国地方公共団体コード」から 04_scripts/build_municipalities.py で作る）。
BigQuery には持たない。サーバーでは、団体コードから名前を引くのに使う。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.config import FRONTEND_DIR

DATA_PATH: Path = FRONTEND_DIR / "data" / "municipalities.json"


@lru_cache(maxsize=1)
def _index() -> dict[str, dict[str, str]]:
    data: dict[str, Any] = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    prefectures = {p["code"]: p["name"] for p in data["prefectures"]}
    return {
        m["code"]: {
            "code": m["code"],
            "name": m["name"],
            "prefecture": prefectures.get(m["pref_code"], ""),
        }
        for m in data["municipalities"]
    }


def lookup(code: str | None) -> dict[str, str] | None:
    """団体コードから {code, name, prefecture} を返す。一覧に無ければ None。"""
    if not code:
        return None
    return _index().get(code.strip())
