# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""
Freelance Escrow with AI Consensus Arbitration - GenLayer Intelligent Contract.

A client deposits native-token funds into escrow for a freelancer to complete a
defined task described by a written spec. On delivery, either party can raise a
dispute. When disputed, GenLayer validators INDEPENDENTLY evaluate the delivered
work against the original spec using LLM-based comparative judgment, and the
contract releases funds (full release to freelancer, full refund to client, or
a percentage split) based on VALIDATOR CONSENSUS - not a single party's claim
and not a single leader's LLM answer.

Why GenLayer consensus is necessary here
-----------------------------------------
The arbitration decision directly moves real money (an atto-scale u256 payout
split). An LLM judgment is non-deterministic external input: if we trusted one
leader's LLM output, the leader could rug the escrow and no one would notice.
So every arbitration goes through leader + validator re-execution of the SAME
evaluation with a field-level agreement rule (see `_arbitrate` below), which is
exactly what GenLayer's equivalence-principle machinery is for.

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

# Split-ratio agreement tolerance, in integer percentage points. Two
# independent LLM runs of the *same* arbitration prompt can legitimately
# land on 70% vs 65% for the freelancer; demanding bit-exact equality on the
# ratio would deadlock consensus, while accepting wildly different ratios
# would let a bad leader through. 15 points is the pragmatic band - the
# *decision label* (release/refund/split) must still match EXACTLY.
SPLIT_TOLERANCE_PERCENT = 15

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
    judgment_decision: str       # "release" | "refund" | "split" | "none"
    judgment_percent: u256       # percent of escrow awarded to the freelancer (0..100)
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


def _clamp_percent(raw) -> int:
    """Coerce an LLM-supplied percent to an integer in 0..100."""
    try:
        pct = int(round(float(str(raw).strip())))
    except (ValueError, TypeError):
        raise gl.vm.UserError(f"{ERROR_LLM} Non-numeric percent: {raw!r}")
    if pct < 0:
        pct = 0
    if pct > 100:
        pct = 100
    return pct


def _normalize_judgment(raw) -> dict:
    """Defensively parse and CANONICALIZE the LLM arbitration output.

    Why normalization exists (equivalence-principle context):
    Validators compare *decision fields* of independently produced LLM answers.
    LLMs use alternate key names and sometimes emit verdicts inconsistent with
    their own percent (e.g. decision="split", percent=100). By forcing the
    output through one deterministic normalization funnel, the fields the
    validator compares (decision + percent) become a pure function of the
    model's semantic intent - so two near-identical answers normalize to
    identical fields, and only genuinely different judgments diverge.
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

    percent_raw = lowered.get("freelancer_percent")
    if percent_raw is None:
        for alt in ("percent", "split_percent", "freelancer_share",
                    "percent_to_freelancer", "share"):
            if alt in lowered:
                percent_raw = lowered[alt]
                break
    if percent_raw is None:
        # A missing percent is harmless for the two extreme verdicts, whose
        # percent is implied (100 / 0); for a split it defaults to a neutral
        # middle, which canonicalization below may still adjust.
        percent_raw = 50

    decision = str(decision_raw).strip().lower()
    # Map common synonyms onto the three canonical decisions.
    if decision in ("release", "released", "full_release", "pay", "fulfilled", "complete"):
        decision = "release"
    elif decision in ("refund", "refunded", "full_refund", "reject", "rejected", "failed"):
        decision = "refund"
    elif decision in ("split", "partial", "partial_release", "partial_refund", "mixed"):
        decision = "split"
    else:
        raise gl.vm.UserError(f"{ERROR_LLM} Unknown decision: {decision_raw!r}")

    pct = _clamp_percent(percent_raw)

    # Canonicalize: decision and percent must describe the same economics.
    # release => pct is 100, refund => pct is 0, split => strictly between.
    if decision == "release":
        pct = 100
    elif decision == "refund":
        pct = 0
    else:  # split
        if pct <= 0:
            decision, pct = "refund", 0
        elif pct >= 100:
            decision, pct = "release", 100

    analysis = str(lowered.get("analysis") or lowered.get("reasoning") or lowered.get("reason") or "")
    if len(analysis) > 2000:
        analysis = analysis[:2000]

    return {"decision": decision, "percent": pct, "analysis": analysis}


def _run_arbitration_prompt(title: str, spec: str, delivered_work: str,
                            dispute_reason: str) -> dict:
    """LEADER-SIDE function: run the LLM arbitration and return normalized fields.

    This is deliberately a *pure function of its arguments*: the exact same
    arguments produce the exact same prompt string. Validators re-execute this
    very function themselves with the same inputs - that is the only way they
    can form an INDEPENDENT candidate judgment rather than trusting the leader.
    """
    prompt = (
        "You are an impartial arbitration expert for a freelance escrow platform. "
        "Evaluate the delivered work strictly against the agreed written spec and "
        "decide how the escrowed funds should be split.\n\n"
        f"TASK TITLE:\n{title}\n\n"
        f"WRITTEN SPEC (the binding agreement):\n{spec}\n\n"
        f"DELIVERED WORK (text or link provided by the freelancer):\n{delivered_work}\n\n"
        f"DISPUTE REASON RAISED BY A PARTY (claim only, not evidence):\n{dispute_reason}\n\n"
        "Judge ONLY on how well the delivered work satisfies the spec. Treat party "
        "claims skeptically. Decide one of:\n"
        "- 'release': the work substantially fulfills the spec (freelancer gets 100%)\n"
        "- 'refund': the work fails to fulfill the spec (client gets 100% back)\n"
        "- 'split': the work partially fulfills the spec (freelancer gets "
        "freelancer_percent, client gets the rest; 0 < freelancer_percent < 100)\n\n"
        "Respond with ONLY a JSON object of this exact shape:\n"
        '{"decision": "release" | "refund" | "split", '
        '"freelancer_percent": <integer 0-100>, '
        '"analysis": "<short objective explanation>"}'
    )
    raw = gl.nondet.exec_prompt(prompt, response_format="json")
    return _normalize_judgment(raw)


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
        """Freelancer submits the deliverable (text, description, or link)."""
        ag = self._get(agreement_id)
        if gl.message.sender_address != ag.freelancer:
            raise gl.vm.UserError("ESCROW: only the freelancer can submit delivery")
        if ag.status != STATUS_FUNDED:
            raise gl.vm.UserError(f"ESCROW: cannot deliver in status '{ag.status}'")
        if delivered_work == "":
            raise gl.vm.UserError("ESCROW: delivered_work must not be empty")

        ag.delivered_work = delivered_work
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

        ag.judgment_decision = "release"
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

        judgment = self._arbitrate(ag.title, ag.spec, ag.delivered_work, ag.dispute_reason)

        ag.judgment_decision = judgment["decision"]
        ag.judgment_percent = judgment["percent"]
        ag.judgment_analysis = judgment["analysis"]
        ag.arbitration_nonce = ag.arbitration_nonce + 1
        ag.resolved_at = _now_iso()
        ag.arbitration_history.append(json.dumps({
            "nonce": ag.arbitration_nonce,
            "decision": judgment["decision"],
            "percent": judgment["percent"],
            "analysis": judgment["analysis"],
            "at": ag.resolved_at,
        }))
        ag.status = STATUS_ARBITRATED
        self.agreements[agreement_id] = ag

    def _arbitrate(self, title: str, spec: str, delivered_work: str,
                   dispute_reason: str) -> dict:
        """Consensus-wrapped LLM arbitration using a CUSTOM comparative validator.

        WHY this shape - the equivalence principle, in its own words:
        1. `strict_eq` is impossible here: two byte-identical LLM calls can
           return different text, so exact equality would deadlock consensus.
        2. Validating only the LEADER's output (is it JSON? is the decision a
           known string?) is NOT consensus - the leader alone would be
           deciding who gets the money, and any confidently-worded wrong (or
           bribed) answer would pass. That pattern is explicitly rejected.
        3. So each VALIDATOR independently re-runs the very same
           `_run_arbitration_prompt(...)` - same prompt, same inputs taken
           from on-chain state, which every validator reads identically - and
           we compare only the DECISION FIELDS of the two independent
           judgments:
             * `decision` must match EXACTLY (release/refund/split), and
             * the numeric `percent` must agree within SPLIT_TOLERANCE_PERCENT
               because a ratio is a continuum where small LLM wobble is
               semantically harmless - the tolerance is explicit, integer,
               and applied AFTER canonicalization.
        4. If the leader's call errored or its output could not be parsed
           (parsed output never leaves `_run_arbitration_prompt` - it raises
           ERROR_LLM instead), the validator DISAGREES by returning False,
           which forces leader rotation instead of quietly accepting a broken
           settlement path.
        """

        def leader_fn():
            return _run_arbitration_prompt(title, spec, delivered_work, dispute_reason)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            # Broken/non-return leader results never get the benefit of the doubt.
            if not isinstance(leaders_res, gl.vm.Return):
                return _handle_leader_error(leaders_res, leader_fn)
            try:
                validator_res = leader_fn()
            except Exception:
                # Validator's own independent run failed -> cannot compare ->
                # disagree and rotate the leader.
                return False
            leader_decision = leaders_res.calldata["decision"]
            validator_decision = validator_res["decision"]
            if leader_decision != validator_decision:
                return False
            leader_pct = leaders_res.calldata["percent"]
            validator_pct = validator_res["percent"]
            diff = leader_pct - validator_pct
            if diff < 0:
                diff = -diff
            return diff <= SPLIT_TOLERANCE_PERCENT

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
        if ag.judgment_decision == "release" or percent == 100:
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
