"""Shared helpers for direct-mode tests.

IMPORTANT — what direct mode does and does not test:
Direct mode executes ONLY the leader side of `gl.vm.run_nondet_unsafe`.
The comparative validator (independent re-run + decision-field comparison in
`FreelanceEscrow._arbitrate`) is NOT exercised here. Validator/consensus
behavior is covered by tests/integration/ instead.
"""

import json

import pytest

CONTRACT = "contracts/freelance_escrow.py"
AMOUNT = 5 * 10**18  # 5 tokens, atto scale (value * 10**18)


def mock_judgment(direct_vm, decision: str, percent: int, analysis: str = "mocked"):
    """Mock the arbitration LLM to return the given judgment."""
    direct_vm.clear_mocks()
    direct_vm.mock_llm(
        r".*arbitration expert.*",
        json.dumps({
            "decision": decision,
            "freelancer_percent": percent,
            "analysis": analysis,
        }),
    )


def mock_judgment_raw(direct_vm, payload):
    """Mock the arbitration LLM with a raw payload (string / malformed etc.)."""
    direct_vm.clear_mocks()
    body = payload if isinstance(payload, str) else json.dumps(payload)
    direct_vm.mock_llm(r".*arbitration expert.*", body)


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
        "Build a 3-section landing page with a contact form. Deliver as a URL.",
        AMOUNT,
    )
    direct_vm.value = AMOUNT
    contract.fund_escrow("a1")
    direct_vm.value = 0
    return contract, client, freelancer


@pytest.fixture
def delivered_agreement(funded_agreement, direct_vm):
    """Contract with agreement 'a1' delivered by the freelancer."""
    contract, client, freelancer = funded_agreement
    direct_vm.sender = freelancer
    contract.submit_delivery("a1", "https://example.com/work — 3 sections, contact form included")
    return contract, client, freelancer
