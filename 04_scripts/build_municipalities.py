"""全国の都道府県・市区町村の一覧を作り直す（要望8。要件定義書には無い追加のスクリプト）。

使い方（リポジトリ直下で、.venv を有効にしてから）：

    python 04_scripts/build_municipalities.py

    # ネットワークにつながらないときは、先にファイルを落としておいて渡す
    python 04_scripts/build_municipalities.py --prefectures prefectures.json --cities cities.json

元データ：総務省の「全国地方公共団体コード」をJSON化した nojimage/local-gov-code-jp
（MITライセンス）。https://github.com/nojimage/local-gov-code-jp
基準日は元データのREADMEに書かれている日付をそのまま使う（2026-11-09時点：2024-01-01）。

出力：02_app/frontend/data/municipalities.json
画面はこのファイルを読む。BigQueryもAPIも使わない（要件定義書に無い追加の一覧のため、
読みを増やさない。追加改修の計画.md 9-3）。

政令指定都市の区（札幌市中央区など）は分けない。市の単位までにする
（追加改修の計画.md 11章の要確認F）。元データの wards.json は使わない。

市町村の合併があったら、元データが更新されたあとにこのスクリプトを流し直す。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "02_app" / "frontend" / "data" / "municipalities.json"

SOURCE_BASE = "https://raw.githubusercontent.com/nojimage/local-gov-code-jp/master/"
# 元データ（nojimage/local-gov-code-jp）のREADMEに書かれている基準日（令和6年1月1日）。
# 元データを作り直したら、この値も合わせて直す。
BASIS_DATE = "2024-01-01"
SOURCE_NOTE = (
    "総務省「全国地方公共団体コード」"
    "（nojimage/local-gov-code-jp のJSONを元にした。MITライセンス）"
)


# 都道府県の読み（ひらがな）。元データの prefectures.json には読みが無いため、ここに持つ
# （追加修正⑤。都道府県も「いしかわ」など読みで絞り込めるようにする）。
PREFECTURE_KANA = {
    "北海道": "ほっかいどう",
    "青森県": "あおもりけん",
    "岩手県": "いわてけん",
    "宮城県": "みやぎけん",
    "秋田県": "あきたけん",
    "山形県": "やまがたけん",
    "福島県": "ふくしまけん",
    "茨城県": "いばらきけん",
    "栃木県": "とちぎけん",
    "群馬県": "ぐんまけん",
    "埼玉県": "さいたまけん",
    "千葉県": "ちばけん",
    "東京都": "とうきょうと",
    "神奈川県": "かながわけん",
    "新潟県": "にいがたけん",
    "富山県": "とやまけん",
    "石川県": "いしかわけん",
    "福井県": "ふくいけん",
    "山梨県": "やまなしけん",
    "長野県": "ながのけん",
    "岐阜県": "ぎふけん",
    "静岡県": "しずおかけん",
    "愛知県": "あいちけん",
    "三重県": "みえけん",
    "滋賀県": "しがけん",
    "京都府": "きょうとふ",
    "大阪府": "おおさかふ",
    "兵庫県": "ひょうごけん",
    "奈良県": "ならけん",
    "和歌山県": "わかやまけん",
    "鳥取県": "とっとりけん",
    "島根県": "しまねけん",
    "岡山県": "おかやまけん",
    "広島県": "ひろしまけん",
    "山口県": "やまぐちけん",
    "徳島県": "とくしまけん",
    "香川県": "かがわけん",
    "愛媛県": "えひめけん",
    "高知県": "こうちけん",
    "福岡県": "ふくおかけん",
    "佐賀県": "さがけん",
    "長崎県": "ながさきけん",
    "熊本県": "くまもとけん",
    "大分県": "おおいたけん",
    "宮崎県": "みやざきけん",
    "鹿児島県": "かごしまけん",
    "沖縄県": "おきなわけん",
}


def fetch(name: str) -> list[dict[str, Any]]:
    """元データをダウンロードする。"""
    with urllib.request.urlopen(SOURCE_BASE + name, timeout=30) as res:  # noqa: S310
        return json.loads(res.read().decode("utf-8"))


def build(
    prefectures_raw: list[dict[str, Any]], cities_raw: list[dict[str, Any]]
) -> dict[str, Any]:
    """画面が読む形に組み立てる（団体コード・名前・読みだけを残す。都道府県の読みは PREFECTURE_KANA）。"""
    prefectures = [
        {
            "code": p["pref_code"],
            "name": p["pref_name"],
            # 読みが分からない都道府県があれば止める（47件そろっていないと絞り込めないため）
            "kana": PREFECTURE_KANA[p["pref_name"]],
        }
        for p in prefectures_raw
    ]
    municipalities = [
        {
            "code": c["city_code"],
            "name": c["city_name"],
            "kana": c["city_kana"],
            "pref_code": c["pref_code"],
        }
        for c in cities_raw
    ]
    return {
        "basis_date": BASIS_DATE,
        "source": SOURCE_NOTE,
        "prefectures": prefectures,
        "municipalities": municipalities,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefectures", type=Path, help="prefectures.json のファイル（省略時はダウンロードする）"
    )
    parser.add_argument(
        "--cities", type=Path, help="cities.json のファイル（省略時はダウンロードする）"
    )
    args = parser.parse_args(argv)

    prefectures_raw = (
        json.loads(args.prefectures.read_text(encoding="utf-8"))
        if args.prefectures
        else fetch("prefectures.json")
    )
    cities_raw = (
        json.loads(args.cities.read_text(encoding="utf-8"))
        if args.cities
        else fetch("cities.json")
    )

    data = build(prefectures_raw, cities_raw)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"{len(data['prefectures'])}都道府県・{len(data['municipalities'])}市区町村を書きました: "
        f"{OUTPUT_PATH.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
