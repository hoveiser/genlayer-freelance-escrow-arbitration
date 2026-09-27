# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""
Freelance Escrow with AI Consensus Arbitration - GenLayer Intelligent Contract.

A client deposits native-token funds into escrow for a freelancer to complete a
defined task described by a written spec. On delivery, either party can raise a
dispute. When disputed, GenLayer validators INDEPENDENTLY fetch the delivered
artifact (when it was submitted as a URL) and evaluate it against the original
spec using LLM-based comparative judgment, and the contract releases funds
based on VALIDATOR CONSENSUS - not a single party's claim and not a single
leader's LLM answer.

Settlement outcomes form a small FIXED label set (RELEASE_FULL / REFUND_FULL /
SPLIT_QUARTER / SPLIT_HALF / SPLIT_THREE_QUARTER). Because the chosen label
DIRECTLY determines each party's native-token transfer, validators must agree
on the EXACT SAME label - there is deliberately NO numeric tolerance anywhere
in the consensus path.

Why GenLayer consensus is necessary here
-----------------------------------------
The arbitration decision directly moves real money (an atto-scale u256 payout
split). An LLM judgment and a web fetch are non-deterministic external inputs:
if we trusted one leader's output, the leader could rug the escrow and no one
would notice. So every arbitration goes through leader + validator
re-execution of the SAME fetch-and-evaluate routine with an exact-agreement
rule on the settlement label (see `_arbitrate` below), which is exactly what
GenLayer's equivalence-principle machinery is for.

Money convention: every value is an integer in ATTO scale (1 token = 10**18).
Never use floats for money or for anything crossing the consensus boundary.
"""

import json
from dataclasses import dataclass

from genlayer import *

# ---------------------------------------------------------------------------
# Error classification prefixes
# ---------------------------------------------------------------------------
# Validators must be able to tell *why* the leader failed. Business-logic
# errors ([EXPECTED]) are deterministic and must match exactly; LLM failures
# ([LLM_ERROR]) are non-deterministic and ALWAYS force disagreement so the
# leader rotates and a fresh arbitration is attempted.
ERROR_EXPECTED = "[EXPECTED]"   # deterministic business-rule violation
ERROR_LLM = "[LLM_ERROR]"       # LLM misbehavior - always disagree, force rotation

# ---------------------------------------------------------------------------
# Agreement statuses - stored as plain `str`, NEVER as a Python Enum.
# Enum instances are not persistable in GenLayer storage; the string values
# are canonical and comparable across validators.
# ---------------------------------------------------------------------------
STATUS_CREATED = "created"        # agreement drafted, no funds yet
STATUS_FUNDED = "funded"          # client deposited escrow, awaiting delivery
STATUS_DELIVERED = "delivered"    # freelancer submitted work, awaiting accept/dispute
STATUS_DISPUTED = "disputed"      # a party raised a dispute, awaiting arbitration
STATUS_ARBITRATED = "arbitrated"  # LLM consensus judgment recorded, awaiting settlement/appeal
STATUS_SETTLED = "settled"        # payout computed & credited, awaiting withdrawal
STATUS_RELEASED = "released"      # client approved delivery - full release, no arbitration
STATUS_REFUNDED = "refunded"      # client never funded / cancelled before funding - no funds moved

# Maximum number of re-arbitrations (appeals) allowed per agreement.
MAX_APPEALS = 1

# ---------------------------------------------------------------------------
# Settlement outcomes - a small FIXED, enumerable label set.
#
# Design decision (reviewer-driven): the earlier design let the LLM emit a
# free-form 0-100 percentage and the comparative validator accepted leader /
# validator percentages differing by up to 15 points. That was wrong on
# purpose: the percentage DIRECTLY determines each party's token transfer, so
# "close enough" is not an acceptable consensus basis for moving money.
# Validators must now produce the EXACT SAME label; the transfer amount is a
# deterministic pure function of that label (_percent_for_decision). There is
# no tolerance anywhere in the consensus comparison.
# ---------------------------------------------------------------------------
DECISION_RELEASE_FULL = "RELEASE_FULL"                # freelancer gets 100%
DECISION_REFUND_FULL = "REFUND_FULL"                  # client gets 100% back
DECISION_SPLIT_QUARTER = "SPLIT_QUARTER"              # freelancer gets 25%
DECISION_SPLIT_HALF = "SPLIT_HALF"                    # freelancer gets 50%
DECISION_SPLIT_THREE_QUARTER = "SPLIT_THREE_QUARTER"  # freelancer gets 75%

VALID_DECISIONS = (
    DECISION_RELEASE_FULL,
    DECISION_REFUND_FULL,
    DECISION_SPLIT_QUARTER,
    DECISION_SPLIT_HALF,
    DECISION_SPLIT_THREE_QUARTER,
)


def _percent_for_decision(decision: str) -> int:
    """The payout split behind a settlement label - deterministic, integer.

    Because the percent is DERIVED from the label (and never parsed from LLM
    output), exact agreement on the label implies exact agreement on the
    money. Any caller that could reach this with a non-label is a programming
    bug, surfaced as a deterministic [EXPECTED] error.
    """
    if decision == DECISION_RELEASE_FULL:
        return 100
    if decision == DECISION_REFUND_FULL:
        return 0
    if decision == DECISION_SPLIT_QUARTER:
        return 25
    if decision == DECISION_SPLIT_HALF:
        return 50
    if decision == DECISION_SPLIT_THREE_QUARTER:
        return 75
    raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown settlement decision '{decision}'")


# ---------------------------------------------------------------------------
# Evidence acquisition modes for arbitration (see _run_arbitration_prompt).
# A delivery that includes a URL is only judged against content the evaluator
# fetched WITH ITS OWN EYES; no fetched copy is ever handed between parties.
# ---------------------------------------------------------------------------
EVIDENCE_FETCHED = "fetched"                  # URL retrieved successfully
EVIDENCE_UNREACHABLE = "unreachable"          # URL present but not retrievable
EVIDENCE_DESCRIPTION_ONLY = "description_only"  # no URL: lower-assurance path

# Cap on fetched content fed into the prompt (bounded prompt = bounded
# non-determinism and predictable validator cost).
MAX_EVIDENCE_CHARS = 8000

# Placeholder used before any arbitration exists.
JUDGMENT_NONE = "none"


@allow_storage
@dataclass
class Agreement:
    """One escrow agreement.

    UPGRADABILITY RULE: new fields may only be APPENDED at the end. Storage
    layout is positional - inserting or reordering fields would corrupt every
    already-deployed agreement record.
    """

    id: str
    client: Address
    freelancer: Address
    title: str
    spec: str                    # the written statement-of-work the LLM grades against
    escrow_atto: u256            # deal amount, atto scale (value * 10**18)
    delivered_work: str          # deliverable text / description / link submitted by freelancer
    status: str                  # one of STATUS_* above - str, never Enum
    dispute_reason: str          # set by whichever party raised the dispute
    judgment_decision: str       # one of DECISION_* labels (or JUDGMENT_NONE before arbitration)
    judgment_percent: u256       # payout percent to the freelancer, DERIVED from the label
    judgment_analysis: str       # latest LLM reasoning, informational only
    created_at: str              # ISO-8601 timestamp of creation
    funded_at: str               # ISO-8601 timestamp of escrow funding ("" until funded)
    delivered_at: str            # ISO-8601 timestamp of delivery ("" until delivered)
    disputed_at: str             # ISO-8601 timestamp of dispute ("" until disputed)
    resolved_at: str             # ISO-8601 timestamp of latest arbitration/settlement
    appeal_count: u256           # number of re-arbitrations used (max MAX_APPEALS)
    arbitration_nonce: u256      # increments on every arbitration run; ties history ordering
    arbitration_history: DynArray[str]  # json.dumps() of each arbitration record, in order
    client_credit_atto: u256     # unsettled refund credit owed to the client (atto)
    freelancer_credit_atto: u256 # unsettled release credit owed to the freelancer (atto)
    delivery_url: str            # externally-hosted evidence URL extracted from
                                 # delivered_work at submission time ("" if none).
                                 # APPENDED at the end per the layout rule above.


def _account_key(account) -> str:
    """Canonical ledger key for an address.

    `str()` of an Address is not stable across storage round-trips, so the
    TreeMap key is derived from the raw 20 bytes - identical on every path.
    Accepts Address, raw bytes, or hex/base64 strings.
    """
    if isinstance(account, Address):
        return account.as_bytes.hex()
    return Address(account).as_bytes.hex()


def _now_iso() -> str:
    """Transaction timestamp as an ISO-8601 string.

    Comes from the consensus-signed message (`gl.message_raw['datetime']`),
    NOT from a wall clock the contract could not agree on: every validator
    reads the same message, so this is equivalent across the consensus set.
    (This runner exposes no other deterministic time source.)
    """
    return str(gl.message_raw["datetime"])


def _extract_delivery_url(delivered_work: str) -> str:
    """Deterministically pull the first http(s) URL out of a delivery string.

    Runs in the DETERMINISTIC context at submit time, so only plain str
    operations are used (no regex module, no nondeterminism). If multiple
    links are pasted, only the first is treated as externally-verifiable
    evidence; the remainder is judged as description text.
    """
    pos = delivered_work.find("https://")
    if pos == -1:
        pos = delivered_work.find("http://")
    if pos == -1:
        return ""
    end = len(delivered_work)
    i = pos
    while i < end:
        ch = delivered_work[i]
        if (ch == " " or ch == "\t" or ch == "\n" or ch == '"'
                or ch == "'" or ch == "<" or ch == ">" or ch == "`"):
            end = i
            break
        i += 1
    return delivered_work[pos:end]


def _normalize_judgment(raw) -> dict:
    """Parse the LLM arbitration answer into a canonical settlement record.

    The ONLY accepted answers are the five fixed DECISION_* labels. Common
    formatting noise is normalized away (string-vs-dict, key aliasing, case,
    spaces/hyphens), but SEMANTICS are never approximated: a label outside
    the fixed set, a missing field, or unparseable JSON raises ERROR_LLM so
    the validator refuses to agree and the leader rotates. The payout percent
    is DERIVED from the label via _percent_for_decision - raw percent-like
    numbers in LLM output are deliberately ignored, because the label IS the
    economics.
    """
    if isinstance(raw, str):
        # Some backends hand back a raw string even with response_format=json;
        # strip prose around the object before parsing.
        first = raw.find("{")
        last = raw.rfind("}")
        if first == -1 or last == -1 or last <= first:
            raise gl.vm.UserError(f"{ERROR_LLM} No JSON object in LLM output")
        try:
            raw = json.loads(raw[first:last + 1])
        except Exception:
            raise gl.vm.UserError(f"{ERROR_LLM} Unparseable JSON from LLM")
    if not isinstance(raw, dict):
        raise gl.vm.UserError(f"{ERROR_LLM} LLM returned non-dict: {type(raw)}")

    # Key aliasing - LLMs invent alternate names AND casings for the same field.
    lowered = {str(k).strip().lower(): v for k, v in raw.items()}

    decision_raw = lowered.get("decision")
    if decision_raw is None:
        for alt in ("verdict", "ruling", "outcome", "resolution", "judgment"):
            if alt in lowered:
                decision_raw = lowered[alt]
                break
    if decision_raw is None:
        raise gl.vm.UserError(f"{ERROR_LLM} Missing 'decision'. Keys: {list(raw.keys())}")

    # Normalize formatting only (case / spaces / hyphens), then require an
    # EXACT member of the fixed label set. Anything else is malformed output.
    label = str(decision_raw).strip().upper().replace(" ", "_").replace("-", "_")
    if label not in VALID_DECISIONS:
        raise gl.vm.UserError(
            f"{ERROR_LLM} decision label not in fixed set: {decision_raw!r}"
        )

    analysis = str(lowered.get("analysis") or lowered.get("reasoning") or lowered.get("reason") or "")
    if len(analysis) > 2000:
        analysis = analysis[:2000]

    return {
        "decision": label,
        "percent": _percent_for_decision(label),
        "analysis": analysis,
    }


def _fetch_evidence(url: str) -> dict:
    """Independently retrieve the externally-hosted delivered artifact.

    CONSENSUS-CRITICAL PROPERTY: every evaluator (the leader AND each
    validator) runs this INSIDE its own execution of
    `_run_arbitration_prompt` - nobody fetches once and hands a copy to the
    others. A delivery is trusted only as far as each evaluator has seen the
    content with its own eyes.

    A fetch failure is a RESULT ({"ok": False}), not an exception, so it is
    comparable: if the leader cannot fetch but a validator can, their
    conclusions differ and consensus disagrees (leader rotates, retry).
    When the whole ring cannot fetch, everyone independently lands on the
    same REFUND_FULL fallback - real consensus, never a silent pass.
    """
    try:
        res = gl.nondet.web.get(url)
        status = int(res.status)
        body = res.body
    except Exception:
        return {"ok": False, "content": ""}
    if status != 200:
        # 4xx: the artifact simply is not there. 5xx / redirects: unreliable.
        # All are treated as not-verifiable; transient disagreements rotate
        # the leader, while a persistently dead URL consensus-refunds.
        return {"ok": False, "content": ""}
    try:
        text = str(body, errors="replace")
    except Exception:
        return {"ok": False, "content": ""}
    return {"ok": True, "content": text[:MAX_EVIDENCE_CHARS]}


def _run_arbitration_prompt(title: str, spec: str, delivered_work: str,
                            delivery_url: str, dispute_reason: str) -> dict:
    """LEADER-SIDE function: fetch evidence, run LLM arbitration, return
    normalized fields.

    This is deliberately a *pure function of its arguments*: the exact same
    arguments produce the exact same prompt string. Validators re-execute
    this very function themselves with the same inputs - including their own
    fresh fetch of `delivery_url` - so they form an INDEPENDENT candidate
    judgment rather than trusting the leader's retrieved content or answer.
    """
    if delivery_url != "":
        fetched = _fetch_evidence(delivery_url)
        if not fetched["ok"]:
            # Deterministic fallback policy - NO LLM is consulted: work whose
            # external artifact cannot be retrieved must not be paid out.
            # Both evaluators reach this branch only from their OWN failed
            # fetch, so exact agreement here (REFUND_FULL + "unreachable")
            # is genuine consensus about the unverifiable, not a pass.
            return {
                "decision": DECISION_REFUND_FULL,
                "percent": 0,
                "evidence_status": EVIDENCE_UNREACHABLE,
                "analysis": (
                    "The delivery URL could not be retrieved by the evaluator; "
                    "under the refund-by-default policy for unverifiable work "
                    "the escrow is returned to the client."
                ),
            }
        evidence_block = (
            "EXTERNALLY FETCHED ARTIFACT - retrieved directly from "
            f"{delivery_url} by THIS evaluator (not the submitter's words):\n"
            f"{fetched['content']}\n\n"
        )
        evidence_status = EVIDENCE_FETCHED
    else:
        # Lower-assurance path: nothing external to verify, the LLM can only
        # weigh the submitter's own description against the spec. Deliberately
        # still allowed - see README "Evidence acquisition" for the reasoning.
        evidence_block = ""
        evidence_status = EVIDENCE_DESCRIPTION_ONLY

    prompt = (
        "You are an impartial arbitration expert for a freelance escrow platform. "
        "Evaluate the delivered work strictly against the agreed written spec and "
        "choose exactly ONE settlement outcome for the escrowed funds.\n\n"
        f"TASK TITLE:\n{title}\n\n"
        f"WRITTEN SPEC (the binding agreement):\n{spec}\n\n"
        f"DELIVERY SUBMITTED BY THE FREELANCER (description text):\n{delivered_work}\n\n"
        f"{evidence_block}"
        f"DISPUTE REASON RAISED BY A PARTY (claim only, not evidence):\n{dispute_reason}\n\n"
        "Judge ONLY on how well the delivered/fetched work satisfies the spec. "
        "Treat party claims skeptically. You MUST choose exactly ONE of these "
        "fixed labels (no numbers, no other words):\n"
        "- RELEASE_FULL: the work substantially fulfills the spec "
        "(freelancer receives 100%)\n"
        "- REFUND_FULL: the work fails to fulfill the spec "
        "(client receives 100% back)\n"
        "- SPLIT_QUARTER: clearly partial fulfillment, low credit to the work "
        "(freelancer 25% / client 75%)\n"
        "- SPLIT_HALF: roughly half the spec fulfilled (freelancer 50%)\n"
        "- SPLIT_THREE_QUARTER: nearly complete with minor gaps "
        "(freelancer 75%)\n\n"
        "Respond with ONLY a JSON object of this exact shape:\n"
        '{"decision": "<one label verbatim>", '
        '"analysis": "<short objective explanation>"}'
    )
    raw = gl.nondet.exec_prompt(prompt, response_format="json")
    judgment = _normalize_judgment(raw)
    judgment["evidence_status"] = evidence_status
    return judgment


def _handle_leader_error(leaders_res, leader_fn) -> bool:
    """Validator rule for when the leader's nondeterministic call failed.

    Equivalence-principle note: a validator must NEVER agree with a leader on
    an error it cannot substantiate. We re-run the leader function ourselves:
    - if we succeed where the leader failed, the failure was leader-specific ->
      disagree (forces leader rotation),
    - if we hit the same deterministic business error, that is legitimate ->
      agree so the transaction can revert uniformly,
    - transient/LLM-classified errors only agree with their own class,
    - anything unknown -> disagree. Silent agreement on broken output is how
      bad states get finalized.
    """
    leader_msg = leaders_res.message if hasattr(leaders_res, "message") else ""
    try:
        leader_fn()
        return False  # leader errored but validator succeeded -> disagree
    except gl.vm.UserError as e:
        validator_msg = e.message if hasattr(e, "message") else str(e)
        if validator_msg.startswith(ERROR_EXPECTED):
            # Deterministic business error: must match the leader's exactly.
            return validator_msg == leader_msg
        if validator_msg.startswith(ERROR_LLM) or leader_msg.startswith(ERROR_LLM):
            # LLM misbehavior is never reproducible - disagree, rotate leader.
            return False
        return False
    except Exception:
        return False


class FreelanceEscrow(gl.Contract):
    """Escrow with LLM-consensus arbitration for disputes.

    NOTE on sender checks: the pinned runner SDK exposes the transaction
    initiator as `gl.message.sender_address` (there is no `sender_account`
    attribute in this SDK version); every access-control check below uses it.
    """

    # --- storage fields (class-level typed annotations only) ---
    agreements: TreeMap[str, Agreement]      # id -> agreement
    agreement_ids: DynArray[str]             # insertion order for enumeration
    escrow_pool_atto: u256                   # native tokens currently held by this contract
    withdrawable_atto: TreeMap[str, u256]    # account (str Address) -> claimable credit
    next_nonce: u256                         # monotonic counter used as an anti-replay nonce

    def __init__(self):
        self.escrow_pool_atto = 0
        self.next_nonce = 0

    # ------------------------------------------------------------------
    # Lifecycle: create -> fund -> deliver -> (approve | dispute -> arbitrate -> settle)
    # ------------------------------------------------------------------

    @gl.public.write
    def create_agreement(self, agreement_id: str, freelancer: Address, title: str,
                         spec: str, escrow_amount_atto: u256) -> None:
        """Draft an agreement. The caller becomes the client."""
        if agreement_id == "":
            raise gl.vm.UserError("ESCROW: agreement_id must not be empty")
        if agreement_id in self.agreements:
            raise gl.vm.UserError(f"ESCROW: agreement '{agreement_id}' already exists")
        if spec == "":
            raise gl.vm.UserError("ESCROW: spec must not be empty")
        if title == "":
            raise gl.vm.UserError("ESCROW: title must not be empty")
        if escrow_amount_atto <= 0:
            raise gl.vm.UserError("ESCROW: escrow_amount_atto must be positive")
        if not isinstance(freelancer, Address):
            freelancer = Address(freelancer)
        if freelancer == gl.message.sender_address:
            raise gl.vm.UserError("ESCROW: client and freelancer must differ")

        self.agreements[agreement_id] = Agreement(
            id=agreement_id,
            client=gl.message.sender_address,
            freelancer=freelancer,
            title=title,
            spec=spec,
            escrow_atto=escrow_amount_atto,
            delivered_work="",
            status=STATUS_CREATED,
            dispute_reason="",
            judgment_decision=JUDGMENT_NONE,
            judgment_percent=0,
            judgment_analysis="",
            created_at=_now_iso(),
            funded_at="",
            delivered_at="",
            disputed_at="",
            resolved_at="",
            appeal_count=0,
            arbitration_nonce=0,
            arbitration_history=[],  # coerced to a stored DynArray on assignment
            client_credit_atto=0,
            freelancer_credit_atto=0,
            delivery_url="",
        )
        self.agreement_ids.append(agreement_id)

    # .payable is REQUIRED by the VM for any method that accepts a non-zero
    # gl.message.value; a plain @gl.public.write reverts value-bearing calls.
    @gl.public.write.payable
    def fund_escrow(self, agreement_id: str) -> None:
        """Client deposits the escrow amount as native value (atto scale)."""
        ag = self._get(agreement_id)
        if gl.message.sender_address != ag.client:
            raise gl.vm.UserError("ESCROW: only the client can fund the escrow")
        if ag.status != STATUS_CREATED:
            raise gl.vm.UserError(f"ESCROW: cannot fund in status '{ag.status}'")
        if gl.message.value < ag.escrow_atto:
            raise gl.vm.UserError(
                f"ESCROW: sent {gl.message.value} atto, expected exactly {ag.escrow_atto} atto"
            )
        if gl.message.value > ag.escrow_atto:
            # Exact transfer keeps the pool accounting trivial and unambiguous;
            # overpayments would need their own refund ledger.
            raise gl.vm.UserError(
                f"ESCROW: overpayment not accepted, send exactly {ag.escrow_atto} atto"
            )

        ag.status = STATUS_FUNDED
        ag.funded_at = _now_iso()
        self.escrow_pool_atto = self.escrow_pool_atto + ag.escrow_atto
        self.agreements[agreement_id] = ag

    @gl.public.write
    def submit_delivery(self, agreement_id: str, delivered_work: str) -> None:
        """Freelancer submits the deliverable (text, description, or link).

        A URL inside the submission is NOT trusted content - it is only the
        ADDRESS of evidence. During arbitration every evaluator fetches that
        URL itself (_fetch_evidence); nothing fetched is stored or passed on.
        """
        ag = self._get(agreement_id)
        if gl.message.sender_address != ag.freelancer:
            raise gl.vm.UserError("ESCROW: only the freelancer can submit delivery")
        if ag.status != STATUS_FUNDED:
            raise gl.vm.UserError(f"ESCROW: cannot deliver in status '{ag.status}'")
        if delivered_work == "":
            raise gl.vm.UserError("ESCROW: delivered_work must not be empty")

        ag.delivered_work = delivered_work
        ag.delivery_url = _extract_delivery_url(delivered_work)
        ag.status = STATUS_DELIVERED
        ag.delivered_at = _now_iso()
        self.agreements[agreement_id] = ag

    @gl.public.write
    def approve_delivery(self, agreement_id: str) -> None:
        """Client accepts the delivery: full release, no arbitration needed."""
        ag = self._get(agreement_id)
        if gl.message.sender_address != ag.client:
            raise gl.vm.UserError("ESCROW: only the client can approve delivery")
        if ag.status != STATUS_DELIVERED:
            raise gl.vm.UserError(f"ESCROW: cannot approve in status '{ag.status}'")

        ag.judgment_decision = DECISION_RELEASE_FULL
        ag.judgment_percent = 100
        ag.judgment_analysis = "Client approved delivery off-chain; no arbitration."
        ag.resolved_at = _now_iso()
        self.agreements[agreement_id] = ag
        self._credit_out(agreement_id, ag, 100)
        # _credit_out already persists with status STATUS_RELEASED.

    @gl.public.write
    def raise_dispute(self, agreement_id: str, reason: str) -> None:
        """Either party can contest a delivered (or stalled) outcome."""
        ag = self._get(agreement_id)
        sender = gl.message.sender_address
        if sender != ag.client and sender != ag.freelancer:
            raise gl.vm.UserError("ESCROW: only a party to the agreement can dispute")
        if ag.status != STATUS_DELIVERED:
            raise gl.vm.UserError(f"ESCROW: cannot dispute in status '{ag.status}'")
        if reason == "":
            raise gl.vm.UserError("ESCROW: dispute reason must not be empty")

        ag.status = STATUS_DISPUTED
        ag.dispute_reason = reason
        ag.disputed_at = _now_iso()
        self.agreements[agreement_id] = ag

    # ------------------------------------------------------------------
    # Arbitration - the consensus-critical heart of the contract
    # ------------------------------------------------------------------

    @gl.public.write
    def resolve_dispute(self, agreement_id: str) -> None:
        """Run (or re-run on appeal) the validator-consensus arbitration.

        Access: either party may trigger it. A second invocation is an APPEAL
        and is capped at MAX_APPEALS (= 1) per agreement.
        """
        ag = self._get(agreement_id)
        sender = gl.message.sender_address
        if sender != ag.client and sender != ag.freelancer:
            raise gl.vm.UserError("ESCROW: only a party to the agreement can arbitrate")

        if ag.status == STATUS_DISPUTED:
            pass  # first arbitration
        elif ag.status == STATUS_ARBITRATED:
            if ag.appeal_count >= MAX_APPEALS:
                raise gl.vm.UserError(
                    f"ESCROW: appeal limit reached ({MAX_APPEALS}); settlement is final"
                )
            ag.appeal_count = ag.appeal_count + 1
        else:
            raise gl.vm.UserError(f"ESCROW: nothing to arbitrate in status '{ag.status}'")

        judgment = self._arbitrate(ag.title, ag.spec, ag.delivered_work,
                                   ag.delivery_url, ag.dispute_reason)

        ag.judgment_decision = judgment["decision"]
        ag.judgment_percent = judgment["percent"]
        ag.judgment_analysis = judgment["analysis"]
        ag.arbitration_nonce = ag.arbitration_nonce + 1
        ag.resolved_at = _now_iso()
        ag.arbitration_history.append(json.dumps({
            "nonce": ag.arbitration_nonce,
            "decision": judgment["decision"],
            "percent": judgment["percent"],
            "evidence_status": judgment["evidence_status"],
            "analysis": judgment["analysis"],
            "at": ag.resolved_at,
        }))
        ag.status = STATUS_ARBITRATED
        self.agreements[agreement_id] = ag

    def _arbitrate(self, title: str, spec: str, delivered_work: str,
                   delivery_url: str, dispute_reason: str) -> dict:
        """Consensus-wrapped fetch + LLM arbitration, CUSTOM comparative validator.

        WHY this shape - the equivalence principle, in its own words:
        1. `strict_eq` is impossible over the whole result: two runs of the
           same prompt/web call can return different text, so byte-equality
           would deadlock consensus.
        2. Validating only the LEADER's output (is it JSON? is the decision a
           known string?) is NOT consensus - the leader alone would be
           deciding who gets the money. Explicitly rejected.
        3. So each VALIDATOR independently re-runs the very same
           `_run_arbitration_prompt(...)` - its OWN fetch of the delivery URL
           and its OWN LLM call - and we require EXACT agreement on every
           economic field of the two independent judgments:
             * `decision` must be an EXACT member of the fixed label set, and
             * leader and validator labels must be IDENTICAL - no tolerance,
               none, anywhere: the label directly determines an on-chain
               token transfer, so "close enough" is not a valid basis for
               moving money. Percent cannot smuggle in wobble because it is
               DERIVED from the label, never parsed from the LLM.
             * `evidence_status` must also match: "fetched" vs "unreachable"
               are economically different worlds (one judged real content,
               the other applied the refund-by-default policy).
        4. Malformed leader output (label outside the fixed set) is an
           automatic disagreement; a leader error is handled by
           `_handle_leader_error`, which re-runs the task rather than giving
           the leader the benefit of the doubt. Both force rotation.
        """

        def leader_fn():
            return _run_arbitration_prompt(title, spec, delivered_work,
                                           delivery_url, dispute_reason)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            # Broken/non-return leader results never get the benefit of the doubt.
            if not isinstance(leaders_res, gl.vm.Return):
                return _handle_leader_error(leaders_res, leader_fn)
            leader_decision = leaders_res.calldata["decision"]
            leader_evidence = leaders_res.calldata["evidence_status"]
            if leader_decision not in VALID_DECISIONS:
                # Malformed settlement outcome: refuse outright - never
                # negotiate with output that cannot move money safely.
                return False
            try:
                # Independent re-run: this validator's OWN web fetch and OWN
                # LLM call; nothing from the leader is reused.
                validator_res = leader_fn()
            except Exception:
                # Validator's own independent run failed -> cannot compare ->
                # disagree and rotate the leader.
                return False
            # EXACT match required on every economic field. No tolerance.
            if validator_res["decision"] != leader_decision:
                return False
            return validator_res["evidence_status"] == leader_evidence

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    @gl.public.write
    def accept_resolution(self, agreement_id: str) -> None:
        """Either party finalizes an arbitration: credits the payout ledger.

        Settlement is a separate step so that the appeal window is
        unambiguous on-chain: funds never move until BOTH the arbitration is
        consensus-approved and a party settles it (an appeal must be filed
        before settlement).
        """
        ag = self._get(agreement_id)
        sender = gl.message.sender_address
        if sender != ag.client and sender != ag.freelancer:
            raise gl.vm.UserError("ESCROW: only a party to the agreement can settle")
        if ag.status != STATUS_ARBITRATED:
            raise gl.vm.UserError(f"ESCROW: nothing to accept in status '{ag.status}'")

        self._credit_out(agreement_id, ag, ag.judgment_percent)

    def _credit_out(self, agreement_id: str, ag: Agreement, percent: u256) -> None:
        """Split escrow by freelancer `percent` and credit both ledgers."""
        freelancer_atto = ag.escrow_atto * percent // 100
        client_atto = ag.escrow_atto - freelancer_atto
        ag.client_credit_atto = ag.client_credit_atto + client_atto
        ag.freelancer_credit_atto = ag.freelancer_credit_atto + freelancer_atto
        if ag.judgment_decision == DECISION_RELEASE_FULL or percent == 100:
            ag.status = STATUS_RELEASED
        elif percent == 0:
            ag.status = STATUS_REFUNDED
        else:
            ag.status = STATUS_SETTLED
        ag.resolved_at = _now_iso()
        self._add_credit(ag.client, client_atto)
        self._add_credit(ag.freelancer, freelancer_atto)
        self.agreements[agreement_id] = ag

    def _add_credit(self, account: Address, amount_atto: u256) -> None:
        if amount_atto <= 0:
            return
        key = _account_key(account)
        current = self.withdrawable_atto.get(key)
        if current is None:
            self.withdrawable_atto[key] = amount_atto
        else:
            self.withdrawable_atto[key] = current + amount_atto

    @gl.public.write
    def withdraw(self) -> None:
        """Pay out accumulated settlement credits to the caller in native tokens."""
        key = _account_key(gl.message.sender_address)
        amount = self.withdrawable_atto.get(key)
        if amount is None or amount == 0:
            raise gl.vm.UserError("ESCROW: nothing to withdraw")
        if self.escrow_pool_atto < amount:
            # Defensive: should be impossible while accounting is consistent.
            raise gl.vm.UserError("ESCROW: contract balance too low")
        self.withdrawable_atto[key] = 0
        self.escrow_pool_atto = self.escrow_pool_atto - amount
        self.next_nonce = self.next_nonce + 1
        # Native payout: emit a value transfer to the caller, executed after
        # this transaction finalizes (the runner's transfer primitive; there
        # is no synchronous gl.user_transfer in the pinned SDK).
        gl.get_contract_at(gl.message.sender_address).emit_transfer(
            value=amount, on="finalized"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, agreement_id: str) -> Agreement:
        ag = self.agreements.get(agreement_id)
        if ag is None:
            raise gl.vm.UserError(f"ESCROW: unknown agreement '{agreement_id}'")
        return ag

    # ------------------------------------------------------------------
    # View methods
    # ------------------------------------------------------------------

    @gl.public.view
    def get_agreement(self, agreement_id: str) -> dict:
        """Full public state of one agreement."""
        ag = self._get(agreement_id)
        return {
            "id": ag.id,
            "client": str(ag.client),
            "freelancer": str(ag.freelancer),
            "title": ag.title,
            "spec": ag.spec,
            "escrow_atto": ag.escrow_atto,
            "delivered_work": ag.delivered_work,
            "delivery_url": ag.delivery_url,
            "status": ag.status,
            "dispute_reason": ag.dispute_reason,
            "judgment_decision": ag.judgment_decision,
            "judgment_percent": ag.judgment_percent,
            "judgment_analysis": ag.judgment_analysis,
            "created_at": ag.created_at,
            "funded_at": ag.funded_at,
            "delivered_at": ag.delivered_at,
            "disputed_at": ag.disputed_at,
            "resolved_at": ag.resolved_at,
            "appeal_count": ag.appeal_count,
            "arbitration_nonce": ag.arbitration_nonce,
            "arbitration_history": list(ag.arbitration_history),
        }

    @gl.public.view
    def list_agreements(self) -> list:
        return list(self.agreement_ids)

    @gl.public.view
    def get_withdrawable(self, account: Address) -> int:
        amount = self.withdrawable_atto.get(_account_key(account))
        if amount is None:
            return 0
        return amount

    @gl.public.view
    def get_escrow_pool(self) -> int:
        return self.escrow_pool_atto
