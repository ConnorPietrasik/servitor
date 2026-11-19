"""Probe endpoint — unknown ``which`` values are 400. There are no
legacy clients to degrade for: the only path that reports
``{"result": null, "busy": ...}`` is the first-touch refusal (fresh
session, no transcript yet), never a malformed request."""
from __future__ import annotations


def test_probe_unknown_which_is_400(api):
    res = api.create_session("probe-gate")
    sid = res["session_id"]
    try:
        for which in ("tree", "fork-messages", "bogus"):
            r = api.get(f"/api/v1/sessions/{sid}/probe?which={which}")
            assert r.status_code == 400, (which, r.status_code, r.text)
        # a known kind is still accepted: fresh session has no
        # transcript, so the first-touch rule refuses — null result,
        # busy state, HTTP 200
        r = api.get(f"/api/v1/sessions/{sid}/probe?which=messages")
        assert r.status_code == 200, r.text
        assert r.json()["result"] is None
    finally:
        api.release(sid)
