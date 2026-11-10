"""参加コード・確認コードのハッシュ（要件定義書10-3、開発手順書 Step 3-1）。

コードは**ハッシュにして保存し、元の値は保存しない**。照合はこのモジュールだけで行う。

- 方式は**ソルト付き SHA-256**。ソルトには、そのコードが属するIDを使う
  （参加コードは組織のID、確認コードは被災者のID）。
  同じコードでも組織ごとに別のハッシュになるため、1つのハッシュが漏れても他に使い回せない。
- 保存する文字列は `sha256$<16進>` の形にして、**方式が後から分かる**ようにしておく
  （方式を変えるときに、古い行を見分けられるようにするため）。
- 照合は `secrets.compare_digest` で行う（比較にかかる時間からコードを推測されないため）。
- 入力は NFKC で正規化し、空白と `-` を取り、大文字にそろえる
  （スマートフォンで全角や区切りの `-` が入ることがあるため）。

秘密の値を `.env` に増やしていない。理由：デプロイ時の環境変数の設定漏れで本番だけ
参加コードが通らなくなる事故を避けるため（デプロイは 11/10 に1度だけ行う）。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata

# コードに使う文字。紛らわしい `0`・`O`・`1`・`I` は入れない（紙に書いて口頭で伝えるため）。
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

# 発行するコードの長さ（32文字から8文字 ≒ 40ビット）。
CODE_LENGTH = 8

# 受け付けるコードの最小の長さ。これより短いものは総当たりで当てられるため拒む。
MIN_CODE_LENGTH = 6

# 正規化したあとのコードに許す文字（英大文字と数字だけ）。
NORMALIZED_PATTERN = re.compile(r"^[A-Z0-9]+$")

# 正規化のときに取り除く文字（区切りとして入れられることがある）。
_STRIPPED = str.maketrans({"-": None, " ": None, "\t": None, "_": None})

# 保存する文字列の頭に付ける、方式を表す印。
HASH_SCHEME = "sha256"
HASH_PREFIX = f"{HASH_SCHEME}$"


class InvalidCode(ValueError):
    """コードの形が正しくない（短すぎる・使えない文字が入っている・空）。"""


def normalize_code(code: str) -> str:
    """入力されたコードを、照合できる形にそろえる。

    全角を半角にし、区切りの `-`・空白・`_` を取り、大文字にする。
    形が正しくないときは `InvalidCode`。
    """
    if not isinstance(code, str):
        raise InvalidCode("コードが文字列ではありません")
    normalized = unicodedata.normalize("NFKC", code).translate(_STRIPPED).strip().upper()
    if not normalized:
        raise InvalidCode("コードが空です")
    if len(normalized) < MIN_CODE_LENGTH:
        raise InvalidCode(f"コードが短すぎます（{MIN_CODE_LENGTH}文字以上にしてください）")
    if not NORMALIZED_PATTERN.match(normalized):
        raise InvalidCode("コードに使えない文字が含まれています（英数字だけにしてください）")
    return normalized


def hash_code(code: str, *, salt: str) -> str:
    """コードを `sha256$<16進>` の形にする。`salt` は組織のIDか被災者のID。"""
    if not isinstance(salt, str) or not salt.strip():
        raise InvalidCode("ソルト（組織のIDなど）が空です")
    normalized = normalize_code(code)
    digest = hashlib.sha256(f"{salt}\x00{normalized}".encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def verify_code(code: str, *, salt: str, code_hash: str | None) -> bool:
    """入力されたコードが、保存されているハッシュと合うかを返す。

    画面から来た値をそのまま渡せるように、形が正しくないときも例外にせず `False` を返す。
    """
    if not code_hash or not isinstance(code_hash, str):
        return False
    try:
        candidate = hash_code(code, salt=salt)
    except InvalidCode:
        return False
    # bytes で比べる。`compare_digest` は非ASCIIの文字列を渡すと TypeError になるため
    # （壊れた値が保存されていたときに500にせず、合わないものとして扱う）。
    return secrets.compare_digest(candidate.encode("utf-8"), code_hash.encode("utf-8"))


def generate_code(length: int = CODE_LENGTH) -> str:
    """新しいコードを作る（`secrets` を使う。推測できる値にしない）。"""
    if length < MIN_CODE_LENGTH:
        raise InvalidCode(f"コードは{MIN_CODE_LENGTH}文字以上にしてください")
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
