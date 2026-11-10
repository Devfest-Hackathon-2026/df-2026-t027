"""処理中の表示・エラーの表示・二重押しの防止（開発手順書 Step 13-2、要件定義書11-2）。

幅や見た目は自動テストで測れないので、**作りのほうを固定する**。

- 書き込みの API 呼び出しが、すべて `withBusy` の中から呼ばれていること
  （押している間はボタンを押せなくする＝二重に送られない）。
- `withBusy` が「処理中…」を出し、**終わったら必ず元に戻す**こと。
- どのフォームにも、誤りを出す置き場（`.form-error`）があること。
- 次に押すボタンを目立たせる指定（`.button-primary`）と、押せないボタンの見た目があること。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "02_app" / "frontend"

# 書き込みの API 呼び出し（apiFetch に method を渡しているところ）
WRITE_PATTERN = re.compile(r'method:\s*"(POST|PUT|PATCH|DELETE)"')
FUNCTION_PATTERN = re.compile(r"(?:export\s+)?(?:async\s+)?function\s+(\w+)")

# 直接 withBusy の中に書かれている場合に、さかのぼって探す行数
INLINE_WINDOW = 20
# 関数の呼び出し側で包んでいる場合に、さかのぼって探す行数
CALLER_WINDOW = 3


def js_files() -> list[Path]:
    return sorted(FRONTEND.glob("*.js"))


def enclosing_function(lines: list[str], index: int) -> tuple[str | None, int]:
    """その行を含む関数の名前と、定義のある行（0始まり）を返す。"""
    for i in range(index, -1, -1):
        match = FUNCTION_PATTERN.match(lines[i])
        if match:
            return match.group(1), i
    return None, -1


def unwrapped_writes() -> list[str]:
    """`withBusy` の外から呼ばれている書き込みを探す。"""
    problems: list[str] = []
    for path in js_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not WRITE_PATTERN.search(line):
                continue
            # (1) その場で withBusy に包まれている
            if "withBusy" in "\n".join(lines[max(0, index - INLINE_WINDOW) : index + 1]):
                continue
            # (2) 包んでいるのは呼び出し側（関数に切り出されている）
            name, start = enclosing_function(lines, index)
            if name is None:
                problems.append(f"{path.name}:{index + 1} 関数の外で書き込んでいます")
                continue
            callers = [
                i
                for i, text in enumerate(lines)
                if i != start and re.search(rf"\b{name}\(", text)
            ]
            if not callers:
                problems.append(f"{path.name}:{index + 1} {name}() を呼んでいるところがありません")
                continue
            for i in callers:
                window = "\n".join(lines[max(0, i - CALLER_WINDOW) : i + 1])
                if "withBusy" not in window:
                    problems.append(
                        f"{path.name}:{i + 1} {name}() が withBusy の外から呼ばれています"
                    )
    return problems


def test_書き込みはすべてwithBusyの中から呼ばれる():
    """ここが落ちたら、そのボタンは二重に押せる（同じ記録が2つ入る）。"""
    problems = unwrapped_writes()
    assert not problems, "二重押しを防いでいない書き込みがあります:\n" + "\n".join(problems)


def test_走査が空回りしていない():
    """書き込みを1つも見つけられていないと、上のテストが通ってしまう。"""
    found = sum(
        1
        for path in js_files()
        for line in path.read_text(encoding="utf-8").splitlines()
        if WRITE_PATTERN.search(line)
    )
    assert found >= 20, f"書き込みの呼び出しを {found} 件しか見つけられていません"


def test_withBusyは処理中を出して必ず元に戻す():
    source = (FRONTEND / "ui.js").read_text(encoding="utf-8")
    match = re.search(r"export async function withBusy\(.*?\n\}", source, re.DOTALL)
    assert match, "withBusy が見つかりません"
    body = match.group(0)
    assert "button.disabled = true" in body, "押している間に押せなくしていません"
    assert "処理中" in body, "処理中の表示が出ません"
    # 誤りで終わってもボタンが戻るように、finally で戻す
    assert "finally" in body, "finally で戻さないと、誤りのあとボタンが押せないままになります"
    assert body.index("finally") < body.index("button.disabled = false")


def test_どのフォームにも誤りの置き場がある():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    forms = html.count("<form")
    errors = html.count("form-error")
    assert forms > 0
    assert errors >= forms, f"フォーム {forms} 個に対して誤りの置き場が {errors} 個しかありません"


# JS で作るボタンのうち、その画面で「次に押すもの」は主ボタンにする（11-2の2-b）。
# 一覧の各行に付くボタン（施設の空きの更新・申請の状況の更新）は、行の数だけ並ぶので
# 主ボタンにしない（全部が目立つと、どれを押すか分からなくなる）。
DYNAMIC_PRIMARY = {
    "f1.js": ["この方を担当にする"],
    "f2.js": ["この案を反映する"],
    "f5.js": ["この施設を選ぶ"],
    "f8.js": ["返事を送る"],
}


def test_画面ごとの次に押すボタンは主ボタンになっている():
    problems = []
    for name, labels in DYNAMIC_PRIMARY.items():
        lines = (FRONTEND / name).read_text(encoding="utf-8").splitlines()
        for label in labels:
            hits = [line for line in lines if f'"{label}"' in line]
            if not hits:
                problems.append(f"{name}: 「{label}」が見つかりません（文言を変えたら表も直す）")
                continue
            if not any("button button-primary" in line for line in hits):
                problems.append(f"{name}: 「{label}」が主ボタンになっていません")
    assert not problems, f"次に押すボタンの指定に漏れがあります: {problems}"


def test_次に押すボタンを目立たせる指定がある():
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    assert ".button-primary" in css, "次に押すボタンを目立たせる指定がありません（11-2の2-b）"
    assert ".button:disabled" in css, "押せないボタンの見た目がありません（処理中が分からない）"
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert html.count("button-primary") >= 10, "主なボタンに button-primary が付いていません"
