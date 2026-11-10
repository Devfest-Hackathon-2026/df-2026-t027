"""ログイン中継のURLをログに残さない（使いやすさの改修の計画.md 14章）。

httpx は送った先のURLを丸ごと INFO で出す。Google ログインの中継では、その中に
1回だけ使える確認用のコード（code・state）が入っていた。
"""

from __future__ import annotations

import logging

from backend import request_log


def test_httpxのINFOのログは出ない():
    logging.getLogger("httpx").setLevel(logging.INFO)
    request_log.setup_logging()
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        assert not logger.isEnabledFor(logging.INFO), name
        assert logger.isEnabledFor(logging.WARNING), name


def test_mainを読み込むとhttpxのINFOが止まっている():
    import backend.main  # noqa: F401  読み込むだけで setup_logging が走る

    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
