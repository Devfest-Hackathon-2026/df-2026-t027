"""受け入れ先の施設と移る先の決定（F5。要件定義書F5・7-3-1の raw_facilities・raw_placements、
開発手順書 Step 9-1・9-2）。

- **上書きはしない。** 施設は更新のたびに版を1つ足す（`stg_facilities_latest` が最新を決める）。
  空き・断水・停電だけを変えるときも、名前・種類・場所・対応できる医療は前の版から引き継ぐ。
- **空きがない施設と、必要な医療に対応できない施設は、候補を並べる前に外す**（要件定義書F5）。
- **候補はルールで並べる。AI は使わない**（画面の改修の計画.md 7章。使いやすさの改修の計画 12-2 の #5）。
  空き・医療はもうルールで絞ってあり、AI が選ぶ余地が少なかったため。並べ方は `rank_candidates`。
- **対応できるか判断できない必要な医療では外さない。** F3 の「必要な医療」は文章で入るため、
  選択肢のコードに読み替えられないことがある。分からないものを「対応できない」と決めると
  候補が理由なく消えるので、**外さずに候補の理由と画面で伝える**。
- 最終更新から24時間以上たった施設は、そのことを候補に出す（要件定義書F5）。
  古いかどうかの判断は `stg_facilities_latest.is_stale`（ビュー側）が正本。
- **決めるのは人。** 候補にない施設も選べるようにし、選んだ理由を必ず残す（要件定義書F5）。
- 対応できる医療の値は**コードの固定の集合**にする。文章で受け取ると、候補を外す判断が狂う。
- ログに施設の名前・被災者の氏名・健康の情報を出さない（要件定義書10-2の6・11-3）。
- **施設の参加コード**（使いやすさの改修の計画.md 9章）。施設の職員が入るためのコードで、
  組織の参加コードと同じ作り（`services/organizations.py`）にする。照合はハッシュで行い、
  元のコードも持つ（その組織の専門職がいつでも見られるように）。どちらも施設の版の列に入れ、
  版を足すたびに前の版から引き継ぐ。画面に返す一覧（`_COLUMNS`・`payload`）には入れない。
- 参加コードを発行しただけの版は、`change_kind` を `join_code` にする。最終更新の時刻
  （24時間の知らせ）は、空きなどを変えた版だけで決める（`stg_facilities_latest`）。
  発行しただけで「新しい情報」に見えると、古い空きの数を信じてしまうため。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from typing import Any

from backend.config import get_settings
from backend.services import bq, codes

logger = logging.getLogger(__name__)

# 施設の種類（要件定義書F5「福祉避難所・介護施設・病院など」）。
# **要件定義書は値を決めていない**ため、ここで決めた（開発ログの要確認に記録する）。
FACILITY_TYPES: dict[str, str] = {
    "welfare_shelter": "福祉避難所",
    "care_facility": "介護施設",
    "hospital": "病院",
    "clinic": "診療所",
    "secondary": "2次避難先（旅館・ホテル）",
    "other": "その他",
}

# 対応できる介護・医療（要件定義書F5「透析・たんの吸引・認知症・車いすなど」）。
# **要件定義書は値を決めていない**ため、F3 の「必要な医療」で書かれやすいものを並べた。
CAPABILITIES: dict[str, str] = {
    "dialysis": "透析",
    "oxygen": "在宅酸素",
    "suction": "たんの吸引",
    "tube_feeding": "経管栄養（胃ろう・鼻からの管）",
    "insulin": "インスリンの注射",
    "catheter": "導尿・カテーテル",
    "stoma": "ストーマ（人工肛門）",
    "wound_care": "傷・褥瘡の処置",
    "nurse": "看護師が常にいる",
    "dementia": "認知症の方の受け入れ",
    "wheelchair": "車いすで生活できる",
    "bedridden": "寝たきりの方の受け入れ",
}

# F3 の「必要な医療」（文章）を、上のコードに読み替えるための言葉。
# **ここに無い言葉は「分からない」として扱い、施設を外す理由にはしない。**
CAPABILITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "dialysis": ("透析", "ダイアライ"),
    "oxygen": ("在宅酸素", "酸素"),
    "suction": ("吸引", "喀痰"),
    "tube_feeding": ("経管栄養", "胃ろう", "胃瘻", "鼻腔栄養", "経鼻"),
    "insulin": ("インスリン",),
    "catheter": ("導尿", "カテーテル", "バルーン"),
    "stoma": ("ストーマ", "人工肛門"),
    "wound_care": ("褥瘡", "じょくそう", "創傷", "傷の処置"),
    "nurse": ("看護師", "医療的ケア", "医療ケア"),
    "dementia": ("認知症",),
    "wheelchair": ("車いす", "車椅子"),
    "bedridden": ("寝たきり", "全介助"),
}

# 最終更新から何時間で「古い」と見なすか（要件定義書F5）。
# `03_sql/03_stg_views.sql` の `stg_facilities_latest.is_stale` と同じ値にする。
STALE_HOURS = 24

# 古い施設に付ける知らせ（文言の正本はここ。画面に書き写さない）。
STALE_NOTICE = f"最終更新から{STALE_HOURS}時間以上たっています。空きの数を電話で確かめてください"

# 候補から外した理由（画面に出す。値の正本はここ）。
EXCLUDED_NO_VACANCY = "空きがない"
EXCLUDED_NO_CAPABILITY = "必要な医療に対応できない"

# 対応できるか判断できない必要な医療があるときに、画面に伝える言い方。
UNMATCHED_NOTICE = "選択肢に無い必要な医療があります。対応できるかは施設に確かめてください"

# 候補の上限（要件定義書F5「最大3件」）。
MAX_CANDIDATES = 3

# 渡せる施設が1件も無いときに画面へ出す案内。
NO_ELIGIBLE_NOTICE = (
    "空きがあり、必要な医療に対応できる施設が見つかりませんでした。"
    "施設の空きの数を更新するか、ほかの組織へ相談してください"
)

# 健康・介護の情報（文章の欄）から読み取る介護の必要（画面の改修の計画.md 7-2）。
# 「必要な医療」と違い、施設を**外す理由にはしない**。合う施設を先に並べるのにだけ使う。
CARE_NEED_FIELDS = (
    "care_level",
    "assistance_mobility",
    "assistance_meal",
    "assistance_toilet",
    "dementia_notes",
)
CARE_NEED_CODES = ("dementia", "wheelchair", "bedridden")

# 「なし」と書いた欄は、言葉が入っていても必要とは読まない（例：「認知症なし」）。
_NO_NEED_WORDS = ("なし", "無し", "ありません", "ない")

# 画面に出す件数（読みを増やさないため上限を置く）。
LIST_LIMIT = 100

# 入力の上限。
NAME_MAX_LENGTH = 120
ADDRESS_MAX_LENGTH = 200
REASON_MAX_LENGTH = 500
CANDIDATE_REASON_MAX_LENGTH = 300
VACANCIES_MAX = 9999


# 版を足した理由（raw_facilities.change_kind。使いやすさの改修の計画.md 9章）。
# 空きなどを変えた版だけが「最終更新」になる。空（前からある行）は CHANGE_INFO と同じ扱い。
CHANGE_INFO = "info"
CHANGE_JOIN_CODE = "join_code"

# 最後に更新した人の立場の名前（施設の一覧に出す。9章）。
UPDATED_ROLE_LABELS: dict[str, str] = {
    "facility_staff": "施設の職員",
    "professional": "専門職",
}


class FacilityValidationError(ValueError):
    """施設・移る先の入力が正しくない。"""


# --- 値をそろえる ---


def _clean_text(value: Any, *, label: str, max_length: int, required: bool) -> str | None:
    if value is None or not isinstance(value, str):
        if required:
            raise FacilityValidationError(f"{label}を入れてください")
        return None
    text = value.strip()
    if not text:
        if required:
            raise FacilityValidationError(f"{label}を入れてください")
        return None
    if len(text) > max_length:
        raise FacilityValidationError(f"{label}は{max_length}文字以内にしてください")
    return text


def validate_facility_type(value: Any) -> str:
    """施設の種類を確かめる（選択肢にない値は断る）。"""
    if not isinstance(value, str) or value not in FACILITY_TYPES:
        raise FacilityValidationError(
            "施設の種類は次のどれかにしてください：" + "・".join(FACILITY_TYPES.values())
        )
    return value


def validate_vacancies(value: Any) -> int:
    """空きの数を確かめる（0以上の整数。未記入は断る）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise FacilityValidationError("空きの数を数字で入れてください")
    if value < 0:
        raise FacilityValidationError("空きの数は0以上にしてください")
    if value > VACANCIES_MAX:
        raise FacilityValidationError(f"空きの数は{VACANCIES_MAX}以下にしてください")
    return value


def clean_capabilities(value: Any) -> list[str]:
    """対応できる介護・医療をそろえる（**選択肢のコードだけ**を通す）。

    文章で受け取ると、候補を外す判断（必要な医療に対応できるか）が狂う。
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise FacilityValidationError("対応できる介護・医療は一覧から選んでください")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or item.strip() not in CAPABILITIES:
            raise FacilityValidationError(
                "対応できる介護・医療に、選択肢にない値が入っています：" + str(item)
            )
        code = item.strip()
        if code not in cleaned:
            cleaned.append(code)
    return cleaned


def _clean_flag(value: Any, *, label: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise FacilityValidationError(f"{label}は「はい」「いいえ」で入れてください")
    return value


def clean_fields(fields: dict[str, Any] | None) -> dict[str, Any]:
    """登録のときに受け取る項目をそろえる。"""
    source = fields or {}
    return {
        "facility_name": _clean_text(
            source.get("facility_name"), label="施設の名前", max_length=NAME_MAX_LENGTH, required=True
        ),
        "facility_type": validate_facility_type(source.get("facility_type")),
        "address": _clean_text(
            source.get("address"), label="場所", max_length=ADDRESS_MAX_LENGTH, required=False
        ),
        "vacancies": validate_vacancies(source.get("vacancies")),
        "capabilities": clean_capabilities(source.get("capabilities")),
        "water_outage": _clean_flag(source.get("water_outage"), label="断水"),
        "power_outage": _clean_flag(source.get("power_outage"), label="停電"),
    }


# --- 書き込み ---


def new_facility_id() -> str:
    """施設のID（毎回ちがう値）。"""
    return f"fc_{uuid.uuid4().hex}"


def new_placement_id() -> str:
    """移る先の決定のID（毎回ちがう値）。"""
    return f"pl_{uuid.uuid4().hex}"


def _next_version(current: dict[str, Any] | None) -> int:
    """次の版の番号（まだ無ければ1）。"""
    if not current:
        return 1
    version = current.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise FacilityValidationError("施設の版が読み取れません")
    return version + 1


def build_row(
    *,
    org_id: str,
    created_by: str,
    created_role: str,
    fields: dict[str, Any],
    version: int,
    facility_id: str | None = None,
    staff_join_code_hash: str | None = None,
    staff_join_code: str | None = None,
    change_kind: str = CHANGE_INFO,
) -> dict[str, Any]:
    """`raw_facilities` に入れる1行を作る（まだ書かない）。

    施設の参加コード（ハッシュと元のコード）は、前の版の値を呼ぶ側が渡す
    （渡し忘れると、最新の版でコードが消えて施設の職員が入れなくなる）。
    """
    if not org_id:
        raise FacilityValidationError("組織が決まっていません")
    if change_kind not in (CHANGE_INFO, CHANGE_JOIN_CODE):
        raise FacilityValidationError("版を足す理由の値が正しくありません")
    row: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "facility_id": facility_id or new_facility_id(),
        "version": version,
    }
    row.update(clean_fields(fields))
    row["staff_join_code_hash"] = staff_join_code_hash or None
    row["staff_join_code"] = staff_join_code or None
    row["change_kind"] = change_kind
    return row


def build_update_row(
    *,
    current: dict[str, Any],
    created_by: str,
    created_role: str,
    vacancies: Any = None,
    water_outage: Any = None,
    power_outage: Any = None,
    capabilities: Any = None,
) -> dict[str, Any]:
    """空き・断水・停電（と対応できる介護・医療）を変えた**新しい版**の行を作る（要件定義書F5・10-5）。

    名前・種類・場所は前の版から引き継ぐ。対応できる介護・医療は、施設の職員が直すとき
    だけ渡す（使いやすさの改修の計画.md 9章）。渡されなかった項目も前の版のままにする
    （`None` を「いいえ」で上書きしない）。施設の参加コードも前の版から引き継ぐ。
    `current` は参加コードの列まで読んだ行（`fetch_facility(..., with_join_code=True)`）を渡す。
    """
    if not current:
        raise FacilityValidationError("施設が見つかりません")
    version = _next_version(current)
    fields = {
        "facility_name": current.get("facility_name"),
        "facility_type": current.get("facility_type"),
        "address": current.get("address"),
        "vacancies": current.get("vacancies") if vacancies is None else vacancies,
        "capabilities": (
            list(current.get("capabilities") or []) if capabilities is None else capabilities
        ),
        "water_outage": (
            bool(current.get("water_outage")) if water_outage is None else water_outage
        ),
        "power_outage": (
            bool(current.get("power_outage")) if power_outage is None else power_outage
        ),
    }
    return build_row(
        org_id=str(current.get("org_id") or ""),
        created_by=created_by,
        created_role=created_role,
        fields=fields,
        version=version,
        facility_id=str(current.get("facility_id") or ""),
        staff_join_code_hash=current.get("staff_join_code_hash"),
        staff_join_code=current.get("staff_join_code"),
    )


def build_join_code_row(
    *, current: dict[str, Any], created_by: str, created_role: str
) -> tuple[dict[str, Any], str]:
    """施設の参加コードを発行し直した**新しい版**の行と、元のコードを返す（9章）。

    空き・名前などは前の版のまま引き継ぎ、コード（ハッシュと元のコード）だけを新しくする。
    `stg_facilities_latest` は施設ごとに最新の版を返すので、照合には新しいコードだけが使われ、
    前のコードではもう入れない。ハッシュのソルトは施設のIDにする（組織のコードと同じ考え方）。
    """
    if not current:
        raise FacilityValidationError("施設が見つかりません")
    facility_id = str(current.get("facility_id") or "")
    if not facility_id:
        raise FacilityValidationError("施設が見つかりません")
    code = codes.generate_code()
    fields = {
        "facility_name": current.get("facility_name"),
        "facility_type": current.get("facility_type"),
        "address": current.get("address"),
        "vacancies": current.get("vacancies"),
        "capabilities": list(current.get("capabilities") or []),
        "water_outage": bool(current.get("water_outage")),
        "power_outage": bool(current.get("power_outage")),
    }
    row = build_row(
        org_id=str(current.get("org_id") or ""),
        created_by=created_by,
        created_role=created_role,
        fields=fields,
        version=_next_version(current),
        facility_id=facility_id,
        staff_join_code_hash=codes.hash_code(code, salt=facility_id),
        staff_join_code=code,
        change_kind=CHANGE_JOIN_CODE,
    )
    return row, code


def save(row: dict[str, Any]) -> dict[str, Any]:
    """作った行を `raw_facilities` に追記する。"""
    bq.insert("raw_facilities", row)
    return row


def clean_candidates(value: Any) -> list[dict[str, Any]]:
    """候補（施設のIDと理由）をそろえる（最大3件）。"""
    if not isinstance(value, (list, tuple)):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        facility_id = item.get("facility_id")
        if not isinstance(facility_id, str) or not facility_id.strip():
            continue
        reason = item.get("reason")
        cleaned.append(
            {
                "facility_id": facility_id.strip(),
                "reason": (
                    reason.strip()[:CANDIDATE_REASON_MAX_LENGTH]
                    if isinstance(reason, str) and reason.strip()
                    else ""
                ),
            }
        )
        if len(cleaned) >= MAX_CANDIDATES:
            break
    return cleaned


def candidates_column(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """JSON の列に入れる形（**辞書**にする。空の配列を渡せないため）。

    `services/bq.py` の `_array_parameter` は、0件の配列では要素の型を決められない。
    F8 の `structured` と同じ形に合わせる。
    """
    return {"items": list(candidates)}


def candidates_from_column(value: Any) -> list[dict[str, Any]]:
    """JSON の列から候補を読み戻す（文字列で返ってくることがある）。"""
    source: Any = value
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except (TypeError, ValueError):
            source = None
    if isinstance(source, dict):
        return clean_candidates(source.get("items"))
    if isinstance(source, list):
        return clean_candidates(source)
    return []


def build_placement_row(
    *,
    survivor_id: str,
    org_id: str,
    created_by: str,
    created_role: str,
    candidates: Any,
    chosen_facility_id: Any,
    chosen_reason: Any,
) -> dict[str, Any]:
    """`raw_placements` に入れる1行を作る（まだ書かない）。

    **候補から選んだかどうかは、渡された値を信じずにこちらで数える**（要件定義書F5）。
    """
    if not survivor_id or not org_id:
        raise FacilityValidationError("記録が決まっていません")
    chosen = _clean_text(
        chosen_facility_id, label="移る先の施設", max_length=NAME_MAX_LENGTH, required=True
    )
    reason = _clean_text(
        chosen_reason, label="選んだ理由", max_length=REASON_MAX_LENGTH, required=True
    )
    cleaned = clean_candidates(candidates)
    return {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "placement_id": new_placement_id(),
        "candidates": candidates_column(cleaned),
        "chosen_facility_id": chosen,
        "chosen_from_candidates": any(item["facility_id"] == chosen for item in cleaned),
        "chosen_reason": reason,
    }


def save_placement(row: dict[str, Any]) -> dict[str, Any]:
    """移る先の決定を `raw_placements` に追記する。"""
    bq.insert("raw_placements", row)
    return row


# --- 候補を並べる前に外す・ルールで並べる（要件定義書F5、画面の改修の計画.md 7章） ---


def required_capabilities(medical_needs: Any) -> tuple[list[str], list[str]]:
    """F3 の「必要な医療」（文章）を、対応できる医療のコードに読み替える。

    Returns:
        (読み替えられたコードの一覧, 読み替えられなかった文章の一覧)

    読み替えられなかったものは、**施設を外す理由にしない**（分からないものを
    「対応できない」と決めると、候補が理由なく消える）。
    """
    matched: list[str] = []
    unmatched: list[str] = []
    for need in medical_needs or []:
        if not isinstance(need, str):
            continue
        text = need.strip()
        if not text:
            continue
        found: str | None = None
        for code, words in CAPABILITY_KEYWORDS.items():
            if any(word and word in text for word in words):
                found = code
                break
        if found is None:
            unmatched.append(text)
        elif found not in matched:
            matched.append(found)
    return matched, unmatched


def filter_eligible(
    rows: list[dict[str, Any]], *, medical_needs: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """**候補を並べる前に**、空きがない施設と必要な医療に対応できない施設を外す。

    Returns:
        (渡してよい施設の行, 外した施設とその理由)

    断水・停電では外さない。要件定義書F5が外すと決めているのは2つだけで、
    断水していても「今夜だけ受け入れる」判断が要ることがある（並び順を後にし、理由に書く）。
    """
    required, _ = required_capabilities(medical_needs)
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        vacancies = row.get("vacancies")
        if not isinstance(vacancies, int) or isinstance(vacancies, bool) or vacancies <= 0:
            excluded.append(_excluded(row, EXCLUDED_NO_VACANCY))
            continue
        capabilities = [str(item) for item in (row.get("capabilities") or [])]
        missing = [code for code in required if code not in capabilities]
        if missing:
            excluded.append(_excluded(row, EXCLUDED_NO_CAPABILITY, missing=missing))
            continue
        eligible.append(row)
    return eligible, excluded


def care_needs(health_row: dict[str, Any] | None) -> list[str]:
    """健康・介護の情報の文章の欄から、介護の必要（認知症・車いす・寝たきり）を読み取る。

    読み違えても、並び順が変わるだけで施設は外れない（外すのは「必要な医療」だけ）。
    """
    found: list[str] = []
    for name in CARE_NEED_FIELDS:
        text = str((health_row or {}).get(name) or "").strip()
        if not text or any(word in text for word in _NO_NEED_WORDS):
            continue
        for code in CARE_NEED_CODES:
            if code not in found and any(word in text for word in CAPABILITY_KEYWORDS[code]):
                found.append(code)
    return found


def _updated_order(value: Any) -> float:
    """新しい情報ほど先にするための数（読めない日時は最も古い扱い）。"""
    if isinstance(value, dt.datetime):
        return -value.timestamp()
    if isinstance(value, str) and value:
        try:
            return -dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def candidate_reason(
    row: dict[str, Any], *, wanted: list[str], unmatched_needs: list[str]
) -> str:
    """候補の理由を、ルールで決まった言い回しで作る（AI の文の代わり）。"""
    capabilities = {str(item) for item in (row.get("capabilities") or [])}
    parts = [f"空き{row.get('vacancies')}"]
    matched = [CAPABILITIES[code] for code in wanted if code in capabilities]
    if matched:
        parts.append("対応できるもの：" + "・".join(matched))
    outages = [label for key, label in (("water_outage", "断水"), ("power_outage", "停電")) if row.get(key)]
    parts.append(("・".join(outages) + "あり") if outages else "断水・停電なし")
    if row.get("is_stale"):
        parts.append(f"情報は{STALE_HOURS}時間以上前の更新（電話で確かめる）")
    else:
        parts.append(f"情報は{STALE_HOURS}時間以内の更新")
    reason = "。".join(parts) + "。"
    if unmatched_needs:
        reason += "選択肢に無い必要な医療は、対応できるかを施設に確かめる必要がある。"
    return reason[:CANDIDATE_REASON_MAX_LENGTH]


def rank_candidates(
    eligible: list[dict[str, Any]],
    *,
    medical_needs: Any,
    health_row: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """外したあとに残った施設を、ルールで並べて最大3件を返す（AI は使わない。7-2）。

    並べ方（上の項目が同じときだけ、次の項目で比べる）：
    1. 必要な医療と介護の必要のうち、施設が対応できるものが多い
    2. 空きの数が多い
    3. 断水・停電が少ない
    4. 情報が新しい（24時間より古い施設は後）
    5. 施設のID（同じ点のときも、毎回同じ順にする）
    """
    required, unmatched = required_capabilities(medical_needs)
    wanted = required + [code for code in care_needs(health_row) if code not in required]

    def key(row: dict[str, Any]) -> tuple:
        capabilities = {str(item) for item in (row.get("capabilities") or [])}
        vacancies = row.get("vacancies")
        return (
            -sum(1 for code in wanted if code in capabilities),
            -(vacancies if isinstance(vacancies, int) and not isinstance(vacancies, bool) else 0),
            int(bool(row.get("water_outage"))) + int(bool(row.get("power_outage"))),
            int(bool(row.get("is_stale"))),
            _updated_order(row.get("updated_at")),
            str(row.get("facility_id") or ""),
        )

    ranked = sorted(eligible, key=key)[:MAX_CANDIDATES]
    return [
        {
            **payload(row),
            "reason": candidate_reason(row, wanted=wanted, unmatched_needs=unmatched),
        }
        for row in ranked
    ]


def _excluded(
    row: dict[str, Any], reason: str, *, missing: list[str] | None = None
) -> dict[str, Any]:
    return {
        "facility_id": row.get("facility_id"),
        "facility_name": row.get("facility_name"),
        "reason": reason,
        "missing_capability_labels": [CAPABILITIES[code] for code in (missing or [])],
    }


# --- 読み ---


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


_COLUMNS = (
    "facility_id",
    "org_id",
    "version",
    "facility_name",
    "facility_type",
    "address",
    "vacancies",
    "capabilities",
    "water_outage",
    "power_outage",
    "updated_at",
    "updated_by",
    # 最後に空きなどを更新した人の立場（施設の職員か専門職か。9章）
    "updated_role",
    "is_stale",
)

# 施設の参加コードの列（9章）。**画面に返す一覧には入れない。** 版を足すときに引き継ぐのと、
# その組織の専門職に元のコードを見せるときだけ読む。
_JOIN_CODE_COLUMNS = ("staff_join_code_hash", "staff_join_code")


def list_facilities(*, org_id: str, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """組織の施設を、空きのある施設から読む（`stg_facilities_latest`）。"""
    if not org_id:
        return []
    columns = ",\n       ".join(_COLUMNS)
    sql = (
        f"SELECT {columns}\n"
        f"FROM {_staging_table('stg_facilities_latest')}\n"
        "WHERE org_id = @org_id\n"
        "ORDER BY vacancies DESC, facility_name ASC\n"
        f"LIMIT {int(limit)}"
    )
    return bq.query(sql, {"org_id": org_id})


def fetch_facility(
    *, facility_id: str, org_id: str, with_join_code: bool = False
) -> dict[str, Any] | None:
    """施設を1件読む（更新のときに実在するか確かめる）。

    組織でも絞る（ほかの組織の施設は、無いときと同じ None）。`with_join_code` は、
    版を足すとき（参加コードを引き継ぐ）と、専門職に参加コードを見せるときだけ true にする。
    """
    if not facility_id or not org_id:
        return None
    names = _COLUMNS + (_JOIN_CODE_COLUMNS if with_join_code else ())
    columns = ",\n       ".join(names)
    sql = (
        f"SELECT {columns}\n"
        f"FROM {_staging_table('stg_facilities_latest')}\n"
        "WHERE facility_id = @facility_id AND org_id = @org_id"
    )
    return bq.query_one(sql, {"facility_id": facility_id, "org_id": org_id})


def resolve_staff_join_code(code: str) -> tuple[str, str] | None:
    """施設の参加コードから（施設のID, 組織のID）を決める。合わなければ None（9章）。

    組織の参加コードと同じく、どの施設のものか分からない状態で入力されるので、
    コードを持つ施設を順に照合する。2か所に当たったときは**どちらも採らない**
    （施設と組織が決まらないまま採ると、別の施設を直せる経路ができる）。
    コードの値はログに出さない。
    """
    sql = (
        "SELECT facility_id, org_id, staff_join_code_hash\n"
        f"FROM {_staging_table('stg_facilities_latest')}\n"
        "WHERE staff_join_code_hash IS NOT NULL\n"
        "ORDER BY facility_id"
    )
    matches: list[tuple[str, str]] = []
    for row in bq.query(sql):
        facility_id = row.get("facility_id")
        org_id = row.get("org_id")
        if not facility_id or not org_id:
            continue
        if codes.verify_code(code, salt=facility_id, code_hash=row.get("staff_join_code_hash")):
            matches.append((str(facility_id), str(org_id)))
    if not matches:
        logger.info("facility_join_code_rejected: matched=0")
        return None
    if len(matches) > 1:
        logger.warning("facility_join_code_ambiguous: matched=%d", len(matches))
        return None
    logger.info("facility_join_code_accepted")
    return matches[0]


def list_placements(*, survivor_id: str, org_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """その人の移る先の決定を新しい順に読む。"""
    if not survivor_id or not org_id:
        return []
    sql = (
        "SELECT placement_id,\n"
        "       survivor_id,\n"
        "       candidates,\n"
        "       chosen_facility_id,\n"
        "       chosen_from_candidates,\n"
        "       chosen_reason,\n"
        "       decided_at,\n"
        "       decided_by,\n"
        "       decided_role\n"
        f"FROM {_staging_table('stg_placements')}\n"
        "WHERE survivor_id = @survivor_id AND org_id = @org_id\n"
        "ORDER BY decided_at DESC\n"
        f"LIMIT {int(limit)}"
    )
    return bq.query(sql, {"survivor_id": survivor_id, "org_id": org_id})


# --- 画面に返す形 ---


def payload(row: dict[str, Any]) -> dict[str, Any]:
    """施設1件分（種類と対応できる医療の名前、古いときの知らせを付ける）。"""
    facility_type = str(row.get("facility_type") or "")
    capabilities = [str(item) for item in (row.get("capabilities") or [])]
    is_stale = bool(row.get("is_stale"))
    updated_role = str(row.get("updated_role") or "")
    return {
        "facility_id": row.get("facility_id"),
        "version": row.get("version"),
        "facility_name": row.get("facility_name"),
        "facility_type": facility_type,
        "facility_type_label": FACILITY_TYPES.get(facility_type, ""),
        "address": row.get("address"),
        "vacancies": row.get("vacancies"),
        "capabilities": capabilities,
        "capability_labels": [CAPABILITIES[code] for code in capabilities if code in CAPABILITIES],
        "water_outage": bool(row.get("water_outage")),
        "power_outage": bool(row.get("power_outage")),
        "updated_at": row.get("updated_at"),
        # 最後に更新したのが施設の職員か専門職か（9章。人の名前は出さない）
        "updated_role": updated_role or None,
        "updated_role_label": UPDATED_ROLE_LABELS.get(updated_role, ""),
        "is_stale": is_stale,
        # 古い施設は、そのことを候補に表示する（要件定義書F5）
        "stale_notice": STALE_NOTICE if is_stale else None,
    }


def join_code_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    """施設の参加コード（元のコード）を返す形（9章。その組織の専門職だけに返す）。

    ハッシュは返さない。まだ発行していない施設は `available` が false。
    """
    row = row or {}
    code = row.get("staff_join_code") or None
    return {
        "facility_id": row.get("facility_id"),
        "facility_name": row.get("facility_name"),
        "staff_join_code": code,
        "available": bool(code),
    }


def placement_payload(
    row: dict[str, Any], *, facility_names: dict[str, str] | None = None
) -> dict[str, Any]:
    """移る先の決定1件分（施設の名前は呼ぶ側が渡す）。"""
    names = facility_names or {}
    chosen = str(row.get("chosen_facility_id") or "")
    candidates = candidates_from_column(row.get("candidates"))
    return {
        "placement_id": row.get("placement_id"),
        "chosen_facility_id": chosen,
        "chosen_facility_name": names.get(chosen, ""),
        "chosen_from_candidates": bool(row.get("chosen_from_candidates")),
        "chosen_reason": row.get("chosen_reason"),
        "candidates": [
            {**item, "facility_name": names.get(item["facility_id"], "")} for item in candidates
        ],
        "decided_at": row.get("decided_at"),
        "decided_role": row.get("decided_role"),
    }
