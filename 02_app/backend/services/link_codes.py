"""確認コードの発行と照合（F1。要件定義書7-3-1の raw_link_codes・10-3、開発手順書 Step 5-2）。

専門職が被災者を登録したときに発行し、紙に書いて本人に渡す。本人はそのコードを入れて、
すでに作られている記録と自分のアカウントをつなぐ（F0の「確認コードでつなぐ」）。

守ること（要件定義書10-3）：

- **コードはハッシュにして保存し、平文はどこにも残さない。** 発行の応答で1回だけ返す。
  画面に出すのは専門職の手元だけで、ログにも出さない。
- **1回使ったら無効にする。** 判定は `stg_link_codes.is_valid`（`used` が無く、期限内）。
  加えて、つなぎ込みの側で「その記録にもう本人がついていないか」も確かめる（`routers`）。
- **何度も間違えたら一定時間入力できないようにする。**

### 照合が「順に試す」形になる理由

ソルトは被災者のID（ナレッジベース06「ソルトにはそのコードが有効な範囲のIDを使う」）。
本人がコードを入れる時点では、被災者のIDも組織も分からない。そのため
`stg_link_codes` の有効な行を1回読み、行ごとに `hash_code(入力, salt=survivor_id)` を
計算して突き合わせる。ハッシュの計算はコード1件あたり1マイクロ秒ほどで、
BigQuery の読みは1回だけなので、デモの規模では問題にならない。

### 誤りの回数を「利用者ごと」に数える理由

`stg_link_codes.failed_count_1h` は `code_hash` ごとの数で、**間違ったコードには使えない**。
間違ったコードはどの被災者のものか分からず、ソルトが決まらないのでハッシュを特定できない。
そこで `raw_link_codes` に `action="failed"` の行を**入力した利用者のID付き**で足し、
直近1時間をその利用者で数える。`org_id`・`survivor_id` は NOT NULL なので `unknown` を入れる
（表を作り直さずに済ませるため。`code_hash` は利用者のIDをソルトにするので、
発行済みのコードのハッシュとは重ならず、`stg_link_codes` の `is_valid` にも出てこない）。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from backend.config import get_settings
from backend.services import bq, codes

logger = logging.getLogger(__name__)

# raw_link_codes.action の値（要件定義書7-3-1）。
ACTION_ISSUED = "issued"
ACTION_USED = "used"
ACTION_FAILED = "failed"

# 家族の招待コード（使いやすさの改修の計画.md 10章）。本人が発行し、家族が入れて記録につながる。
# 同じ表に別の action で残す。`stg_link_codes.is_valid` は `issued` の期限だけを見るので、
# 招待コードは本人をつなぐ確認コードの照合（`fetch_valid_codes`）には出てこない。
ACTION_FAMILY_ISSUED = "family_issued"
ACTION_FAMILY_USED = "family_used"

# 招待コードのソルトの頭に付ける文字。本人をつなぐ確認コードと同じ被災者のIDでも、
# ハッシュが重ならないようにする
FAMILY_SALT_PREFIX = "family:"

# コードの有効期限。紙で渡して後日入れる使い方に耐える長さにする（正本に数字がないため本人の判断）。
ISSUE_TTL_DAYS = 7

# 直近1時間に許す誤りの回数。これに達したら、その利用者は照合に進めない。
MAX_FAILURES_PER_HOUR = 5

# 誤りを数える窓（時間）。
FAILURE_WINDOW_HOURS = 1

# 間違ったコードの行に入れる、組織と被災者の印（NOT NULL の列を埋めるため）。
UNKNOWN = "unknown"


class TooManyAttempts(RuntimeError):
    """直近の誤りが多すぎる（一定時間、確認コードを入れられない）。"""


@dataclass(frozen=True)
class IssuedCode:
    """発行した確認コード。`code` は平文で、この応答のためだけに持つ。"""

    code: str
    code_hash: str
    survivor_id: str
    expires_at: dt.datetime


@dataclass(frozen=True)
class ResolvedCode:
    """入力されたコードが当たった行（どの被災者の記録につなぐか）。"""

    code_hash: str
    survivor_id: str
    org_id: str


def _staging_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_staging, table)


def _raw_table(table: str) -> str:
    settings = get_settings()
    return bq.qualified_table(settings.project_id, settings.dataset_raw, table)


def _event(
    *,
    org_id: str,
    survivor_id: str,
    created_by: str,
    created_role: str,
    code_hash: str,
    action: str,
    expires_at: dt.datetime | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "org_id": org_id,
        "created_at": dt.datetime.now(dt.timezone.utc),
        "created_by": created_by,
        "created_role": created_role,
        "survivor_id": survivor_id,
        "code_hash": code_hash,
        "action": action,
        "expires_at": expires_at,
    }


# --- 発行 ---


def issue_code(
    *, survivor_id: str, org_id: str, created_by: str, created_role: str
) -> IssuedCode:
    """確認コードを発行し、ハッシュだけを raw_link_codes に残す。平文は戻り値にだけ入れる。"""
    if not survivor_id:
        raise ValueError("被災者のIDがありません")
    code = codes.generate_code()
    code_hash = codes.hash_code(code, salt=survivor_id)
    row = _event(
        org_id=org_id,
        survivor_id=survivor_id,
        created_by=created_by,
        created_role=created_role,
        code_hash=code_hash,
        action=ACTION_ISSUED,
    )
    row["expires_at"] = row["created_at"] + dt.timedelta(days=ISSUE_TTL_DAYS)
    bq.insert("raw_link_codes", row)
    # コードの値はログに出さない（紙に書く値で、漏れると他人が記録につながる）
    logger.info("link_code_issued: ttl_days=%d", ISSUE_TTL_DAYS)
    return IssuedCode(
        code=code,
        code_hash=code_hash,
        survivor_id=survivor_id,
        expires_at=row["expires_at"],
    )


# --- 読み ---


def fetch_valid_codes() -> list[dict[str, Any]]:
    """まだ使われておらず、期限内のコードのハッシュを読む（照合のため全件を順に試す）。"""
    sql = (
        "SELECT code_hash, survivor_id, org_id\n"
        f"FROM {_staging_table('stg_link_codes')}\n"
        "WHERE is_valid\n"
        "ORDER BY issued_at DESC"
    )
    return bq.query(sql)


def count_recent_failures(user_id: str) -> int:
    """その利用者が直近1時間に何回間違えたかを数える。

    `stg_link_codes` はコードごとの集計なので使えない（間違ったコードはハッシュが決まらない）。
    `raw_link_codes` を発生日時で絞って読む（日付で分割してあるため読む量は小さい）。
    """
    if not user_id:
        return 0
    sql = (
        "SELECT COUNT(*) AS failures\n"
        f"FROM {_raw_table('raw_link_codes')}\n"
        f"WHERE action = \"{ACTION_FAILED}\"\n"
        "  AND created_by = @user_id\n"
        "  AND created_at >= TIMESTAMP_SUB("
        f"CURRENT_TIMESTAMP(), INTERVAL {FAILURE_WINDOW_HOURS} HOUR)"
    )
    row = bq.query_one(sql, {"user_id": user_id})
    return int((row or {}).get("failures") or 0)


# --- 照合 ---


def record_failure(code: str, *, user_id: str, created_role: str = "survivor") -> None:
    """入力の誤りを1行残す。コードの平文は残さない（利用者のIDをソルトにしたハッシュにする）。

    確認コードを入れるのは本人（F0の「支援を受けたい」の経路）。家族の招待コードを入れるのは
    一般の支援者なので、そのときは `created_role` に立場を渡す（使いやすさの改修の計画.md 10章）。
    誤りの回数は、どちらのコードでも同じ利用者の数として数える。
    """
    bq.insert(
        "raw_link_codes",
        _event(
            org_id=UNKNOWN,
            survivor_id=UNKNOWN,
            created_by=user_id,
            created_role=created_role,
            code_hash=codes.hash_code(code, salt=user_id),
            action=ACTION_FAILED,
        ),
    )


def resolve_code(code: str, *, user_id: str) -> ResolvedCode | None:
    """入力されたコードから、つなぐ先の記録を決める。合わなければ None。

    - 直近の誤りが上限に達していたら `TooManyAttempts`（照合そのものを行わない）。
    - 形が正しくない入力（空・短すぎる・使えない文字）は、どのコードにも当たりえないので
      誤りに数えない（打ち間違いで入力できなくなるのを防ぐ）。
    """
    try:
        normalized = codes.normalize_code(code)
    except codes.InvalidCode:
        return None

    if count_recent_failures(user_id) >= MAX_FAILURES_PER_HOUR:
        logger.info("link_code_locked: window_hours=%d", FAILURE_WINDOW_HOURS)
        raise TooManyAttempts(
            "確認コードの入力を何度も間違えました。1時間ほど待ってからお試しください"
        )

    for row in fetch_valid_codes():
        survivor_id = row.get("survivor_id")
        if not survivor_id:
            continue
        if codes.verify_code(normalized, salt=survivor_id, code_hash=row.get("code_hash")):
            logger.info("link_code_accepted")
            return ResolvedCode(
                code_hash=str(row.get("code_hash")),
                survivor_id=str(survivor_id),
                org_id=str(row.get("org_id") or ""),
            )

    logger.info("link_code_rejected")
    record_failure(normalized, user_id=user_id)
    return None


def mark_used(resolved: ResolvedCode, *, used_by: str) -> None:
    """使ったことを残す（この行が入ると `stg_link_codes.is_valid` が false になる）。"""
    bq.insert(
        "raw_link_codes",
        _event(
            org_id=resolved.org_id,
            survivor_id=resolved.survivor_id,
            created_by=used_by,
            created_role="survivor",
            code_hash=resolved.code_hash,
            action=ACTION_USED,
        ),
    )


# --- 家族の招待コード（使いやすさの改修の計画.md 10章） ---
#
# 本人がプロフィールで発行し、家族（一般の支援者）が入れると、その記録の家族としてつながる。
# 作りは確認コードと同じにする：ハッシュだけを残す・1回使ったら無効・期限がある・
# 何度も間違えたら一定時間入れられない（回数は確認コードと合わせて利用者ごとに数える）。


def _family_salt(survivor_id: str) -> str:
    return f"{FAMILY_SALT_PREFIX}{survivor_id}"


def issue_family_code(*, survivor_id: str, org_id: str, created_by: str) -> IssuedCode:
    """家族の招待コードを発行する（発行できるのは本人だけ。呼ぶ側が確かめる）。"""
    if not survivor_id:
        raise ValueError("被災者のIDがありません")
    code = codes.generate_code()
    code_hash = codes.hash_code(code, salt=_family_salt(survivor_id))
    row = _event(
        org_id=org_id,
        survivor_id=survivor_id,
        created_by=created_by,
        created_role="survivor",
        code_hash=code_hash,
        action=ACTION_FAMILY_ISSUED,
    )
    row["expires_at"] = row["created_at"] + dt.timedelta(days=ISSUE_TTL_DAYS)
    bq.insert("raw_link_codes", row)
    # コードの値はログに出さない（漏れると他人が家族として記録につながる）
    logger.info("family_code_issued: ttl_days=%d", ISSUE_TTL_DAYS)
    return IssuedCode(
        code=code,
        code_hash=code_hash,
        survivor_id=survivor_id,
        expires_at=row["expires_at"],
    )


def fetch_valid_family_codes() -> list[dict[str, Any]]:
    """まだ使われておらず、期限内の招待コードのハッシュを読む。

    `stg_link_codes` は確認コード（`issued`）の期限しか見ないので、`raw_link_codes` を
    コードごとにまとめて読む（`count_recent_failures` と同じく raw から読む）。
    期限より前に発行したものは有効になりえないので、発生日時で絞って読む量を減らす
    （日付で分割してある）。
    """
    sql = (
        "SELECT code_hash, MAX(survivor_id) AS survivor_id, MAX(org_id) AS org_id\n"
        f"FROM {_raw_table('raw_link_codes')}\n"
        "WHERE action IN UNNEST(@actions)\n"
        "  AND created_at >= TIMESTAMP_SUB("
        f"CURRENT_TIMESTAMP(), INTERVAL {ISSUE_TTL_DAYS} DAY)\n"
        "GROUP BY code_hash\n"
        "HAVING COUNTIF(action = @used) = 0\n"
        "  AND MAX(IF(action = @issued, expires_at, NULL)) > CURRENT_TIMESTAMP()\n"
        "ORDER BY MAX(created_at) DESC"
    )
    return bq.query(
        sql,
        {
            "actions": [ACTION_FAMILY_ISSUED, ACTION_FAMILY_USED],
            "issued": ACTION_FAMILY_ISSUED,
            "used": ACTION_FAMILY_USED,
        },
    )


def resolve_family_code(code: str, *, user_id: str, user_role: str) -> ResolvedCode | None:
    """入力された招待コードから、つなぐ先の記録を決める。合わなければ None。

    誤りの扱いは `resolve_code` と同じ（上限に達したら `TooManyAttempts`。形が違う入力は数えない）。
    """
    try:
        normalized = codes.normalize_code(code)
    except codes.InvalidCode:
        return None

    if count_recent_failures(user_id) >= MAX_FAILURES_PER_HOUR:
        logger.info("family_code_locked: window_hours=%d", FAILURE_WINDOW_HOURS)
        raise TooManyAttempts(
            "招待コードの入力を何度も間違えました。1時間ほど待ってからお試しください"
        )

    for row in fetch_valid_family_codes():
        survivor_id = row.get("survivor_id")
        if not survivor_id:
            continue
        if codes.verify_code(
            normalized, salt=_family_salt(str(survivor_id)), code_hash=row.get("code_hash")
        ):
            logger.info("family_code_accepted")
            return ResolvedCode(
                code_hash=str(row.get("code_hash")),
                survivor_id=str(survivor_id),
                org_id=str(row.get("org_id") or ""),
            )

    logger.info("family_code_rejected")
    record_failure(normalized, user_id=user_id, created_role=user_role)
    return None


def mark_family_used(resolved: ResolvedCode, *, used_by: str, used_role: str) -> None:
    """招待コードを使ったことを残す（この行が入ると、そのコードはもう使えない）。"""
    bq.insert(
        "raw_link_codes",
        _event(
            org_id=resolved.org_id,
            survivor_id=resolved.survivor_id,
            created_by=used_by,
            created_role=used_role,
            code_hash=resolved.code_hash,
            action=ACTION_FAMILY_USED,
        ),
    )
