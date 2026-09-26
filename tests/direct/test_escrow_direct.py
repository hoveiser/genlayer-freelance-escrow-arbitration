"""Direct-mode unit tests for the Freelance Escrow contract.

NOTE: Direct mode runs the LEADER path only — the comparative validator logic
inside `_arbitrate` (independent LLM re-run, decision-field comparison with
percent tolerance, error → disagreement → leader rotation) is NOT exercised
here. See tests/integration/ for consensus-level coverage.
"""

from conftest import AMOUNT, mock_judgment, mock_judgment_raw

GIGAX = 10**18


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

    direct_vm.value = 0
    direct_vm.sender = client
    contract.approve_delivery("a1")
    ag = contract.get_agreement("a1")
    assert ag["status"] == "released"
    assert ag["judgment_decision"] == "release"
    assert ag["judgment_percent"] == 100

    assert contract.get_withdrawable(freelancer) == AMOUNT
    assert contract.get_withdrawable(client) == 0

    direct_vm.sender = freelancer
    contract.withdraw()
    assert contract.get_withdrawable(freelancer) == 0
    assert contract.get_escrow_pool() == 0


def test_dispute_full_release(escrow, direct_vm):
    """Validator-consensus arbitration decides the work fully fulfills the spec."""
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "Client suspects the files are placeholders")

    mock_judgment(direct_vm, "release", 100, "Work matches the spec on all points")
    direct_vm.sender = freelancer
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["status"] == "arbitrated"
    assert ag["judgment_decision"] == "release"
    assert ag["judgment_percent"] == 100

    contract.accept_resolution("a1")  # freelancer accepts
    assert contract.get_withdrawable(freelancer) == AMOUNT
    assert contract.get_withdrawable(client) == 0


def test_dispute_full_refund(escrow, direct_vm):
    """Arbitration decides the work fails the spec: full refund to the client."""
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "Delivered file does not exist")

    mock_judgment(direct_vm, "refund", 0, "Deliverable does not satisfy the spec")
    direct_vm.sender = client
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "refund"
    assert ag["judgment_percent"] == 0

    contract.accept_resolution("a1")
    assert contract.get_agreement("a1")["status"] == "refunded"
    assert contract.get_withdrawable(client) == AMOUNT
    assert contract.get_withdrawable(freelancer) == 0


def test_dispute_split(escrow, direct_vm):
    """Arbitration awards 60% of the escrow to the freelancer."""
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = freelancer
    contract.raise_dispute("a1", "Client is refusing to approve completed work")

    mock_judgment(direct_vm, "split", 60, "Partial spec compliance")
    direct_vm.sender = client
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "split"
    assert ag["judgment_percent"] == 60

    contract.accept_resolution("a1")
    assert contract.get_agreement("a1")["status"] == "settled"
    assert contract.get_withdrawable(freelancer) == 3 * GIGAX      # 60%
    assert contract.get_withdrawable(client) == 2 * GIGAX          # 40%

    direct_vm.sender = client
    contract.withdraw()
    assert contract.get_withdrawable(client) == 0
    assert contract.get_escrow_pool() == 3 * GIGAX


# ---------------------------------------------------------------------------
# Appeal path (max 1 re-trigger of resolve_dispute)
# ---------------------------------------------------------------------------

def test_appeal_path_single_retrigger(escrow, direct_vm):
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "Missing the contact form required by the spec")

    # First arbitration: refund.
    mock_judgment(direct_vm, "refund", 0)
    direct_vm.sender = freelancer
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["appeal_count"] == 0
    assert ag["judgment_decision"] == "refund"
    assert len(ag["arbitration_history"]) == 1

    # Appeal: freelancer re-triggers resolve_dispute; second judgment wins.
    mock_judgment(direct_vm, "split", 50, "Contact form present but broken layout")
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["appeal_count"] == 1
    assert ag["judgment_decision"] == "split"
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
    mock_judgment(direct_vm, "refund", 0)
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
    mock_judgment(direct_vm, "split", 50)
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

def test_judgment_synonyms_and_string_percent_normalized(escrow, direct_vm):
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "half the pages missing")

    # LLMs drift on key spelling/casing and return numbers as strings.
    mock_judgment_raw(direct_vm, {
        "Verdict": " Partial_Release ",
        "freelancer_share": "35",
        "reasoning": "some pages missing",
    })
    direct_vm.sender = freelancer
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "split"
    assert ag["judgment_percent"] == 35


def test_inconsistent_split_canonicalized(escrow, direct_vm):
    """decision='split' with percent=100 must canonicalize to full release."""
    contract, client, freelancer, _ = delivered_fixture(escrow, direct_vm)
    direct_vm.sender = client
    contract.raise_dispute("a1", "claim")
    mock_judgment(direct_vm, "split", 100)
    direct_vm.sender = client
    contract.resolve_dispute("a1")
    ag = contract.get_agreement("a1")
    assert ag["judgment_decision"] == "release"
    assert ag["judgment_percent"] == 100


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
    direct_vm.sender = client
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.resolve_dispute("a1")
    # State is untouched by the reverted arbitration.
    assert contract.get_agreement("a1")["status"] == "disputed"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def delivered_fixture(escrow, direct_vm):
    """Run the delivered_agreement fixture flow inline (returns 4-tuple)."""
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
    contract.submit_delivery("a1", "https://example.com/work")
    return contract, client, freelancer, stranger
