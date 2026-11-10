"""03_sql/05_mart.sql を BigQuery のスケジュールクエリに登録する（開発手順書 Step 11-2）。

使い方（リポジトリ直下で、.venv を有効にしてから）:

    python 04_scripts/schedule_mart.py --dry-run   # 登録せず、送る内容と料金の見積もりを表示
    python 04_scripts/schedule_mart.py             # 登録（すでにあれば SQL と間隔を上書き）
    python 04_scripts/schedule_mart.py --show      # いまの登録と、直近の実行の結果を表示
    python 04_scripts/schedule_mart.py --run-now   # 1回だけ手で走らせる（デモ前の作り直し）
    python 04_scripts/schedule_mart.py --disable   # 止める（登録は残す。料金を止めたいとき）
    python 04_scripts/schedule_mart.py --enable    # 止めたものを動かす
    python 04_scripts/schedule_mart.py --delete    # 登録を消す

守ること：

- **`05_mart.sql` を直したら、このスクリプトを流し直す。** スケジュールクエリは登録したときの
  SQL の写しを持つため、ファイルを直しても登録済みの内容は変わらない（開発手順書 Step 11-2）。
- 実行するアカウントは**サービスアカウント `gdgoc-app`**（人の認証では登録できない。
  登録した人の認証が切れると動かなくなるため）。流す人に `iam.serviceAccounts.actAs` が要る。
- **登録できても、それだけでは実行で落ちる**（2026-11-08 に1回踏んだ）。スケジュールクエリを
  動かすのは BigQuery Data Transfer のサービスエージェント
  `service-（プロジェクト番号）@gcp-sa-bigquerydatatransfer.iam.gserviceaccount.com` で、
  この相手に **`gdgoc-app` を使う権限（`roles/iam.serviceAccountTokenCreator`）** が要る。
  無いと実行の履歴が `FAILED The transfer user is not valid.` になる（開発手順書2章に無い権限）。
- 場所は `.env` の `BQ_LOCATION`（既定は asia-northeast1）。データセットとずれると動かない。
- **料金**：スケジュールクエリそのものは無料で、かかるのは中の SQL のクエリ代だけ。
  1時間ごとだと**1か月に約720回**走る。1回あたりの課金対象バイトは
  `python 04_scripts/run_sql.py 03_sql/05_mart.sql` の出力で分かるので、
  その値を `--billed-mib` に入れて見積もりを出す。
  デモが終わったら `--disable` で止める（放っておくと走り続ける）。
- 認証は gcloud の application-default（`gcloud auth application-default login`）。
  APIキーは作らない（Cloud Credit の対象外になる）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import google.auth
import google.auth.transport.requests
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "02_app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.config import get_settings  # noqa: E402

import run_sql  # noqa: E402

API_ROOT = "https://bigquerydatatransfer.googleapis.com/v1"
SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
DATA_SOURCE_ID = "scheduled_query"

# 登録の名前。これで既存の登録を探すので、変えると二重に登録される。
DISPLAY_NAME = "gdgoc_mart_hourly"

# 間隔（開発手順書 Step 11-2 の「1時間ごと」）。
SCHEDULE = "every 1 hours"

# 1か月に走る回数（30日×24時間）。料金の見積もりに使う。
RUNS_PER_MONTH = 30 * 24

# 無料枠（毎月のクエリ 1TiB）と、超えた分の単価（オンデマンド $6.25/TiB）。
FREE_TIB_PER_MONTH = 1.0
USD_PER_TIB = 6.25

SQL_PATH = REPO_ROOT / "03_sql" / "05_mart.sql"

# サービスアカウント（開発手順書2章で作ったもの）。
SERVICE_ACCOUNT_TEMPLATE = "gdgoc-app@{project}.iam.gserviceaccount.com"


# 実行の履歴が「transfer user is not valid」で落ちたときの直し方（2026-11-08 に踏んだ）。
TRANSFER_USER_HINT = (
    "  → スケジュールクエリを動かすサービスエージェントに、gdgoc-app を使う権限がありません。\n"
    "     PowerShell で次の2つを流し、そのあと `python 04_scripts/schedule_mart.py` で\n"
    "     登録し直してください（値は山かっこの中を自分のものに置き換える）。\n"
    "     1) gcloud beta services identity create "
    "--service=bigquerydatatransfer.googleapis.com --project=<プロジェクトID>\n"
    "     2) gcloud iam service-accounts add-iam-policy-binding "
    "gdgoc-app@<プロジェクトID>.iam.gserviceaccount.com "
    "--member=serviceAccount:service-<プロジェクト番号>"
    "@gcp-sa-bigquerydatatransfer.iam.gserviceaccount.com "
    "--role=roles/iam.serviceAccountTokenCreator"
)


class ScheduleError(RuntimeError):
    """スケジュールクエリの登録や確認に失敗した。"""


def render_query(*, project: str, location: str) -> str:
    """`05_mart.sql` を読み、`{{PROJECT}}`・`{{LOCATION}}` を置き換える（run_sql と同じ規則）。"""
    return run_sql.render(SQL_PATH.read_text(encoding="utf-8"), project=project, location=location)


def build_config(query: str) -> dict[str, Any]:
    """登録する内容（スケジュールクエリ1本）。

    `destination_dataset_id` は入れない。`CREATE OR REPLACE TABLE` を並べた複数文の SQL は、
    書き込む先を SQL 自身が持っているため（入れると「宛先の表が要る」と断られる）。
    """
    return {
        "displayName": DISPLAY_NAME,
        "dataSourceId": DATA_SOURCE_ID,
        "schedule": SCHEDULE,
        "params": {"query": query},
    }


def estimate_cost(billed_mib: float) -> dict[str, float]:
    """1回あたりの課金対象バイト（MiB）から、1か月の見積もりを出す。"""
    monthly_tib = billed_mib * RUNS_PER_MONTH / 1024 / 1024
    over_tib = max(monthly_tib - FREE_TIB_PER_MONTH, 0.0)
    return {
        "runs_per_month": float(RUNS_PER_MONTH),
        "monthly_tib": monthly_tib,
        "over_free_tib": over_tib,
        "usd_per_month": over_tib * USD_PER_TIB,
    }


def service_account(project: str, override: str | None) -> str:
    return override or SERVICE_ACCOUNT_TEMPLATE.format(project=project)


def _now_rfc3339() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- API の呼び出し ---


def _session() -> requests.Session:
    try:
        credentials, _ = google.auth.default(scopes=list(SCOPES))
        credentials.refresh(google.auth.transport.requests.Request())
    except Exception as exc:  # 認証していない・切れている
        raise ScheduleError(
            "gcloud の認証が見つかりません。"
            "`gcloud auth application-default login` を実行してください"
        ) from exc
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {credentials.token}"})
    return session


def _call(session: requests.Session, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    response = session.request(method, url, timeout=60, **kwargs)
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            detail = response.text
        if response.status_code == 403 and "actAs" in str(detail):
            detail = (
                f"{detail}\n"
                "サービスアカウントを使う権限（roles/iam.serviceAccountUser）が足りません。"
            )
        raise ScheduleError(f"{method} が {response.status_code} で失敗しました: {detail}")
    if not response.content:
        return {}
    return response.json()


def _parent(project: str, location: str) -> str:
    return f"{API_ROOT}/projects/{project}/locations/{location}"


def find_config(session: requests.Session, *, project: str, location: str) -> dict[str, Any] | None:
    """`DISPLAY_NAME` の登録を探す（なければ None）。"""
    data = _call(
        session,
        "GET",
        f"{_parent(project, location)}/transferConfigs",
        params={"dataSourceIds": DATA_SOURCE_ID},
    )
    for config in data.get("transferConfigs", []):
        if config.get("displayName") == DISPLAY_NAME:
            return config
    return None


def create_config(
    session: requests.Session, *, project: str, location: str, body: dict[str, Any], sa: str
) -> dict[str, Any]:
    return _call(
        session,
        "POST",
        f"{_parent(project, location)}/transferConfigs",
        params={"serviceAccountName": sa},
        json=body,
    )


def update_config(
    session: requests.Session, *, name: str, body: dict[str, Any], sa: str
) -> dict[str, Any]:
    """SQL と間隔を上書きする（`05_mart.sql` を直したときに使う）。

    updateMask は `params`（`params.query` ではない）。2026-11-10 に、`params.query` だと
    エラーにならずに更新の時刻だけが変わり、**SQL は古いまま残る**ことを踏んだ
    （登録された SQL を読み直して、古い 05_mart.sql と同じだった）。
    """
    return _call(
        session,
        "PATCH",
        f"{API_ROOT}/{name}",
        params={"updateMask": "params,schedule,disabled", "serviceAccountName": sa},
        json={**body, "disabled": False},
    )


def set_disabled(session: requests.Session, *, name: str, disabled: bool) -> dict[str, Any]:
    return _call(
        session,
        "PATCH",
        f"{API_ROOT}/{name}",
        params={"updateMask": "disabled"},
        json={"disabled": disabled},
    )


def delete_config(session: requests.Session, *, name: str) -> None:
    _call(session, "DELETE", f"{API_ROOT}/{name}")


def start_manual_run(session: requests.Session, *, name: str) -> dict[str, Any]:
    """いますぐ1回走らせる。"""
    return _call(
        session,
        "POST",
        f"{API_ROOT}/{name}:startManualRuns",
        json={"requestedRunTime": _now_rfc3339()},
    )


def latest_runs(session: requests.Session, *, name: str, count: int = 3) -> list[dict[str, Any]]:
    data = _call(session, "GET", f"{API_ROOT}/{name}/runs", params={"pageSize": count})
    return data.get("transferRuns", [])


# --- 画面への出力 ---


def print_estimate(billed_mib: float) -> None:
    estimate = estimate_cost(billed_mib)
    print("=== 料金の見積もり（1時間ごとに走らせたとき）===")
    print(f"1回あたりの課金対象 : {billed_mib:.1f} MiB")
    print(f"1か月の回数         : {int(estimate['runs_per_month'])} 回")
    print(f"1か月の課金対象     : {estimate['monthly_tib']:.3f} TiB")
    print(f"無料枠（1TiB）の超過: {estimate['over_free_tib']:.3f} TiB")
    print(f"超過分の概算        : ${estimate['usd_per_month']:.2f} / 月")
    if estimate["over_free_tib"] <= 0:
        print("→ 無料枠（毎月1TiB）に収まる見込みです。")
    else:
        print("→ 無料枠を超えます。間隔を広げるか、デモの前後だけ動かしてください。")
    print()


def print_config(config: dict[str, Any], runs: list[dict[str, Any]]) -> None:
    # 一覧の応答には serviceAccountName が入らないことがある（登録の応答には入る）
    owner = (
        config.get("serviceAccountName")
        or config.get("ownerInfo", {}).get("email")
        or "(応答に含まれません)"
    )
    print("=== いまの登録 ===")
    print(f"名前       : {config.get('displayName')}")
    print(f"間隔       : {config.get('schedule')}")
    print(f"止めている : {'はい' if config.get('disabled') else 'いいえ'}")
    print(f"実行する人 : {owner}")
    print(f"次に走る   : {config.get('nextRunTime') or '(未定)'}")
    print(f"識別子     : {config.get('name')}")
    if runs:
        print("--- 直近の実行 ---")
        for index, run in enumerate(runs):
            message = run.get("errorStatus", {}).get("message", "")
            print(f"- {run.get('runTime')} : {run.get('state')} {message}".rstrip())
            # ヒントは**一番新しい実行が落ちているときだけ**出す。直したあとも古い失敗に
            # 出し続けると、直っていないように見える
            if index == 0 and "transfer user is not valid" in message:
                print(TRANSFER_USER_HINT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="05_mart.sql をスケジュールクエリに登録する")
    parser.add_argument("--dry-run", action="store_true", help="登録せず、送る内容と見積もりを表示")
    parser.add_argument("--show", action="store_true", help="いまの登録と直近の実行を表示")
    parser.add_argument("--run-now", action="store_true", help="1回だけ手で走らせる")
    parser.add_argument("--disable", action="store_true", help="止める（登録は残す）")
    parser.add_argument("--enable", action="store_true", help="止めたものを動かす")
    parser.add_argument("--delete", action="store_true", help="登録を消す")
    parser.add_argument("--service-account", default=None, help="実行するサービスアカウント")
    parser.add_argument(
        "--billed-mib",
        type=float,
        # 2026-11-08 の実測（`run_sql.py 03_sql/05_mart.sql` が 12.4秒・220.0 MiB）。
        default=220.0,
        help="1回あたりの課金対象（MiB）。run_sql.py の出力の値を入れる",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    project = settings.project_id
    location = settings.bq_location
    sa = service_account(project, args.service_account)

    print("=== 設定 ===")
    print(f"プロジェクト : {project or '(未設定)'}")
    print(f"場所         : {location}")
    print(f"SQL          : 03_sql/{SQL_PATH.name}")
    print(f"間隔         : {SCHEDULE}")
    print(f"実行する人   : {sa}")
    print()

    try:
        query = render_query(project=project, location=location)
    except (ValueError, run_sql.UnknownPlaceholder) as exc:
        print(f"NG: {exc}")
        return 1

    body = build_config(query)

    if args.dry_run:
        print_estimate(args.billed_mib)
        print("=== 送る内容（SQL は先頭10行だけ表示）===")
        shown = {**body, "params": {"query": "\n".join(query.splitlines()[:10]) + "\n…"}}
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        print("\n登録はしていません。登録するときは --dry-run を外してください。")
        return 0

    try:
        session = _session()
        config = find_config(session, project=project, location=location)

        if args.delete:
            if not config:
                print("消す登録がありません。")
                return 0
            delete_config(session, name=str(config.get("name")))
            print("登録を消しました。")
            return 0

        if args.disable or args.enable:
            if not config:
                print("NG: 登録がありません。先に登録してください。")
                return 1
            config = set_disabled(session, name=str(config.get("name")), disabled=bool(args.disable))
            print("止めました（料金はかかりません）。" if args.disable else "動かしました。")
            print_config(config, [])
            return 0

        if args.show:
            if not config:
                print("まだ登録されていません。")
                return 0
            print_config(config, latest_runs(session, name=str(config.get("name"))))
            return 0

        if args.run_now:
            if not config:
                print("NG: 登録がありません。先に登録してください。")
                return 1
            start_manual_run(session, name=str(config.get("name")))
            print("1回だけ走らせました。数分後に --show で結果を確かめてください。")
            return 0

        if config:
            config = update_config(session, name=str(config.get("name")), body=body, sa=sa)
            print("すでにある登録の SQL と間隔を上書きしました。")
        else:
            config = create_config(session, project=project, location=location, body=body, sa=sa)
            print("登録しました。")
        print_config(config, [])
        print()
        print_estimate(args.billed_mib)
        print("デモが終わったら `--disable` で止めてください（放っておくと走り続けます）。")
        return 0
    except ScheduleError as exc:
        print(f"NG: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
