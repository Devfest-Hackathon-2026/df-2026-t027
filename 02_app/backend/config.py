"""設定を1か所にまとめる（開発手順書 Step 1-1）。

`.env` はリポジトリ直下に置く。値はここ以外から直接 os.environ で読まない。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# このファイルは 02_app/backend/config.py にあるので、リポジトリ直下は3つ上
REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "02_app" / "frontend"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

load_dotenv(REPO_ROOT / ".env")

# アプリの名前は仮称（開発手順書9章の要確認1）。変えるときはこの1か所だけ直す。
APP_NAME = "被災者支援つなぎ記録"


@dataclass(frozen=True)
class Settings:
    """アプリ全体の設定。"""

    app_name: str
    project_id: str
    bq_location: str
    gemini_api_key: str
    gemini_model: str
    firebase_api_key: str
    firebase_auth_domain: str
    firebase_app_id: str

    # 公開URLを1つにそろえる（要望5。Cloud Runは1サービスに2つのURLを必ず発行する。
    # ログインの状態がURLごとに別に保存されるため、もう一方で開かれたらこちらへ移す）。
    # 空なら移さない（ローカルや、まだ決めていないデプロイ先ではこの仕組みを使わない）。
    canonical_host: str = ""

    # BigQuery のデータセット（要件定義書10-4）
    dataset_raw: str = "gdgoc_raw"
    dataset_staging: str = "gdgoc_staging"
    dataset_mart: str = "gdgoc_mart"

    # Gemini の打ち切りとやり直し（要件定義書10-2の4）
    gemini_timeout_seconds: int = 30
    gemini_max_retries: int = 2

    @property
    def is_configured(self) -> bool:
        """Google Cloud と Gemini を呼ぶのに最低限必要な値がそろっているか。"""
        return bool(self.project_id and self.gemini_api_key and self.gemini_model)

    def missing_keys(self) -> list[str]:
        """未設定の項目の一覧（画面やスクリプトの案内に使う）。"""
        required = {
            "GOOGLE_CLOUD_PROJECT": self.project_id,
            "GEMINI_API_KEY": self.gemini_api_key,
            "GEMINI_MODEL": self.gemini_model,
            "FIREBASE_API_KEY": self.firebase_api_key,
            "FIREBASE_AUTH_DOMAIN": self.firebase_auth_domain,
            "FIREBASE_APP_ID": self.firebase_app_id,
        }
        return [name for name, value in required.items() if not value]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """設定を読み込む（プロセスで1回だけ）。"""
    return Settings(
        app_name=APP_NAME,
        project_id=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        bq_location=os.getenv("BQ_LOCATION", "asia-northeast1"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", ""),
        firebase_api_key=os.getenv("FIREBASE_API_KEY", ""),
        firebase_auth_domain=os.getenv("FIREBASE_AUTH_DOMAIN", ""),
        firebase_app_id=os.getenv("FIREBASE_APP_ID", ""),
        canonical_host=os.getenv("CANONICAL_HOST", ""),
    )
