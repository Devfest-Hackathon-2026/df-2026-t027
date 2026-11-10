"""AIのエージェント向けの決まりのファイルのテスト（追加修正の計画.md 10章）。

- CLAUDE.md は AGENTS.md を読み込むだけにする（決まりを2か所に書かない）
- GEMINI.md は作らない（Antigravity が AGENTS.md と両方を読み、決まりが二重になる）
- スキルの手順に書いた関数名が、gemini.py に今もあること（コードを変えたとき、手順が古くなったことに気づくため）
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / ".agents" / "skills" / "add-gemini-process" / "SKILL.md"
GEMINI_PY = REPO_ROOT / "02_app" / "backend" / "services" / "gemini.py"


def test_CLAUDEmdはAGENTSmdを読み込むだけ():
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip()
    assert text == "@AGENTS.md"


def test_GEMINImdは作らない():
    assert not (REPO_ROOT / "GEMINI.md").exists()


def test_AGENTSmdにGeminiのAPIを移さない決まりがある():
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Interactions API へ移さない" in text
    assert "temperature・thinking_budget を外さない" in text
    assert ".agents/skills/add-gemini-process/SKILL.md" in text


def test_スキルの先頭にnameとdescriptionがある():
    lines = SKILL.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    header = lines[1:end]
    assert "name: add-gemini-process" in header
    assert any(line.startswith("description: ") for line in header)


def test_スキルに書いた関数名がgeminipyにある():
    skill = SKILL.read_text(encoding="utf-8")
    code = GEMINI_PY.read_text(encoding="utf-8")
    for name in ("generate_structured", "load_prompt", "GeminiUnavailable"):
        assert name in skill
        assert f"def {name}" in code or f"class {name}" in code
    for arg in ("schema", "contents", "system_instruction", "operation", "thinking_budget", "temperature"):
        assert f"    {arg}:" in code, arg
