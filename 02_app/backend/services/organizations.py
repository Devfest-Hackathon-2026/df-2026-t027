"""組織と参加コード（F0。要件定義書7-3-2の stg_organizations、10-3）。

- 画面に返す一覧には、**参加コードのハッシュを含めない**。`SELECT *` にせず列を並べる
  （一覧は初回のログインだけ済んだ利用者にも返るため、照合の材料を外に出さない）。
- 参加コードは、どの組織のものか分からない状態で入力される。組織を順に、`pro`・`vol` の順で
  照合し、合った組織と立場を採る（ナレッジベース06「範囲を順に試す」）。
- 1つのコードが2か所に当たったときは**どちらも採らない**。立場や組織が決まらないまま採ると、
  別の組織の記録を読める経路ができる（要件定義書10-3）。発行は組織ごとに別の値にしてある。
- 組織のオーナーがいつでも見られるように、元のコードも保存する（`pro_join_code`・
  `vol_join_code`。追加修正⑥）。返すのはオーナーの API（`fetch_join_codes`）だけ。照合は
  今までどおりハッシュで行う。発行し直すと新しい版の行を足し、前のコードは照合に使われなくなる。
- 参加コードの値はログに出さない。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from typing import Any

from backend.config import get_settings
from backend.services import bq, codes, municipalities
from backend.services.users import ROLE_PROFESSIONAL, ROLE_VOLUNTEER

# 組織の名前の長さの上限（要望2）。
ORG_NAME_MAX_LENGTH = 60

logger = logging.getLogger(__name__)


class OrganizationValidationError(ValueError):
    """組織・市区町村の入力が正しくない。"""


# 参加コードの列と、それが表す立場。照合はこの順に試す（結果が毎回同じになるようにする）。
JOIN_CODE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("pro_join_code_hash", ROLE_PROFESSIONAL),
    ("vol_join_code_hash", ROLE_VOLUNTEER),
)

# 画面に返す列（参加コードのハッシュも元のコードも入れない）。
_PUBLIC_COLUMNS = ("org_id", "org_name", "prefecture", "municipality_code")

# 市区町村の団体コード（要望8）。総務省の「全国地方公共団体コード」は6桁の数字
# （`02_app/frontend/data/municipalities.json`。04_scripts/build_municipalities.py で作る）。
# この一覧そのものはBigQueryに持たないため、ここでは形（6桁の数字）だけを確かめる。
MUNICIPALITY_CODE_PATTERN = re.compile(r"^\d{6}$")


def validate_municipality_code(value: str | None) -> str:
    """市区町村の団体コードの形を確かめる（一覧にあるかはフロントの一覧が正本）。"""
    code = (value or "").strip()
    if not MUNICIPALITY_CODE_PATTERN.match(code):
        raise OrganizationValidationError("お住まいの市区町村を一覧から選んでください")
    return code


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


def list_organizations() -> list[dict[str, Any]]:
    """本人が選ぶ市町（組織）の一覧。並び順は org_id で固定する。"""
    sql = (
        f"SELECT {', '.join(_PUBLIC_COLUMNS)}\n"
        f"FROM {_staging_table('stg_organizations')}\n"
        "ORDER BY org_id"
    )
    return bq.query(sql)


def fetch_organization(org_id: str) -> dict[str, Any] | None:
    """組織を1件読む（本人が選んだ組織が実在するかの確認に使う）。"""
    if not org_id:
        return None
    sql = (
        f"SELECT {', '.join(_PUBLIC_COLUMNS)}\n"
        f"FROM {_staging_table('stg_organizations')}\n"
        "WHERE org_id = @org_id"
    )
    return bq.query_one(sql, {"org_id": org_id})


def fetch_by_municipality(municipality_code: str) -> list[dict[str, Any]]:
    """その市区町村を受け持つ組織を探す（要望8。本人の登録で組織を決めるのに使う）。

    0件・1件・複数件のどれもありうる：
    - 0件：その市区町村には、まだ支援の組織が登録されていない
    - 1件：そのまま使う
    - 複数件：本人に選ばせる（呼ぶ側の `routers/account.py` が行う）

    市区町村がまだ入っていない組織は、**都道府県と組織の名前**で探す
    （例：石川県の「輪島市」は 172049 を受け持つ）。市区町村の一覧
    （`services/municipalities.py`）から名前を引くので、組織ごとに市区町村を
    入れ直さなくても、市区町村の名前どおりの組織は見つかる。
    """
    if not municipality_code:
        return []
    params: dict[str, Any] = {"municipality_code": municipality_code}
    where = "municipality_code = @municipality_code"
    place = municipalities.lookup(municipality_code)
    if place is not None:
        where += (
            "\n   OR (IFNULL(municipality_code, '') = ''"
            " AND TRIM(org_name) = @municipality_name"
            " AND TRIM(IFNULL(prefecture, '')) = @prefecture_name)"
        )
        params["municipality_name"] = place["name"]
        params["prefecture_name"] = place["prefecture"]
    sql = (
        f"SELECT {', '.join(_PUBLIC_COLUMNS)}\n"
        f"FROM {_staging_table('stg_organizations')}\n"
        f"WHERE {where}\n"
        "ORDER BY org_id"
    )
    return bq.query(sql, params)


def _fetch_join_code_hashes() -> list[dict[str, Any]]:
    columns = ("org_id",) + tuple(column for column, _ in JOIN_CODE_COLUMNS)
    sql = (
        f"SELECT {', '.join(columns)}\n"
        f"FROM {_staging_table('stg_organizations')}\n"
        "ORDER BY org_id"
    )
    return bq.query(sql)


def resolve_join_code(code: str) -> tuple[str, str] | None:
    """参加コードから（組織のID, 立場）を決める。合わなければ None。

    コードの値はログに出さない。合ったかどうかと、当たった件数だけを出す。
    """
    matches: list[tuple[str, str]] = []
    for row in _fetch_join_code_hashes():
        org_id = row.get("org_id")
        if not org_id:
            continue
        for column, role in JOIN_CODE_COLUMNS:
            if codes.verify_code(code, salt=org_id, code_hash=row.get(column)):
                matches.append((org_id, role))

    if not matches:
        logger.info("join_code_rejected: matched=0")
        return None
    if len(matches) > 1:
        # 同じコードが2か所に発行されている。立場と組織が決まらないため通さない。
        logger.warning("join_code_ambiguous: matched=%d", len(matches))
        return None
    logger.info("join_code_accepted: role=%s", matches[0][1])
    return matches[0]


# --- 組織を作る（要望2。要件定義書には無い追加の機能） ---


def new_org_id() -> str:
    """アプリで作る組織のID（初期データの `org_wajima` などとぶつからない）。"""
    return f"org_{uuid.uuid4().hex[:12]}"


def validate_org_name(value: str | None) -> str:
    """組織の名前をそろえる（空では作れない）。"""
    name = (value or "").strip()
    if not name:
        raise OrganizationValidationError("組織の名前を入れてください")
    if len(name) > ORG_NAME_MAX_LENGTH:
        raise OrganizationValidationError(f"組織の名前は{ORG_NAME_MAX_LENGTH}文字以内にしてください")
    return name


def find_duplicate(*, municipality_code: str, org_name: str) -> dict[str, Any] | None:
    """同じ市区町村に、同じ名前の組織が既にないか確かめる（要望2・11章の要確認B）。"""
    name = org_name.strip()
    for row in fetch_by_municipality(municipality_code):
        if str(row.get("org_name") or "").strip() == name:
            return row
    return None


def create_organization(
    *,
    org_name: str,
    prefecture: str,
    municipality_code: str,
    created_by: str,
    created_role: str,
) -> dict[str, Any]:
    """組織を作り、専門職用・一般の支援者用の参加コードを発行する（要望2）。

    照合に使うのはハッシュ（`04_scripts/seed.py` の初期データと同じ方式。`services/codes.py`）。
    元のコードも保存し、組織のオーナーがあとから見られるようにする（追加修正⑥）。
    """
    name = validate_org_name(org_name)
    code = validate_municipality_code(municipality_code)
    duplicate = find_duplicate(municipality_code=code, org_name=name)
    if duplicate is not None:
        raise OrganizationValidationError(
            "同じ市区町村に、同じ名前の組織が既にあります。別の名前にするか、"
            "その組織の参加コードをお持ちの方にもらってください"
        )

    org_id = new_org_id()
    pro_code = codes.generate_code()
    vol_code = codes.generate_code()
    row = {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "org_name": name,
        "prefecture": (prefecture or "").strip() or None,
        "municipality_code": code,
        "pro_join_code_hash": codes.hash_code(pro_code, salt=org_id),
        "vol_join_code_hash": codes.hash_code(vol_code, salt=org_id),
        # 元のコード（追加修正⑥）。オーナーの API だけが返す
        "pro_join_code": pro_code,
        "vol_join_code": vol_code,
    }
    bq.insert("raw_organizations", row)
    return {
        "org_id": org_id,
        "org_name": name,
        "prefecture": row["prefecture"],
        "municipality_code": code,
        # 作った人（オーナー）に返す。あとからはプロフィールの画面で見られる（追加修正⑥）
        "pro_join_code": pro_code,
        "vol_join_code": vol_code,
    }


# --- 参加コードを見る・発行し直す（追加修正⑥。組織のオーナーだけ） ---

# オーナーに返す列。照合に使うハッシュは返さない。
_JOIN_CODE_VIEW_COLUMNS = ("org_id", "org_name", "pro_join_code", "vol_join_code")


def fetch_join_codes(org_id: str) -> dict[str, Any] | None:
    """組織の参加コード（元のコード）を読む。呼ぶのはオーナーの API だけ。

    元のコードを保存する前に作った組織（初期データの輪島市・珠洲市など）では空になる。
    そのときは発行し直してもらう。
    """
    if not org_id:
        return None
    sql = (
        f"SELECT {', '.join(_JOIN_CODE_VIEW_COLUMNS)}\n"
        f"FROM {_staging_table('stg_organizations')}\n"
        "WHERE org_id = @org_id"
    )
    return bq.query_one(sql, {"org_id": org_id})


def reissue_join_codes(*, org_id: str, created_by: str, created_role: str) -> dict[str, Any]:
    """参加コードを発行し直す（追加修正⑥）。前のコードは使えなくなる。

    `raw_organizations` に新しい版の行を足す（上書きしない）。名前・都道府県・市区町村は
    今の版から引き継ぎ、コード（ハッシュと元のコード）だけを新しくする。`stg_organizations` は
    組織ごとに最新の1行を返すので、照合（`resolve_join_code`）には新しいコードだけが使われる。
    """
    current = fetch_organization(org_id)
    if current is None:
        raise OrganizationValidationError("組織が見つかりません")
    pro_code = codes.generate_code()
    vol_code = codes.generate_code()
    row = {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "org_name": current.get("org_name"),
        "prefecture": current.get("prefecture"),
        "municipality_code": current.get("municipality_code"),
        "pro_join_code_hash": codes.hash_code(pro_code, salt=org_id),
        "vol_join_code_hash": codes.hash_code(vol_code, salt=org_id),
        "pro_join_code": pro_code,
        "vol_join_code": vol_code,
    }
    bq.insert("raw_organizations", row)
    logger.info("join_codes_reissued")
    return {
        "org_id": org_id,
        "org_name": current.get("org_name"),
        "pro_join_code": pro_code,
        "vol_join_code": vol_code,
    }
