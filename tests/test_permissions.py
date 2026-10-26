"""Approval broker tests: the human gate for external actions."""

from __future__ import annotations

import threading

from jobpilot.core.models import PermissionTier
from jobpilot.harness.permissions import ApprovalBroker, PermissionManager


def test_unknown_tools_fail_closed():
    manager = PermissionManager()
    assert manager.tier_for("made_up_tool") is PermissionTier.external
    assert manager.requires_approval("mark_submitted") is True
    assert manager.requires_approval("rank_jobs") is False


def test_approval_can_be_approved(clean_database):
    broker = ApprovalBroker(clean_database, timeout_seconds=2)
    request = broker.create(
        run_id="run_test",
        session_id="ses_test",
        tool="mark_submitted",
        arguments={"application_id": "app_test"},
    )
    assert request.status == "pending"
    assert broker.pending("ses_test")[0].id == request.id

    timer = threading.Timer(0.05, lambda: broker.resolve(request.id, True))
    timer.start()
    try:
        assert broker.wait(request.id, timeout=2) is True
    finally:
        timer.cancel()
    assert broker.get(request.id).status == "approved"
    assert broker.pending("ses_test") == []


def test_approval_can_be_denied(clean_database):
    broker = ApprovalBroker(clean_database, timeout_seconds=2)
    request = broker.create(
        run_id="run_test",
        session_id="ses_test",
        tool="mark_submitted",
        arguments={},
    )
    threading.Timer(0.05, lambda: broker.resolve(request.id, False)).start()
    assert broker.wait(request.id, timeout=2) is False
    assert broker.get(request.id).status == "denied"
