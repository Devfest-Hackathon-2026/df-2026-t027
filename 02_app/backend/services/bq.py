"""BigQuery の共通処理（要件定義書10-3・10-4、開発手順書 Step 2-3）。

守ること：

- 書き込みはパラメータ付きの DML の INSERT だけ。ストリーミング挿入は無料枠の外なので使わない。
- 上書きと消去（UPDATE・DELETE）はしない。記録は版と履歴で残す（要件定義書7-1）。
- 表と列の名前は、英数字と `_` 以外を含むものを受け付けない。値は必ずクエリのパラメータで渡し、
  文字列をつないで SQL を作らない（要件定義書10-3）。
- ログに記録の本文や健康の情報を出さない。処理の名前・かかった時間・行数だけを出す（要件定義書10-2の6）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time
from typing import Any, Sequence

from google.cloud import bigquery

from backend.config import get_settings

logger = logging.getLogger(__name__)

# 英数字と `_` だけ。表・列・パラメータの名前に使う。
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")

# Google Cloud のプロジェクトIDに使える文字（英小文字・数字・ハイフン）。場所は英小文字・数字・ハイフン。
PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9\-]{4,29}$")
LOCATION_PATTERN = re.compile(r"^[a-z][a-z0-9\-]{1,30}$")

# 空の配列は要素の型を決められない。7-3-1 の配列の列はすべて ARRAY<STRING> なので既定を STRING にする。
EMPTY_ARRAY_TYPE = "STRING"


class InvalidIdentifier(ValueError):
    """表・列・パラメータの名前に、英数字と `_` 以外が含まれていた。"""


class BigQueryUnavailable(RuntimeError):
    """BigQuery を呼べなかった（設定漏れ・認証切れなど）。"""


_client: bigquery.Client | None = None

# クライアントを作るところの鍵。F1（Step 5）から、関係のない読みを別のスレッドで
# 同時に走らせるようになったため、鍵が無いと**同時に2つ作られる**（認証も2回走る）。
_client_lock = threading.Lock()


# --- 名前の検査 ---


def validate_identifier(name: str) -> str:
    """表・列・パラメータの名前を検査して返す。英数字と `_` 以外はすべて拒む。"""
    if not isinstance(name, str) or not IDENTIFIER_PATTERN.match(name):
        raise InvalidIdentifier(f"表・列の名前に使えない文字が含まれています: {name!r}")
    return name


def qualified_table(project: str, dataset: str, table: str) -> str:
    """`プロジェクト.データセット.表` の形にする。名前はすべて検査する。"""
    if not PROJECT_PATTERN.match(project or ""):
        raise InvalidIdentifier(f"プロジェクトIDの形ではありません: {project!r}")
    validate_identifier(dataset)
    validate_identifier(table)
    return f"`{project}.{dataset}.{table}`"


# --- 値をクエリのパラメータにする ---


def _scalar(name: str, type_: str, value: Any) -> bigquery.ScalarQueryParameter:
    return bigquery.ScalarQueryParameter(name, type_, value)


def _as_json_text(value: Any) -> str:
    # 日本語をそのまま残す（BigQuery の JSON 型は UTF-8 を扱える）
    return json.dumps(value, ensure_ascii=False)


def _array_parameter(name: str, values: Sequence[Any]) -> bigquery.ArrayQueryParameter:
    if not values:
        return bigquery.ArrayQueryParameter(name, EMPTY_ARRAY_TYPE, [])
    first = values[0]
    if isinstance(first, bool):
        element_type = "BOOL"
    elif isinstance(first, str):
        element_type = "STRING"
    elif isinstance(first, int):
        element_type = "INT64"
    elif isinstance(first, float):
        element_type = "FLOAT64"
    else:
        raise TypeError(f"配列の要素の型を決められません: {type(first).__name__}")
    if any(not isinstance(item, type(first)) for item in values):
        raise TypeError("配列の要素の型がそろっていません")
    return bigquery.ArrayQueryParameter(name, element_type, list(values))


def _value_to_sql(param_name: str, value: Any) -> tuple[str, Any]:
    """値を「SQL に書く式」と「クエリのパラメータ」の組にする。

    JSON の列（辞書・辞書の配列）は `PARSE_JSON(@…)` にして、文字列のパラメータで渡す。
    None は SQL の `NULL` にする（型を決められないため。結果は未設定と同じ）。
    """
    placeholder = f"@{param_name}"

    if value is None:
        return "NULL", None
    if isinstance(value, bool):
        return placeholder, _scalar(param_name, "BOOL", value)
    if isinstance(value, int):
        return placeholder, _scalar(param_name, "INT64", value)
    if isinstance(value, float):
        return placeholder, _scalar(param_name, "FLOAT64", value)
    if isinstance(value, str):
        return placeholder, _scalar(param_name, "STRING", value)
    if isinstance(value, dt.datetime):
        return placeholder, _scalar(param_name, "TIMESTAMP", value)
    if isinstance(value, dt.date):
        return placeholder, _scalar(param_name, "DATE", value)
    if isinstance(value, dict):
        return f"PARSE_JSON({placeholder})", _scalar(param_name, "STRING", _as_json_text(value))
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, (dict, list)) for item in value):
            return f"PARSE_JSON({placeholder})", _scalar(
                param_name, "STRING", _as_json_text(list(value))
            )
        return placeholder, _array_parameter(param_name, list(value))

    raise TypeError(f"BigQuery の型に対応づけられない値です: {type(value).__name__}")


def build_parameters(params: dict[str, Any]) -> list[Any]:
    """読み取りのクエリのパラメータを作る。名前は英数字と `_` だけ。"""
    built: list[Any] = []
    for name, value in params.items():
        validate_identifier(name)
        _, parameter = _value_to_sql(name, value)
        if parameter is None:
            # None は SQL 側で IS NULL を書く。型を決められないパラメータは渡さない。
            raise TypeError(f"パラメータ {name} が None です。SQL 側で NULL を扱ってください")
        built.append(parameter)
    return built


# --- 書き込み（INSERT だけ） ---


def build_insert(
    project: str, dataset: str, table: str, row: dict[str, Any]
) -> tuple[str, list[Any]]:
    """1行追記する INSERT の SQL とパラメータを作る（接続はしない）。"""
    if not row:
        raise ValueError("追記する列がありません")

    target = qualified_table(project, dataset, table)
    columns: list[str] = []
    expressions: list[str] = []
    parameters: list[Any] = []

    for column, value in row.items():
        validate_identifier(column)
        expression, parameter = _value_to_sql(f"p_{column}", value)
        columns.append(column)
        expressions.append(expression)
        if parameter is not None:
            parameters.append(parameter)

    sql = (
        f"INSERT INTO {target} ({', '.join(columns)})\n"
        f"VALUES ({', '.join(expressions)})"
    )
    return sql, parameters


# --- 接続と実行 ---


def get_client() -> bigquery.Client:
    """BigQuery のクライアント（プロセスで1回だけ作る）。認証は ADC かサービスアカウント。"""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        # 鍵を取るまでに、ほかのスレッドが作り終えていることがある
        if _client is not None:
            return _client
        settings = get_settings()
        if not settings.project_id:
            raise BigQueryUnavailable(
                "GOOGLE_CLOUD_PROJECT が設定されていません（.env を確認してください）"
            )
        _client = bigquery.Client(project=settings.project_id, location=settings.bq_location)
        return _client


def _run(sql: str, parameters: Sequence[Any], operation: str) -> bigquery.table.RowIterator:
    client = get_client()
    settings = get_settings()
    job_config = bigquery.QueryJobConfig(query_parameters=list(parameters))
    started = time.monotonic()
    try:
        job = client.query(sql, job_config=job_config, location=settings.bq_location)
        result = job.result()
    except Exception as exc:  # 認証切れ・表がない・構文の誤りなど
        logger.warning("bq_failed: operation=%s error=%s", operation, type(exc).__name__)
        raise BigQueryUnavailable(f"{operation} に失敗しました: {exc}") from exc
    # ログに本文は出さない（要件定義書10-2の6）
    logger.info(
        "bq_done: operation=%s elapsed=%.2fs rows=%s",
        operation,
        time.monotonic() - started,
        getattr(job, "num_dml_affected_rows", None) or result.total_rows,
    )
    return result


def insert(table: str, row: dict[str, Any], *, dataset: str | None = None) -> int:
    """raw の表に1行追記する。追記した行数を返す。"""
    settings = get_settings()
    sql, parameters = build_insert(
        settings.project_id, dataset or settings.dataset_raw, table, row
    )
    _run(sql, parameters, operation=f"insert:{table}")
    return 1


def query(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """パラメータ付きで読み、辞書のリストで返す。"""
    parameters = build_parameters(params or {})
    result = _run(sql, parameters, operation="query")
    return [dict(row.items()) for row in result]


def query_one(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """1行だけ返す（なければ None）。"""
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: dict[str, Any] | None = None, *, operation: str = "execute") -> None:
    """DDL や INSERT を流す（確認スクリプトの一時の表づくりに使う）。"""
    _run(sql, build_parameters(params or {}), operation=operation)
