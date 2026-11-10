"""スケジュールクエリの上書き（04_scripts/schedule_mart.py）。

2026-11-10、updateMask を `params.query` にしていたため、エラーにならずに更新の時刻だけが
変わり、登録された SQL は古いまま残っていた。`params` で上書きすることを固定する。
"""

from __future__ import annotations

import schedule_mart


def test_SQLを上書きするときはparamsごと送る(monkeypatch):
    calls = []

    def fake_call(session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {}

    monkeypatch.setattr(schedule_mart, "_call", fake_call)
    schedule_mart.update_config(None, name="projects/p/locations/l/transferConfigs/x", body={"params": {"query": "SELECT 1"}}, sa="sa@example.com")
    method, _, kwargs = calls[0]
    assert method == "PATCH"
    mask = kwargs["params"]["updateMask"].split(",")
    assert "params" in mask
    assert "params.query" not in mask
    assert kwargs["json"]["params"]["query"] == "SELECT 1"
