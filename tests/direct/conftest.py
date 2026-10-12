"""Shared helpers for direct-mode tests.

IMPORTANT - what direct mode does and does not test:
Direct mode executes ONLY the leader side of `gl.vm.run_nondet_unsafe`.
The comparative validator (independent fetch + LLM re-run and the EXACT
label/evidence-status match in `FreelanceEscrow._arbitrate`) is NOT exercised
here. Validator/consensus behavior is covered by tests/integration/ instead.
"""

import calendar
import datetime as dt
import json

import pytest

CONTRACT = "contracts/freelance_escrow.py"
AMOUNT = 5 * 10**18  # 5 tokens, atto scale (value * 10**18)
WINDOW = 24 * 60 * 60  # 1 day, the delivery window used by most fixtures


def iso(ts: int) -> str:
    """Render a unix timestamp the way the consensus message datetime does
    (UTC ISO-8601 with a Z suffix) so tests can warp the VM clock."""
    return (dt.datetime.fromtimestamp(ts, dt.timezone.utc)
            .isoformat().replace("+00:00", "Z"))


def epoch_of(iso_str: str) -> int:
    """Unix seconds of a message-datetime string as stored by the contract
    (funded_at etc.) - the SAME wall the contract's _now_ts reads."""
    return calendar.timegm(
        dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00")).utctimetuple()
    )


def warp_to(direct_vm, ts: int):
    """Move the VM clock to exactly unix timestamp `ts`."""
    direct_vm.warp(iso(ts))


def set_clock(direct_vm, ts: int) -> None:
    """Move the CONTRACT-visible clock to exactly unix timestamp `ts`.

    The direct-mode runner captures the message dict once at contract module
    import (fd0 injection) and vm.warp() afterwards does not refresh
    `gl.message_raw`, so post-deploy time control must patch the loaded
    module's copy directly. This mirrors what the real network does per
    transaction (a consensus-signed message datetime the contract reads);
    the live studionet integration test covers that real path.
    """
    direct_vm.warp(iso(ts))  # keeps VM-side bookkeeping consistent
    import sys
    for name, mod in list(sys.modules.items()):
        if name.startswith("_contract_"):
            gl_obj = getattr(mod, "gl", None)
            raw = getattr(gl_obj, "message_raw", None)
            if isinstance(raw, dict) and "datetime" in raw:
                raw["datetime"] = iso(ts)


def warp_past(direct_vm, deadline_ts: int, seconds: int = 1):
    """Move the VM clock strictly past `deadline_ts` (reclaim condition)."""
    warp_to(direct_vm, deadline_ts + seconds)


def assert_accounting(contract):
    """Escrow conservation invariant must hold after EVERY test step."""
    acc = contract.get_accounting()
    assert acc["invariant_holds"], f"accounting invariant broken: {acc}"
    assert acc["escrow_pool_atto"] == (
        acc["unwithdrawn_credits_atto"] + acc["locked_escrow_atto"]
    )
    return acc


def mock_judgment(direct_vm, decision: str, analysis: str = "mocked"):
    """Mock the arbitration LLM to return one of the fixed settlement labels.

    NOTE: no percent is sent - the payout percent is DERIVED from the label
    by the contract; LLM-supplied numbers are ignored (see the dedicated
    test that proves this).
    """
    direct_vm.clear_mocks()
    direct_vm.mock_llm(
        r".*arbitration expert.*",
        json.dumps({"decision": decision, "analysis": analysis}),
    )


def mock_judgment_raw(direct_vm, payload):
    """Mock the arbitration LLM with a raw payload (string / malformed etc.)."""
    direct_vm.clear_mocks()
    body = payload if isinstance(payload, str) else json.dumps(payload)
    direct_vm.mock_llm(r".*arbitration expert.*", body)


def mock_delivery_fetch(direct_vm, url_pattern: str, body: str, status: int = 200):
    """Mock the evaluator's OWN web fetch of a delivered artifact URL.

    Call AFTER mock_judgment / mock_judgment_raw: those start with
    clear_mocks() and would otherwise wipe this mock. A 200 response is
    required for arbitration tests - without one the contract's
    refund-by-default path (unreachable) fires before the LLM.
    """
    direct_vm.mock_web(url_pattern, {"status": status, "body": body})


@pytest.fixture
def escrow(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """Deploy the contract and return (contract, client, freelancer, stranger)."""
    contract = direct_deploy(CONTRACT)
    return contract, direct_alice, direct_bob, direct_charlie


@pytest.fixture
def funded_agreement(escrow, direct_vm):
    """Contract with agreement 'a1' created and funded, ready for delivery."""
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    direct_vm.value = 0
    contract.create_agreement(
        "a1", freelancer, "Landing page",
        "Build a 3-section landing page with a contact form.",
        AMOUNT, WINDOW,
    )
    direct_vm.value = AMOUNT
    contract.fund_escrow("a1")
    direct_vm.value = 0
    return contract, client, freelancer


@pytest.fixture
def delivered_agreement(funded_agreement, direct_vm):
    """Contract with agreement 'a1' delivered WITH a fetchable URL - the only
    delivery form the fail-closed contract allows to reach LLM arbitration
    (tests running arbitration must still mock the fetch itself)."""
    contract, client, freelancer = funded_agreement
    direct_vm.sender = freelancer
    contract.submit_delivery(
        "a1",
        "https://delivered.example/a1 - all three sections and the contact "
        "form are live on this page.",
    )
    return contract, client, freelancer
