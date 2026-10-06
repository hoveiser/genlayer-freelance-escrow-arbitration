"""Direct-mode unit tests for the Freelance Escrow contract.

NOTE: Direct mode runs the LEADER path only — the comparative validator logic
inside `_arbitrate` (independent URL fetch + LLM re-run, EXACT label/evidence
match with NO tolerance, error → disagreement → leader rotation) is NOT
exercised here. See tests/integration/ for consensus-level coverage.
"""

import json

from conftest import AMOUNT, mock_delivery_fetch, mock_judgment, mock_judgment_raw


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_create_and_read_back(escrow, direct_vm):
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "Logo design", "A vector logo, 3 revisions", AMOUNT)

    ag = contract.get_agreement("a1")
    assert ag["status"] == "created"
    assert ag["escrow_atto"] == AMOUNT
    assert ag["judgment_decision"] == "none"
    assert ag["appeal_count"] == 0
    assert contract.list_agreements() == ["a1"]


def test_happy_path_no_dispute_full_release(escrow, direct_vm):
    """create -> fund -> deliver -> client approves -> freelancer withdraws all."""
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "Logo design", "A vector logo", AMOUNT)
    direct_vm.value = AMOUNT
    contract.fund_escrow("a1")
    assert contract.get_agreement("a1")["status"] == "funded"
    assert contract.get_escrow_pool() == AMOUNT

    direct_vm.sender = freelancer
    contract.submit_delivery("a1", "https://example.com/logo.svg")
    assert contract.get_agreement("a1")["status"] == "delivered"
    assert contract.get_agreement("a1")["delivery_url"] == "https://example.com/logo.svg"

    direct_vm.value = 0
    direct_vm.sender = client
    contract.approve_delivery("a1")
    ag = contract.get_agreement("a1")
    assert ag["status"] == "released"
    assert ag["judgment_decision"] == "RELEASE_FULL"
    assert ag["judgment_percent"] == 100

    assert contract.get_withdrawable(freelancer) == AMOUNT
    assert contract.get_withdrawable(client) == 0

    direct_vm.sender = freelancer
    contract.withdraw()
    assert contract.get_withdrawable(freelancer) == 0
    assert contract.get_escrow_pool() == 0


def test_dispute_full_release(escrow, direct_vm):
    """Arbitration (leader side) decides the work fully fulfills the spec."""
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "Client suspects the files are placeholders")

    mock_judgment(direct_vm, "RELEASE_FULL", "Work matches the spec on all points")
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "hero features contact-form")
    direct_vm.sender = freelancer
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["status"] == "arbitrated"
    assert ag["judgment_decision"] == "RELEASE_FULL"
    assert ag["judgment_percent"] == 100

    contract.accept_resolution("a1")  # freelancer accepts
    assert contract.get_withdrawable(freelancer) == AMOUNT
    assert contract.get_withdrawable(client) == 0


def test_dispute_full_refund(escrow, direct_vm):
    """Arbitration decides the work fails the spec: full refund to the client."""
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "Delivered file does not exist")

    mock_judgment(direct_vm, "REFUND_FULL", "Deliverable does not satisfy the spec")
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "broken placeholder page")
    direct_vm.sender = client
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "REFUND_FULL"
    assert ag["judgment_percent"] == 0

    contract.accept_resolution("a1")
    assert contract.get_agreement("a1")["status"] == "refunded"
    assert contract.get_withdrawable(client) == AMOUNT
    assert contract.get_withdrawable(freelancer) == 0


def test_dispute_split_half(escrow, direct_vm):
    """A fixed-set split label drives the exact payout — no free-form percent."""
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = freelancer
    contract.raise_dispute("a1", "Client is refusing to approve completed work")

    mock_judgment(direct_vm, "SPLIT_HALF", "Partial spec compliance")
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "two of three sections")
    direct_vm.sender = client
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "SPLIT_HALF"
    assert ag["judgment_percent"] == 50

    contract.accept_resolution("a1")
    assert contract.get_agreement("a1")["status"] == "settled"
    assert contract.get_withdrawable(freelancer) == AMOUNT // 2   # 50%
    assert contract.get_withdrawable(client) == AMOUNT // 2       # 50%

    direct_vm.sender = client
    contract.withdraw()
    assert contract.get_withdrawable(client) == 0
    assert contract.get_escrow_pool() == AMOUNT // 2


def test_dispute_split_quarter_payout(escrow, direct_vm):
    """SPLIT_QUARTER must credit exactly 25% / 75%."""
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "Most of the spec unmet")
    mock_judgment(direct_vm, "SPLIT_QUARTER")
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "one section only")
    direct_vm.sender = client
    contract.resolve_dispute("a1")
    contract.accept_resolution("a1")
    assert contract.get_withdrawable(freelancer) == AMOUNT // 4        # 25%
    assert contract.get_withdrawable(client) == 3 * AMOUNT // 4        # 75%


def test_dispute_split_three_quarter_payout(escrow, direct_vm):
    """SPLIT_THREE_QUARTER must credit exactly 75% / 25%."""
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "Minor gaps only")
    mock_judgment(direct_vm, "SPLIT_THREE_QUARTER")
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "nearly complete")
    direct_vm.sender = client
    contract.resolve_dispute("a1")
    contract.accept_resolution("a1")
    assert contract.get_withdrawable(freelancer) == 3 * AMOUNT // 4    # 75%
    assert contract.get_withdrawable(client) == AMOUNT // 4            # 25%


# ---------------------------------------------------------------------------
# Appeal path (max 1 re-trigger of resolve_dispute)
# ---------------------------------------------------------------------------

def test_appeal_path_single_retrigger(escrow, direct_vm):
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "Missing the contact form required by the spec")

    # First arbitration: refund.
    mock_judgment(direct_vm, "REFUND_FULL")
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "incomplete page")
    direct_vm.sender = freelancer
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["appeal_count"] == 0
    assert ag["judgment_decision"] == "REFUND_FULL"
    assert len(ag["arbitration_history"]) == 1

    # Appeal: freelancer re-triggers resolve_dispute; second judgment wins.
    mock_judgment(direct_vm, "SPLIT_HALF", "Contact form present but broken layout")
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "form present, layout broken")
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["appeal_count"] == 1
    assert ag["judgment_decision"] == "SPLIT_HALF"
    assert ag["judgment_percent"] == 50
    assert ag["status"] == "arbitrated"
    assert len(ag["arbitration_history"]) == 2  # full appeal trail retained
    assert ag["arbitration_nonce"] == 2

    # A second appeal is rejected — settlement becomes final.
    with direct_vm.expect_revert("appeal limit reached"):
        contract.resolve_dispute("a1")

    # Settling uses the appealed judgment.
    direct_vm.sender = client
    contract.accept_resolution("a1")
    assert contract.get_withdrawable(freelancer) == AMOUNT // 2
    assert contract.get_withdrawable(client) == AMOUNT // 2


def test_cannot_appeal_after_settlement(escrow, direct_vm):
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "not done")
    mock_judgment(direct_vm, "REFUND_FULL")
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "nothing works")
    contract.resolve_dispute("a1")
    contract.accept_resolution("a1")
    with direct_vm.expect_revert("nothing to arbitrate"):
        contract.resolve_dispute("a1")


# ---------------------------------------------------------------------------
# Unauthorized-sender rejections
# ---------------------------------------------------------------------------

def test_only_client_can_fund(escrow, direct_vm):
    contract, client, freelancer, stranger = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "t", "spec", AMOUNT)
    direct_vm.sender = freelancer
    direct_vm.value = AMOUNT
    with direct_vm.expect_revert("only the client can fund"):
        contract.fund_escrow("a1")
    direct_vm.value = 0


def test_only_freelancer_can_deliver(funded_agreement, direct_vm):
    contract, client, freelancer = funded_agreement
    direct_vm.sender = client
    with direct_vm.expect_revert("only the freelancer can submit delivery"):
        contract.submit_delivery("a1", "client writing checks")


def test_only_parties_can_dispute_arbitrate_and_settle(delivered_agreement, direct_vm,
                                                       direct_charlie):
    contract, client, freelancer = delivered_agreement
    stranger = direct_charlie
    direct_vm.sender = stranger
    with direct_vm.expect_revert("only a party"):
        contract.raise_dispute("a1", "not my business")
    with direct_vm.expect_revert("only a party"):
        contract.resolve_dispute("a1")
    direct_vm.sender = client
    contract.raise_dispute("a1", "work incomplete")
    direct_vm.sender = stranger
    with direct_vm.expect_revert("only a party"):
        contract.resolve_dispute("a1")
    mock_judgment(direct_vm, "SPLIT_HALF")
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "half done")
    direct_vm.sender = freelancer
    contract.resolve_dispute("a1")
    direct_vm.sender = stranger
    with direct_vm.expect_revert("only a party"):
        contract.accept_resolution("a1")


def test_only_client_can_approve(delivered_agreement, direct_vm):
    contract, client, freelancer = delivered_agreement
    direct_vm.sender = freelancer
    with direct_vm.expect_revert("only the client can approve"):
        contract.approve_delivery("a1")


# ---------------------------------------------------------------------------
# State-machine and input validation
# ---------------------------------------------------------------------------

def test_duplicate_agreement_id_rejected(escrow, direct_vm):
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "t", "spec", AMOUNT)
    with direct_vm.expect_revert("already exists"):
        contract.create_agreement("a1", freelancer, "t2", "spec2", AMOUNT)


def test_underpayment_rejected(escrow, direct_vm):
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "t", "spec", AMOUNT)
    direct_vm.value = AMOUNT - 1
    with direct_vm.expect_revert("expected exactly"):
        contract.fund_escrow("a1")


def test_overpayment_rejected(escrow, direct_vm):
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "t", "spec", AMOUNT)
    direct_vm.value = AMOUNT + 1
    with direct_vm.expect_revert("overpayment"):
        contract.fund_escrow("a1")


def test_double_fund_rejected(funded_agreement, direct_vm):
    contract, client, _ = funded_agreement
    direct_vm.value = AMOUNT
    with direct_vm.expect_revert("cannot fund in status"):
        contract.fund_escrow("a1")
    direct_vm.value = 0


def test_cannot_arbitrate_before_dispute(funded_agreement, direct_vm):
    contract, client, freelancer = funded_agreement
    direct_vm.sender = client
    with direct_vm.expect_revert("nothing to arbitrate"):
        contract.resolve_dispute("a1")


def test_unknown_agreement_rejected(escrow, direct_vm):
    contract, client, _, _ = escrow
    direct_vm.sender = client
    with direct_vm.expect_revert("unknown agreement"):
        contract.fund_escrow("nope")


def test_empty_fields_rejected(escrow, direct_vm):
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    with direct_vm.expect_revert("spec must not be empty"):
        contract.create_agreement("a2", freelancer, "t", "", AMOUNT)
    with direct_vm.expect_revert("escrow_amount_atto must be positive"):
        contract.create_agreement("a3", freelancer, "t", "spec", 0)
    with direct_vm.expect_revert("client and freelancer must differ"):
        contract.create_agreement("a4", client, "t", "spec", AMOUNT)


def test_nothing_to_withdraw(escrow, direct_vm):
    contract, client, _, _ = escrow
    direct_vm.sender = client
    with direct_vm.expect_revert("nothing to withdraw"):
        contract.withdraw()


# ---------------------------------------------------------------------------
# LLM output resilience (leader-side parsing; validator comparison itself is
# only covered by integration tests)
# ---------------------------------------------------------------------------

def test_judgment_key_aliasing_and_label_format_normalized(escrow, direct_vm):
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "roughly half done")

    # LLMs drift on key spelling/casing and on label formatting (case,
    # spaces, hyphens) - formatting noise is normalized; a numeric percent
    # in LLM output is IGNORED because the label is the economics.
    mock_judgment_raw(direct_vm, {
        "Verdict": " split-half ",
        "freelancer_share": "99",
        "reasoning": "about half the spec fulfilled",
    })
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "about half there")
    direct_vm.sender = freelancer
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "SPLIT_HALF"
    assert ag["judgment_percent"] == 50  # NOT 99: label-derived, no tolerance


def test_label_outside_fixed_set_reverts(escrow, direct_vm):
    """Free-form or invented outcomes are malformed output - never accepted."""
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "claim")
    for bad in ("SPLIT_60", "RELEASE_90_PERCENT", "partial", ""):
        mock_judgment_raw(direct_vm, {"decision": bad, "analysis": "x"})
        mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "fetched content")
        with direct_vm.expect_revert("[LLM_ERROR]"):
            contract.resolve_dispute("a1")
    assert contract.get_agreement("a1")["status"] == "disputed"


def test_unparseable_llm_output_reverts(escrow, direct_vm):
    """Garbage LLM output must abort the arbitration (never silently stored).

    In production the validator ALSO disagrees on this and forces leader
    rotation; in direct mode only the leader path runs, so we observe the
    transaction reverting with the [LLM_ERROR]-classing failure instead.
    """
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "claim")
    mock_judgment_raw(direct_vm, "the work seems fine to me, trust me")
    mock_delivery_fetch(direct_vm, r"delivered\.example/a1", "fetched content")
    direct_vm.sender = client
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.resolve_dispute("a1")
    # State is untouched by the reverted arbitration.
    assert contract.get_agreement("a1")["status"] == "disputed"


# ---------------------------------------------------------------------------
# Evidence acquisition: delivered URLs are fetched by the evaluator itself
# ---------------------------------------------------------------------------

def test_delivery_url_extraction_is_first_url_only(escrow, direct_vm):
    """Only the first http(s) link becomes verifiable evidence."""
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "t", "spec", AMOUNT)
    direct_vm.value = AMOUNT
    contract.fund_escrow("a1")
    direct_vm.value = 0
    direct_vm.sender = freelancer
    contract.submit_delivery(
        "a1",
        "see https://example.com/a first, then http://example.net/b too",
    )
    assert contract.get_agreement("a1")["delivery_url"] == "https://example.com/a"


def test_arbitration_feeds_own_fetch_into_prompt(escrow, direct_vm):
    """The LLM prompt must contain content fetched by THIS evaluator.

    Proof mechanism: the LLM mock only matches prompts containing the
    FETCHED_MARKER string, which exists solely in the mocked WEB response.
    If the contract passed only the submitter's description, exec_prompt
    would hit no mock and the arbitration would revert.
    """
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "Landing page",
                              "3 sections with contact form", AMOUNT)
    direct_vm.value = AMOUNT
    contract.fund_escrow("a1")
    direct_vm.value = 0
    direct_vm.sender = freelancer
    contract.submit_delivery("a1", "deployed at https://work.example/dev")
    direct_vm.sender = client
    contract.raise_dispute("a1", "client doubts it")

    mock_judgment(direct_vm, "RELEASE_FULL")
    mock_delivery_fetch(direct_vm, r"work\.example/dev",
                        "FETCHED_MARKER hero features contact-form")

    direct_vm.sender = client
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "RELEASE_FULL"
    record = json.loads(ag["arbitration_history"][0])
    assert record["evidence_status"] == "fetched"


def test_unreachable_url_defaults_to_refund_without_llm(escrow, direct_vm):
    """Un-fetchable artifact => deterministic REFUND_FULL fallback.

    The LLM mock below would answer RELEASE_FULL; the arbitration still
    records REFUND_FULL, proving the fallback short-circuits BEFORE the LLM
    is ever consulted: unverifiable work must not be paid out.
    """
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "Landing page", "spec", AMOUNT)
    direct_vm.value = AMOUNT
    contract.fund_escrow("a1")
    direct_vm.value = 0
    direct_vm.sender = freelancer
    contract.submit_delivery("a1", "https://dead.example/work")
    direct_vm.sender = client
    contract.raise_dispute("a1", "cannot open anything")

    mock_judgment(direct_vm, "RELEASE_FULL")
    mock_delivery_fetch(direct_vm, r"dead\.example/work", "not found", status=404)

    direct_vm.sender = freelancer
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "REFUND_FULL"
    assert ag["judgment_percent"] == 0
    record = json.loads(ag["arbitration_history"][0])
    assert record["evidence_status"] == "unreachable"

    contract.accept_resolution("a1")
    assert contract.get_withdrawable(client) == AMOUNT
    assert contract.get_withdrawable(freelancer) == 0


def test_server_error_url_also_not_payable(escrow, direct_vm):
    """5xx responses are 'not reliably there' - same refund-by-default path."""
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "Landing page", "spec", AMOUNT)
    direct_vm.value = AMOUNT
    contract.fund_escrow("a1")
    direct_vm.value = 0
    direct_vm.sender = freelancer
    contract.submit_delivery("a1", "app at https://flaky.example/live")
    direct_vm.sender = client
    contract.raise_dispute("a1", "500s everywhere")

    mock_judgment(direct_vm, "RELEASE_FULL")
    mock_delivery_fetch(direct_vm, r"flaky\.example/live", "boom", status=500)

    direct_vm.sender = client
    contract.resolve_dispute("a1")
    assert contract.get_agreement("a1")["judgment_decision"] == "REFUND_FULL"


def test_no_url_delivery_fails_closed_to_refund_without_llm(escrow, direct_vm):
    """FAIL CLOSED: a delivery with no URL can never pay out or split.

    Reviewer-driven change: the old description-only path let the LLM
    release or split funds based solely on the freelancer's own words.
    Now an empty/missing URL refunds deterministically BEFORE any prompt
    or fetch. Proof the LLM is never consulted: the mock below would
    answer SPLIT_HALF, yet the stored outcome is REFUND_FULL.
    """
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "Landing page", "spec", AMOUNT)
    direct_vm.value = AMOUNT
    contract.fund_escrow("a1")
    direct_vm.value = 0
    direct_vm.sender = freelancer
    contract.submit_delivery(
        "a1", "Done! Files were handed over in person; no link."
    )
    assert contract.get_agreement("a1")["delivery_url"] == ""
    direct_vm.sender = client
    contract.raise_dispute("a1", "nothing was delivered")

    mock_judgment(direct_vm, "SPLIT_HALF")  # must NEVER be consulted

    direct_vm.sender = freelancer
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "REFUND_FULL"
    assert ag["judgment_percent"] == 0
    record = json.loads(ag["arbitration_history"][0])
    assert record["evidence_status"] == "no_url_provided"
    assert record["decision"] == "REFUND_FULL"
    assert "fail-closed" in ag["judgment_analysis"]

    contract.accept_resolution("a1")
    assert contract.get_withdrawable(client) == AMOUNT
    assert contract.get_withdrawable(freelancer) == 0


def test_whitespace_only_url_path_also_fails_closed(escrow, direct_vm):
    """A whitespace-only URL argument is treated as no URL at all."""
    contract, client, freelancer, _ = escrow
    direct_vm.sender = client
    contract.create_agreement("a1", freelancer, "t", "spec", AMOUNT)
    direct_vm.value = AMOUNT
    contract.fund_escrow("a1")
    direct_vm.value = 0
    direct_vm.sender = freelancer
    contract.submit_delivery("a1", "delivered by carrier pigeon")
    direct_vm.sender = client
    contract.raise_dispute("a1", "claim")
    mock_judgment(direct_vm, "RELEASE_FULL")  # must NEVER be consulted
    direct_vm.sender = client
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "REFUND_FULL"
    record = json.loads(ag["arbitration_history"][0])
    assert record["evidence_status"] == "no_url_provided"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def delivered_fixture(escrow, direct_vm):
    """Run the delivered-agreement flow inline (returns 4-tuple).

    Delivery carries a fetchable URL - the ONLY form the fail-closed
    contract allows to reach LLM arbitration. Tests that run arbitration
    must additionally mock the fetch (200) via mock_delivery_fetch, since
    mock_judgment clears all mocks first.
    """
    contract, client, freelancer, stranger = escrow
    direct_vm.sender = client
    contract.create_agreement(
        "a1", freelancer, "Landing page",
        "Build a 3-section landing page with a contact form.", AMOUNT,
    )
    direct_vm.value = AMOUNT
    contract.fund_escrow("a1")
    direct_vm.value = 0
    direct_vm.sender = freelancer
    contract.submit_delivery(
        "a1",
        "https://delivered.example/a1 - all three sections and the contact "
        "form are live on this page.",
    )
    return contract, client, freelancer, stranger
