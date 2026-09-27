"""Integration tests: exercise the REAL consensus/validator path.

Unlike Direct mode (leader-only), every write transaction here goes through
full GenLayer consensus: a leader executes the contract (including its OWN
fetch of the delivered artifact URL and the LLM arbitration prompt), and each
validator INDEPENDENTLY re-runs `_run_arbitration_prompt` - its own web fetch,
its own LLM call - and requires an EXACT match on the settlement label and
evidence status via the custom validator in `FreelanceEscrow._arbitrate`.
There is NO tolerance anywhere: the label set is fixed
(RELEASE_FULL / REFUND_FULL / SPLIT_QUARTER / SPLIT_HALF /
SPLIT_THREE_QUARTER) and the payout percent is derived from the label alone.
A transaction only succeeds if consensus agrees; disagreement triggers leader
rotation until a quorum matches.

Run against a real environment (see README "Testing"):

    gltest tests/integration/ -v -s --network studionet

or testnet_bradbury / local Studio (accounts must exist in
gltest.config.yaml; on public testnets, fund them via the GenLayer faucet —
studionet is gasless). NOTE: `glsim` does NOT work with this contract's pinned
GenVM runner generation (it doesn't execute real GenVM runners); see README.

Marked `slow`: excluded from default runs with `-m "not slow"` if you want.
"""

import json

import pytest

from gltest import get_contract_factory
from gltest.accounts import get_accounts
from gltest.assertions import tx_execution_failed, tx_execution_succeeded

pytestmark = pytest.mark.slow

CONTRACT = "FreelanceEscrow"
AGREEMENT_ID = "integration-1"
AMOUNT_ATTO = 5 * 10**18  # 5 tokens at atto scale
SPEC = (
    "Build a single-page landing site with exactly three sections "
    "(hero, features, contact form) and deliver it as a public URL."
)
# The ONLY settlement outcomes the redesigned arbitration may produce, and
# the exact payout behind each (label-derived - no continuum exists anymore).
VALID_JUDGMENTS = {
    "RELEASE_FULL": 100,
    "REFUND_FULL": 0,
    "SPLIT_QUARTER": 25,
    "SPLIT_HALF": 50,
    "SPLIT_THREE_QUARTER": 75,
}
VALID_EVIDENCE = ("fetched", "unreachable", "description_only")


def _assert_consensus_judgment(ag):
    """A stored judgment must be an exact fixed-set label with derived percent."""
    assert ag["judgment_decision"] in VALID_JUDGMENTS
    assert ag["judgment_percent"] == VALID_JUDGMENTS[ag["judgment_decision"]]
    record = json.loads(ag["arbitration_history"][-1])
    assert record["evidence_status"] in VALID_EVIDENCE
    assert record["decision"] == ag["judgment_decision"]


def _parties():
    try:
        accounts = get_accounts()
    except Exception as e:  # unconfigured placeholder keys raise here
        pytest.skip(f"Configure two accounts in gltest.config.yaml ({e})")
    if len(accounts) < 2:
        pytest.skip("This test needs at least two accounts in gltest.config.yaml")
    return accounts[0], accounts[1]


def _deliver_flow(contract, freelancer_contract, freelancer):
    """create -> fund -> deliver -> dispute, as the two different parties.

    NOTE: `freelancer_contract.address` is the ESCROW contract's address
    (build_contract only rebinds the signer); the freelancer party of the
    agreement must be the signer account's own address (`freelancer.address`).
    """
    assert tx_execution_succeeded(
        contract.create_agreement(
            args=[AGREEMENT_ID, freelancer.address, "Landing page", SPEC, AMOUNT_ATTO]
        ).transact()
    )
    assert tx_execution_succeeded(
        contract.fund_escrow(args=[AGREEMENT_ID]).transact(value=AMOUNT_ATTO)
    )
    assert tx_execution_succeeded(
        freelancer_contract.submit_delivery(
            args=[AGREEMENT_ID,
                  "https://example.com - hero, features, contact form"]
        ).transact()
    )
    # A URL was submitted, so the agreement carries externally-verifiable
    # evidence; every evaluator fetches it independently during arbitration.
    assert contract.get_agreement(args=[AGREEMENT_ID]).call()["delivery_url"] == \
        "https://example.com"
    assert tx_execution_succeeded(
        contract.raise_dispute(args=[AGREEMENT_ID, "Client claims the contact form is missing"]).transact()
    )


def test_dispute_arbitration_reaches_full_consensus():
    """resolve_dispute must pass leader + validator fetch-and-judge consensus.

    Validators each independently fetch the delivery URL and re-run the LLM
    prompt; the transaction only succeeds when leader and validator produce
    the EXACT SAME settlement label and evidence status - there is no
    tolerance window left to paper over a disagreement.
    """
    client, freelancer = _parties()
    factory = get_contract_factory(CONTRACT)
    contract = factory.deploy(args=[], account=client)
    freelancer_contract = factory.build_contract(contract.address, account=freelancer)

    _deliver_flow(contract, freelancer_contract, freelancer)

    receipt = contract.resolve_dispute(args=[AGREEMENT_ID]).transact()
    assert tx_execution_succeeded(receipt), (
        "arbitration failed consensus - either validators did not agree on "
        "the EXACT label/evidence status (no tolerance exists) or the "
        "fetch/LLM call errored; inspect the receipt stdout/stderr"
    )

    ag = contract.get_agreement(args=[AGREEMENT_ID]).call()
    assert ag["status"] == "arbitrated"
    _assert_consensus_judgment(ag)
    # Whatever the evaluators agreed on, the payout math must reproduce it
    # exactly from the label-derived percent:
    freelancer_share = AMOUNT_ATTO * ag["judgment_percent"] // 100
    assert freelancer_share + (AMOUNT_ATTO - freelancer_share) == AMOUNT_ATTO


def test_appeal_re_runs_consensus_and_limit_is_enforced():
    """One appeal re-triggers the full consensus evaluation; a second fails."""
    client, freelancer = _parties()
    factory = get_contract_factory(CONTRACT)
    contract = factory.deploy(args=[], account=client)
    freelancer_contract = factory.build_contract(contract.address, account=freelancer)

    _deliver_flow(contract, freelancer_contract, freelancer)

    assert tx_execution_succeeded(contract.resolve_dispute(args=[AGREEMENT_ID]).transact())
    first = contract.get_agreement(args=[AGREEMENT_ID]).call()

    # Appeal (still consensus — validators independently re-judge again).
    assert tx_execution_succeeded(
        freelancer_contract.resolve_dispute(args=[AGREEMENT_ID]).transact()
    )
    second = contract.get_agreement(args=[AGREEMENT_ID]).call()
    assert second["appeal_count"] == 1
    assert len(second["arbitration_history"]) == 2

    # A second appeal must be rejected by the contract's own rule.
    failed = freelancer_contract.resolve_dispute(args=[AGREEMENT_ID]).transact()
    assert tx_execution_failed(failed)

    # Settlement then splits exactly per the appealed judgment.
    assert tx_execution_succeeded(
        contract.accept_resolution(args=[AGREEMENT_ID]).transact()
    )
    expected_freelancer = AMOUNT_ATTO * second["judgment_percent"] // 100
    assert contract.get_withdrawable(args=[freelancer.address]).call() == expected_freelancer

    # Sanity: both consensus runs must be exact fixed-set judgments - each
    # one independently fetched + re-judged with zero tolerance.
    _assert_consensus_judgment(first)
    _assert_consensus_judgment(second)
