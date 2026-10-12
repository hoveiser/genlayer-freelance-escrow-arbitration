# genlayer-freelance-escrow-arbitration

A standalone **GenLayer Intelligent Contract**: a freelance escrow in which
disputes are settled by **LLM-based arbitration validated by GenLayer validator
consensus** - not by either party's claim and not by a single leader's model
output - and in which the escrow can never be locked forever: every agreement
carries an **enforceable delivery deadline** with a **client-side reclaim**.

Submitter entry for GenLayer's builder program, "Intelligent Contract"
category. Contract-only submission (no frontend/backend).

**Live evidence** (full details in
[Studionet deployment & evidence](#studionet-deployment--evidence)):

- Current contract (delivery-deadline revision): `0x06004A0D77F91d1d4691403595c185173Dd4a6F4`
  - Deploy tx: [`0x61a7a0722c47e230242503ce6791e8991b57a687a1132e505aa86b749d495076`](https://explorer-studio.genlayer.com/tx/0x61a7a0722c47e230242503ce6791e8991b57a687a1132e505aa86b749d495076) (`MAJORITY_AGREE`, consensus-finalized)
- Previous revision (fail-closed arbitration): `0x5EadC908deb2dc5Ad80a65f03105e4Ee9620EbFD`
- Explorer: <https://explorer-studio.genlayer.com/>

## The use case

1. A **client** drafts an agreement with a written spec, a deal amount, and a
   **delivery window** (bounded: at least 1 hour, at most 90 days), then
   **funds the escrow** with native tokens (atto-scale `u256`). Funding starts
   the clock: `delivery_deadline_ts = fund time + window`.
2. The **freelancer** submits the delivered work (text, description, or link)
   - but only **at or before the deadline**; late submissions are refused.
3. Before any delivery exists, either side has a defined exit:
   - the client can **cancel** a never-funded agreement (no funds involved),
   - the freelancer can **decline** a funded agreement before the deadline
     (full refund to the client - a path that can only ever favor the client),
   - after the deadline passes with no delivery, the client can
     **reclaim_undelivered** and recover the FULL escrow.
4. Once delivered, either:
   - the client **approves** - funds are released in full, or
   - either party **raises a dispute** - the contract runs **on-chain
     arbitration**: every evaluator independently FETCHES the delivered
     artifact and an LLM grades that fetched evidence against the original
     spec; if the delivery carried **no URL to fetch, the contract fails closed
     and refunds in full without ever consulting the LLM**. The outcome must be
     exactly one of five fixed settlement labels (`RELEASE_FULL` / `REFUND_FULL`
     / `SPLIT_QUARTER` / `SPLIT_HALF` / `SPLIT_THREE_QUARTER`), adopted only if
     GenLayer **validators reach consensus on the identical label**.
5. A party that disagrees with the outcome may **appeal exactly once**
   (re-triggers the full consensus evaluation). After settlement each party
   **withdraws** its share as a native-token transfer.

## Why GenLayer consensus is necessary

The arbitration decision moves real money. The input to the decision is an LLM
judgment - a non-deterministic, external, subjective signal that a
deterministic smart contract cannot evaluate and that a single trusted party
should not be allowed to author. If the contract merely stored whatever one
leader's LLM call returned, the leader (or anyone who can influence the leader's
model output) could unilaterally decide escrow outcomes. GenLayer is what makes
the settlement decision **reproducible enough for multiple independent
validators to agree on it**, while still allowing AI-mediated subjective
judgment.

The deadline machinery, by contrast, is deliberately **deterministic**: it is
derived from the consensus-signed transaction message (never a wall clock), so
every validator computes identical deadlines and no LLM is involved. It still
runs under full consensus because every state transition in this contract moves
or protects money.

## How the equivalence principle works here

The consensus-critical call is `_arbitrate()` in
[contracts/freelance_escrow.py](contracts/freelance_escrow.py). Design
decisions, in order:

- **`strict_eq` over the raw result is impossible.** Two byte-identical
  executions of the same LLM prompt / web fetch can return different text;
  requiring exact equality of raw outputs would deadlock consensus forever.
  Equivalence is therefore enforced on a **canonical, discrete result**, not on
  raw text.
- **Fixed, enumerable outcome set - no tolerance, anywhere.** The LLM must pick
  exactly one of five labels: `RELEASE_FULL` (100%), `REFUND_FULL` (0%),
  `SPLIT_QUARTER` (25%), `SPLIT_HALF` (50%), `SPLIT_THREE_QUARTER` (75%). The
  payout percent is **derived from the label** by a deterministic function
  (`_percent_for_decision`) and is **never parsed from LLM output** - any
  `percent`-like number the model invents is ignored. Because money is a pure
  function of the label, exact agreement on the label IS exact agreement on the
  transfer. (An early prototype had a +/-15 percentage-point tolerance; it was
  removed on purpose: "close enough" is not a valid basis for moving funds.)
- **Leader-output-only validation is rejected.** A validator that merely checks
  the leader's answer "is well-formed JSON with an allowed status" proves
  formatting, not agreement - the leader alone would be deciding who gets the
  money. This contract does not do that.
- **Genuine comparative validation** (custom validator function via
  `gl.vm.run_nondet_unsafe`):
  1. The leader runs `_run_arbitration_prompt(title, spec, work, url,
     dispute_reason)` - a pure function of on-chain state: it performs its own
     `gl.nondet.web.get` fetch of the delivery URL and its own
     `gl.nondet.exec_prompt` LLM call.
  2. The output is pushed through a deterministic **normalization funnel** that
     accepts ONLY the five fixed labels (after formatting-only case / space /
     hyphen normalization and key aliasing). Anything else raises `[LLM_ERROR]`.
  3. **Each validator independently re-runs that entire function - its own
     fetch, its own LLM call - and the validator rule requires an EXACT match**
     on both economic fields: `decision` (label) and `evidence_status`
     (`fetched` / `unreachable` / `no_url_provided`). Any mismatch returns
     `False` - leader rotation. There is no numeric band, no rounding
     allowance, no "similar enough".
  4. Malformed leader output (label outside the fixed set) is an automatic
     disagreement; a leader error is handled by `_handle_leader_error`, which
     re-runs the task rather than granting the doubt. Both force rotation.
- Money never touches the non-deterministic path: payouts are computed
  deterministically from the consensus-agreed label
  (`escrow_atto * percent // 100`, percent in {0, 25, 50, 75, 100}).

Every one of these rules is annotated with inline `WHY` comments at the point in
the code where it applies.

## Evidence acquisition and the refund-by-default policy

A submitted delivery description or link is **claim, not evidence**. The
contract does not take it on faith:

- `submit_delivery` deterministically extracts the first `http(s)://` URL from
  the delivery text into `delivery_url` (stored, viewable).
- During arbitration, **every evaluator independently fetches that URL** inside
  its own run (`_fetch_evidence` - `gl.nondet.web.get`). The leader never hands
  a fetched copy to validators, and validators never trust the submitter's
  self-description over the retrieved content. The fetched body (capped at
  `MAX_EVIDENCE_CHARS = 8000`) is embedded in the LLM prompt and explicitly
  weighted above the description.
- **Fetch failure is explicit, never a silent pass.** A non-200 response or a
  raised exception makes the evaluator return the deterministic fallback
  `{decision: REFUND_FULL, evidence_status: "unreachable"}` **without consulting
  the LLM**. If the leader cannot fetch but a validator can, the two disagree,
  leader rotates, and the fetch is retried. Only when the whole ring genuinely
  cannot retrieve the artifact do all evaluators independently converge on
  `REFUND_FULL` - money returns to the client, because **unverifiable work must
  not pay out**. (Volatile pages can cause repeated disagreement; that is
  fail-safe by design: while evaluators cannot agree on what the artifact is,
  no funds move.)
- **Deliveries with no URL FAIL CLOSED to a full refund.** A delivery that is
  only self-described text gives every evaluator nothing external to fetch - the
  only "evidence" would be the freelancer's own words. The contract
  short-circuits **before any LLM prompt or web fetch**:
  `_run_arbitration_prompt` returns `{decision: REFUND_FULL, evidence_status:
  "no_url_provided"}` deterministically. Because the branch depends only on the
  empty on-chain `delivery_url`, leader and every validator independently reach
  the same result, so `REFUND_FULL` is genuine consensus, never a pass. **This
  replaces the earlier lower-assurance "description-only" path, which could
  still release or split funds on the freelancer's say-so.**
- **To be paid, a freelancer must supply a fetchable URL** (public repo,
  deployed app, shared doc - any host the client can reach). The escrow only
  pays against evidence each validator can independently retrieve.

## Delivery deadline and client exits (reviewer-driven)

Earlier feedback: after `fund_escrow` the only exit was `submit_delivery`, so a
freelancer who never delivered locked the escrow forever. The lifecycle now has
an enforceable deadline and three defined exits, all deterministic (no LLM):

| Situation | Exit | Effect |
|---|---|---|
| drafted, never funded | `cancel_agreement` (client) | status `cancelled`, no funds ever moved |
| funded, freelancer declines before deadline | `decline_agreement` (freelancer) | FULL escrow credited to client (client-favoring only) |
| funded, deadline passed, nothing delivered | `reclaim_undelivered` (client) | FULL escrow credited to client, freelancer credit stays 0, status `refunded` |
| delivered on time | `approve_delivery` or dispute - arbitration | existing exact-label settlement, unchanged |

Rules that make the deadline real:

- **Window validated at entry, both bounds.** `create_agreement` takes
  `delivery_window_seconds` and rejects anything below 1 hour
  (`MIN_DELIVERY_WINDOW_SECONDS`) or above 90 days
  (`MAX_DELIVERY_WINDOW_SECONDS`). An adversarial client cannot craft a
  near-zero window and reclaim minutes after funding.
- **Deterministic clock, no wall time.** `_now_ts()` derives unix seconds from
  the consensus-signed message datetime (`gl.message_raw["datetime"]`) using
  pure string operations and integer calendar math (`_days_from_civil`, verified
  against stdlib epoch anchors including century leap-year edges). Every
  validator computes the identical value from the same signed message. The live
  runtime format was verified against studionet receipts (ISO-8601 UTC; both
  `...Z` and `+00:00` forms are handled, fractional seconds truncated).
- **Deadline starts at funding**: `fund_escrow` sets
  `delivery_deadline_ts = _now_ts() + delivery_window_seconds` (0 before).
- **No early recovery**: `reclaim_undelivered` requires
  `_now_ts() > delivery_deadline_ts` (strictly). At exactly the deadline second
  the client still cannot reclaim.
- **No late delivery**: `submit_delivery` requires
  `_now_ts() <= delivery_deadline_ts`. At exactly the deadline the freelancer
  still CAN deliver.
- **Race analysis** (the two boundaries are exact complements over the same
  stored integer and the same deterministic message clock): a transaction
  cannot satisfy both `ts <= deadline` (deliver) and `ts > deadline` (reclaim);
  within one block the VM orders transactions serially, and whichever lands
  first moves the status out of `funded`, so the other is additionally refused
  by its status precheck. Double release is impossible by BOTH timestamp and
  status, not by either alone. The same status precheck makes
  `reclaim_undelivered` impossible after any delivery and exactly once - see
  the settlement guard below.
- **`decline_agreement` is open only before the deadline** and always credits
  the client in full - the freelancer can never use it to extract funds.

## Accounting invariants

Every credit path funnels through `_credit_out`, which enforces:

- **No double settlement**: an agreement may be credited exactly once - the
  call reverts unless `client_credit_atto + freelancer_credit_atto == 0` and
  the status is not already final (`settled` / `released` / `refunded` /
  `cancelled`). Per-agreement credit fields stay non-zero for life, so a
  forged second call cannot pass either.
- **All-or-nothing split**: `freelancer_atto + client_atto == escrow_atto` by
  construction (integer division remainder always goes to the client).
- **Conservation view**: `get_accounting()` returns `escrow_pool_atto`, the sum
  of unwithdrawn credits, and the sum of escrow locked in funded-but-uncredited
  agreements. While accounting is consistent the identity
  `escrow_pool == unwithdrawn_credits + locked_escrow` holds after EVERY state
  transition (settled money leaves "locked" and enters "unwithdrawn" in the
  same transaction; withdraw moves both sides down together). The direct tests
  assert this after every step of several mixed scenarios, including the appeal
  flow and the reclaim flow.

## Storage schema

Typed persistent fields only - `TreeMap` / `DynArray`, never raw `dict` /
`list`. Status is stored as `str`, never as an `Enum`. All amounts are `u256`
in **atto scale** (`value * 10**18`); no floats anywhere in consensus-relevant
code.

```
FreelanceEscrow (gl.Contract)
+-- agreements:        TreeMap[str, Agreement]    # id -> agreement
+-- agreement_ids:     DynArray[str]              # enumeration order
+-- escrow_pool_atto:  u256                       # native tokens held
+-- withdrawable_atto: TreeMap[str, u256]          # account-key (hex) -> credit
+-- next_nonce:        u256                       # anti-replay / ordering

Agreement (@allow_storage @dataclass)   # new fields may ONLY be appended
+-- id, client: Address, freelancer: Address, title, spec
+-- escrow_atto: u256                   # atto scale
+-- delivered_work: str                 # text/link submitted by freelancer
+-- status: str                         # created|funded|delivered|disputed|
|                                       # arbitrated|settled|released|refunded|cancelled
+-- dispute_reason: str
+-- judgment_decision: str              # RELEASE_FULL|REFUND_FULL|SPLIT_QUARTER|
|                                       # SPLIT_HALF|SPLIT_THREE_QUARTER|none
+-- judgment_percent: u256              # DERIVED from the label (0/25/50/75/100)
+-- judgment_analysis: str
+-- created_at/funded_at/delivered_at/disputed_at/resolved_at: str
|                                       # from consensus message datetime
+-- appeal_count: u256                  # max 1
+-- arbitration_nonce: u256
+-- arbitration_history: DynArray[str]  # json.dumps of every judgment/reclaim
|                                       # record, kept after appeal
+-- client_credit_atto / freelancer_credit_atto: u256
+-- delivery_url: str                   # APPENDED earlier: first http(s) URL
|                                       # extracted from delivered_work
+-- delivery_window_seconds: u256       # APPENDED: bounded 1h..90d at creation
+-- delivery_deadline_ts: u256          # APPENDED: unix seconds, 0 until funded
```

Lifecycle:

```
created --fund--> funded --deliver (ts <= deadline)--> delivered
   |                 |                    |                 +--approve--> released
   |                 |                    |                 +--dispute--> disputed
   |                 |                    |                       --> arbitrated --+
   |                 |                    |                             (appeal x1) |
   |                 |                    |                     settled/released/refunded
   |                 |                    +--(late submit rejected, ts > deadline)
   |                 +--reclaim_undelivered (ts > deadline, no delivery)--> refunded
   |                 +--decline_agreement (freelancer, ts <= deadline)--> refunded
   +--cancel_agreement--> cancelled        (no funds ever moved)

released/settled/refunded credits are claimable via withdraw()
```

## Public methods

| Method | Kind | Access rule |
|---|---|---|
| `create_agreement(id, freelancer, title, spec, escrow_amount_atto, delivery_window_seconds)` | write | caller becomes client; window bounded 1h..90d at entry |
| `fund_escrow(id)` | write | client only; `gl.message.value` must equal escrow exactly; sets `delivery_deadline_ts` |
| `submit_delivery(id, delivered_work)` | write | freelancer only; only in `funded`; rejected when `_now_ts() > delivery_deadline_ts`; first URL becomes independently fetchable evidence |
| `approve_delivery(id)` | write | client only, delivered only - full release |
| `raise_dispute(id, reason)` | write | client or freelancer |
| `resolve_dispute(id)` | write | client or freelancer; second call = appeal (max 1) |
| `accept_resolution(id)` | write | client or freelancer; credits payout via the guarded `_credit_out` |
| `cancel_agreement(id)` | write | client only, `created` only; no funds move; status `cancelled` |
| `decline_agreement(id)` | write | freelancer only, `funded`, before deadline; full refund to client |
| `reclaim_undelivered(id)` | write | client only, `funded`, strictly after deadline; full escrow back to client |
| `withdraw()` | write | anyone with a credit; emits native transfer |
| `get_agreement(id)` | view | full state incl. `delivery_window_seconds`, `delivery_deadline_ts` |
| `list_agreements()` / `get_withdrawable(addr)` / `get_escrow_pool()` | view | - |
| `get_accounting()` | view | pool / unwithdrawn credits / locked escrow + `invariant_holds` |

All violations raise `gl.vm.UserError` (the `gl.UserError` of this SDK
namespace) with `ESCROW:`-prefixed messages - never bare `Exception`.

## Repository layout

```
contracts/freelance_escrow.py      # the contract (pinned runner header, line 1)
tests/direct/                      # fast Direct-mode unit tests (no server)
tests/integration/                 # full-consensus tests via gltest
gltest.config.yaml                 # integration-test network/account config
requirements.txt                   # genlayer-test + genvm-linter + pytest
scripts/setup_direct_test_cache.sh # one-time local cache fix (see below)
scripts/studionet_e2e_demo.py      # drives the live URL + no-URL lifecycles
scripts/studionet_reclaim_demo.py  # drives the live deadline/reclaim lifecycle
scripts/deploy_studionet.py        # persistent studionet deployment
scripts/verify_deployed_source.py  # repo-vs-deployed source comparison
scripts/probe_source_retrieval.py  # diagnostic for the source-retrieval RPC
.github/workflows/escrow-ci.yml    # CI: genvm-lint + direct tests on every push
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Requires Python 3.12+.

## Linting

```bash
genvm-lint check contracts/freelance_escrow.py
```

Must report `ok` before anything else is trusted. (`genvm-lint check --json` for
machine-readable output.)

## Direct-mode tests (fast, no server)

```bash
scripts/setup_direct_test_cache.sh   # one-time; see note below
pytest tests/direct/ -v
```

**Coverage (48 tests):** happy path with no dispute; dispute to full release,
full refund, and each split label with its exact 25/50/75% payout;
unauthorized-sender rejections for every role; the appeal path (one
re-trigger, second rejected); state-machine guards; value validation;
deterministic URL extraction; **evidence fetching** (fetched content proven to
reach the prompt; 404/500 produce deterministic `REFUND_FULL` fallback without
the LLM being consulted); **fail-closed no-URL handling** (a delivery with no
URL, and a whitespace-only URL, refunds with `evidence_status:
"no_url_provided"` and never reaches the LLM, proven by mocks that would have
said otherwise); fixed-set output strictness (unknown labels and unparseable
output revert; LLM-supplied percents are ignored); **the deadline suite**:
window floor/ceiling rejection at the exact bounds, deadline computed from the
funding message epoch, late `submit_delivery` rejected at deadline+1 and
accepted at deadline exactly, `reclaim_undelivered` rejected before and at the
deadline, rejected for non-clients, rejected after delivery, rejected twice,
successful only strictly after the deadline with FULL credit to the client and
zero to the freelancer, `cancel_agreement` only in `created` (terminal, blocks
later funding), `decline_agreement` by the freelancer before the deadline only,
post-reclaim `withdraw` returning the pool to exactly 0, plus unit tests that
pin the epoch math (`_days_from_civil` against stdlib anchors incl. 2000/2100
leap-year edges, `_now_ts` against both message-datetime formats); and
`get_accounting()` conservation asserted after **every** step of several mixed
scenarios (happy lifecycle, dispute + appeal + partial withdrawals, reclaim +
parallel agreement + withdrawals).

> **Important caveat (also in the test file headers):** Direct mode executes
> only the **leader** path of `run_nondet_unsafe`. The comparative validator -
> independent fetch + LLM re-execution and the EXACT label/evidence match (no
> tolerance) - is **not** exercised by these tests. Consensus behavior is
> covered by the integration tests below.

> **Clock control in direct tests:** the direct-mode runner reads the message
> datetime from the VM context, so `direct_vm.warp()` pins contract-visible
> time deterministically (the same mechanism the deadline math uses on-chain:
> a message-derived clock, never a wall clock).

> **Cache note:** `genlayer-test`'s direct runner resolves the "latest" genvm
> release and looks for an unversioned `genvm-universal.tar.xz` asset; the
> current release ships it with a versioned filename, which yields an HTTP 404.
> `scripts/setup_direct_test_cache.sh` seeds the local cache from the bundle
> `genvm-lint` already downloaded, which contains the pinned runner this
> contract depends on.

## Integration tests (full consensus)

```bash
# Option A - hosted Studio (gasless, rate-limited 60 req/min per IP): put two
# account private keys in gltest.config.yaml (studionet section), then
gltest tests/integration/ -v -s --network studionet

# Option B - Testnet Bradbury (fund both accounts via
# https://testnet-faucet.genlayer.foundation/ and put their keys in
# gltest.config.yaml)
gltest tests/integration/ -v -s --network testnet_bradbury

# Option C - local Studio (`genlayer up`): full GenVM, Docker required.
```

> **GLSim caveat:** `glsim` (genlayer-test 0.29.x) is **not** a usable path for
> this contract: deploying the pinned runner generation either rejects the
> contract class (`@allow_storage` errors) or extracts an empty schema, because
> the simulator does not run the real GenVM runner. Use studionet / testnet /
> local Studio instead.

[tests/integration/test_escrow_consensus.py](tests/integration/test_escrow_consensus.py)
drives the real flow with **two different signing accounts** (client and
freelancer) and asserts:

1. `resolve_dispute` passes **full leader + validator consensus** - acceptance
   implies independently-executing validators each fetched the delivery URL,
   re-ran the LLM prompt, and agreed on the **exact same** settlement label and
   evidence status (there is no tolerance band left to agree "within"), and
2. the **appeal** re-triggers consensus exactly once, the second appeal
   transaction fails execution, and settlement splits per the appealed
   judgment, and
3. a **no-URL delivery fails closed**: `resolve_dispute` on a description-only
   delivery settles `REFUND_FULL` with `evidence_status: "no_url_provided"`
   through full validator consensus, without any evaluator ever reaching the
   LLM prompt or a URL fetch, and
4. the **reclaim path through real consensus and a real clock**: at the
   production 1-hour window floor (NOT weakened for tests), early reclaim
   reverts on-chain, late delivery reverts after the deadline, reclaim then
   succeeds crediting the full escrow to the client with the freelancer at 0,
   a second reclaim reverts, and `get_accounting()` reports the conservation
   identity holding on-chain. Because it waits out a real hour this test runs
   ~1h+consensus (skip switch: `ESCROW_RECLAIM_DEADLINE_WAIT`).

> **Live verification status:** all four integration scenarios were executed
> against **studio.genlayer.com** with real 5-validator consensus (see the
> deployment section for tx hashes). Deploy, schema pull, and every write
> method executed through full consensus on the pinned runner.

## Studionet deployment and evidence

**Network:** studionet - "Genlayer Studio Network", chainId **61999**, RPC
`https://studio.genlayer.com/api` (explorer:
<https://explorer-studio.genlayer.com/>)

**Accounts used (both real, distinct signers):**

| Role | Address | Signs |
|---|---|---|
| Client | `0x3de43AA2f7162c80af98abe78222aE0Cdf83c506` | create / fund / dispute / resolve / accept / reclaim / withdraw |
| Freelancer | `0x4322029253822d4EA1A72f83a3bFaa2F55993d01` | submit_delivery / decline |

The client key was supplied only via the `GENLAYER_PRIVATE_KEY` environment
variable (`.env`, git-ignored - see [.gitignore](.gitignore)); it was never
hardcoded, logged, or committed. Value-bearing calls sent real GEN transfers
(`fund_escrow` = 3-5 GEN at atto scale).

**Deployments (every contract revision changes the storage layout, so each is
a NEW address):**

| Revision | Address | Deploy tx | Consensus |
|---|---|---|---|
| Current, deployed from the frozen commit (deadline + exits) | `0x148c670A41cFEF2BA6218219d1Fe7addfEE92A1F` | [`0x7831833a1fff6d7bec2348b36b7074fe62c2d79effa3a4d192f956a59de54cdf`](https://explorer-studio.genlayer.com/tx/0x7831833a1fff6d7bec2348b36b7074fe62c2d79effa3a4d192f956a59de54cdf) | `MAJORITY_AGREE` |
| Validation copy of the identical source (pre-freeze deploy used to prove scripts and flows) | `0x06004A0D77F91d1d4691403595c185173Dd4a6F4` | [`0x61a7a0722c47e230242503ce6791e8991b57a687a1132e505aa86b749d495076`](https://explorer-studio.genlayer.com/tx/0x61a7a0722c47e230242503ce6791e8991b57a687a1132e505aa86b749d495076) | `MAJORITY_AGREE` |
| Previous (fail-closed arbitration) | `0x5EadC908deb2dc5Ad80a65f03105e4Ee9620EbFD` | [`0xcf65806dc8e5a51695fc307ec76336336ef904b1d6a7e6ab642de947fe3b067e`](https://explorer-studio.genlayer.com/tx/0xcf65806dc8e5a51695fc307ec76336336ef904b1d6a7e6ab642de947fe3b067e) | `MAJORITY_AGREE` |

The validation copy is byte-identical to the frozen commit (`sha256
2566d5d39f9b2366b71efe953db410edea50f08e11d6c727eb03dd94154bfdb1`, verified
with `scripts/verify_deployed_source.py` against both deploy txs), so the
evidence recorded below for it applies to exactly the code that runs on the
primary address.

**Reclaim lifecycle evidence on the primary deployment** (driven by
[scripts/studionet_reclaim_demo.py](scripts/studionet_reclaim_demo.py), all
through full consensus; every tx re-verified via the explorer JSON API, not
just CLI output):

| Step | Explorer tx | Verified on-chain outcome (explorer JSON API) |
|---|---|---|
| create_agreement (window = 3600s, the production floor) | [`0x932624b9...`](https://explorer-studio.genlayer.com/tx/0x932624b947363a401c1fad87e37d5bd4cdb44d43334c992c20e60b4c0b52b4de) | `FINALIZED` / `MAJORITY_AGREE` / executed |
| fund_escrow (3 GEN at atto; sets `delivery_deadline_ts`) | [`0x1de732ea...`](https://explorer-studio.genlayer.com/tx/0x1de732eabcddd43a2e94aa8e0a87ab6be541da0d42638ad1ee7d557043f9632f) | `FINALIZED` / executed; `value = 3000000000000000000` |
| reclaim_undelivered BEFORE the deadline | [`0xec662a07...`](https://explorer-studio.genlayer.com/tx/0xec662a074c480a3606143328048f947bec1817a0b46d52543fd91192f829405e) | `FINALIZED` / reverted (early recovery refused) |
| submit_delivery AFTER the deadline (freelancer signer) | [`0x59d98f4b...`](https://explorer-studio.genlayer.com/tx/0x59d98f4bd6f8aac8cf151c2f66023989a7673579fe3e1b2f378e4961664c1bba) | `FINALIZED` / reverted (late delivery refused) |
| reclaim_undelivered after a real 1h wall-clock wait | [`0x12d178c9...`](https://explorer-studio.genlayer.com/tx/0x12d178c951d4a578f513743f8086482ec4cb03ef0d8f13386c310642e7d62552) | `FINALIZED` / executed; agreement flipped to `refunded`, `judgment_decision = REFUND_FULL`, client credited 3 GEN, freelancer 0, `invariant_holds = true` |
| second reclaim_undelivered on the same agreement | [`0x789de9ba...`](https://explorer-studio.genlayer.com/tx/0x789de9ba43522e2656c098c9903b58c433d2951849c311c4ff3e8e5aedcd1ca3) | `FINALIZED` / reverted (double settlement impossible via `_credit_out` guard) |
| withdraw (client takes the refund) | [`0xe97056ca...`](https://explorer-studio.genlayer.com/tx/0xe97056cae42f2b5fc1a0ee37141bb0c7cc64bfe8e91f68b5045c7d841b3a4850) | `FINALIZED` / executed; `escrow_pool_atto` went from `3000000000000000000` to `0` |

Each row was confirmed via POST `{jsonrpc, method:"eth_getTransactionByHash", params:[tx]}` to `https://explorer-studio.genlayer.com/api`, reading `status`, `result_name`, `value`, and the `eq_blocks_outputs` msgpack envelope (which encodes `c2 01 00` for a reverted call and `c2 80 00` for an executed one). The full lifecycle on the primary address finished with `get_accounting() = {pool: 0, unwithdrawn: 0, locked: 0, invariant_holds: true}`.

**Corroborating validation run on the byte-identical copy** (same script):

| Step | Explorer tx | On-chain outcome |
|---|---|---|
| create_agreement (1h window floor) | [`0x3b22bf7d...`](https://explorer-studio.genlayer.com/tx/0x3b22bf7d6481cd78778d744f2fa806756bb302b4047dd4901262831349958831) | FINALIZED / executed |
| fund_escrow (3 GEN, starts deadline) | [`0xf75a14cd...`](https://explorer-studio.genlayer.com/tx/0xf75a14cd3eaa914a2311fa23005b08bf58fbf7cee6517b72bd012b8c72f59ac8) | FINALIZED / executed, value 3e18 |
| reclaim_undelivered BEFORE deadline | [`0x027c0494...`](https://explorer-studio.genlayer.com/tx/0x027c0494d7d41b1fcfeca9e6688be2f740eb75c82bc700d36fca2fd27f41be69) | FINALIZED / reverted (early recovery refused) |
| submit_delivery AFTER deadline | [`0x07500d21...`](https://explorer-studio.genlayer.com/tx/0x07500d212f9ccf246ee2dfe2d78e821401f13df3a1fbed310bc7f9c7f456814b) | FINALIZED / reverted (late delivery refused) |
| reclaim_undelivered after real 1h wait | [`0x35c590ac...`](https://explorer-studio.genlayer.com/tx/0x35c590ac327e04df02041674ce2621f05c2e2fd39ad15e49059c8e3dd580c69f) | FINALIZED / executed; status refunded, client credit 3 GEN, freelancer 0, `invariant_holds: true` |
| second reclaim_undelivered | [`0xaba7731c...`](https://explorer-studio.genlayer.com/tx/0xaba7731c201450254cdb13a5aeb674661c0f58b79236e123df499df7ff83b376) | FINALIZED / reverted (double release impossible) |
| withdraw (client takes the refund) | [`0x6d8d1a62...`](https://explorer-studio.genlayer.com/tx/0x6d8d1a623b81ea2926a3edade1b7af84bdbca446ce122c8b208aaffc77164b02) | FINALIZED / executed; pool returned to 0 |

The same flow on the byte-identical source also passed as a real gltest
integration run against full validator consensus:
`test_reclaim_undelivered_live_via_consensus` (1 passed in 1:01:07, production
1h floor, not weakened for tests).

Earlier-lifecycle evidence on the previous deployment (URL arbitration and
no-URL fail-closed, unchanged in this revision) remains on-chain:
arbitration txs
[`0xb6dcf2b...`](https://explorer-studio.genlayer.com/tx/0xb6dcf2b578081c98d1d36266d821e09a87ee0ff6ef818b734ccc30a638770bca)
(exact-label consensus over evaluator-fetched content) and
[`0x8395902...`](https://explorer-studio.genlayer.com/tx/0x83959026b33431945be20ad4f79ab1bea2d26d0fe884935b18261a8b4b6a55f6)
(no-URL fail-closed refund), each FINALIZED on the explorer.

### Source provenance (submitted == deployed)

- GenLayer stores the FULL Python source inside the deploy transaction; the
  RPC exposes it at `eth_getTransactionByHash(deploy_tx).data.contract_code`
  (base64). `eth_getCode` returns 0 bytes (intelligent contracts carry source,
  not EVM bytecode).
- [scripts/verify_deployed_source.py](scripts/verify_deployed_source.py)
  decodes the deployed source from the deploy tx, recomputes its sha256 and
  compares it against the repo file, and additionally compares the deployed
  ABI (`gen_getContractSchema`) against the schema derived from the repo source
  (`gen_getContractSchemaForCode`). Anyone can re-run it read-only:

  ```bash
  python scripts/verify_deployed_source.py <ADDRESS> <DEPLOY_TX>
  ```

- The commit hash and source sha256 of the deployed revision are recorded in
  the deploy section below and in the git tag for this release.

### Frozen provenance of the deployed source

| Field | Value |
|---|---|
| Deployed contract address (primary) | `0x148c670A41cFEF2BA6218219d1Fe7addfEE92A1F` |
| Deploy tx | [`0x7831833a1fff6d7bec2348b36b7074fe62c2d79effa3a4d192f956a59de54cdf`](https://explorer-studio.genlayer.com/tx/0x7831833a1fff6d7bec2348b36b7074fe62c2d79effa3a4d192f956a59de54cdf) (FINALIZED / MAJORITY_AGREE) |
| Commit that produced the deployed source | `1b363be38f682828b84830a5c16138b618e39470` |
| Git tag on that commit | `escrow-delivery-deadline-1` |
| sha256 of `contracts/freelance_escrow.py` at that commit | `2566d5d39f9b2366b71efe953db410edea50f08e11d6c727eb03dd94154bfdb1` |
| sha256 of the source decoded from the deploy tx `data.contract_code` | `2566d5d39f9b2366b71efe953db410edea50f08e11d6c727eb03dd94154bfdb1` (identical) |
| Verification command anyone can re-run | `python scripts/verify_deployed_source.py 0x148c670A41cFEF2BA6218219d1Fe7addfEE92A1F 0x7831833a1fff6d7bec2348b36b7074fe62c2d79effa3a4d192f956a59de54cdf` (read-only, no keys) |
| Output of that command on the current repo state | `[1] byte-level source match: True / [2] schema match: True / VERIFIED` |

Subsequent documentation-only commits (e.g. the one that adds this provenance
text) do not touch `contracts/freelance_escrow.py`, so the deployed source
remains byte-identical to the file at `1b363be`.

**Honest caveat:** GenLayer's `eth_getCode(address)` returns `0x` because
intelligent contracts are stored as Python source embedded in the deploy
transaction's `data.contract_code` field, not as EVM bytecode. That is
exactly what makes the byte-level comparison above possible via the public
explorer API alone (no trusted side channel needed): anyone can re-fetch
`eth_getTransactionByHash(deploy_tx)`, base64-decode the source, hash it, and
confirm it equals the hash of the file at the tagged commit.

## Deploy (from scratch)

```bash
pip install -r requirements.txt
set -a; . ./.env; set +a            # GENLAYER_PRIVATE_KEY only
python scripts/deploy_studionet.py  # prints deploy tx + address (MAJORITY_AGREE)
```

Or with the GenLayer CLI: `genlayer deploy --contract
contracts/freelance_escrow.py` after `genlayer network set studionet`.

## Verification checklist (submission requirements)

- Line 1 is a pinned runner header - **no** `py-genlayer:test` / `:latest` /
  unversioned.
- Dispute judgment uses a **custom comparative validator**
  (`run_nondet_unsafe`): validators independently fetch the delivery URL,
  re-run the prompt, and require an **EXACT match on the fixed settlement label
  + evidence status**. **No tolerance anywhere; no `strict_eq` on raw LLM/web
  output.**
- **No leader-output-only/schema validation** anywhere on the judgment path;
  outputs outside the five fixed labels (or unparseable) make the validator
  return `False` - leader rotation.
- Delivered URLs are **fetched independently by every evaluator**
  (`gl.nondet.web.get` inside its own run - no shared fetched copy);
  unreachable artifacts fall back **by consensus** to `REFUND_FULL`
  (refund-by-default), never a silent pass.
- **Fail closed on missing evidence:** a delivery with no (or whitespace-only)
  URL returns `REFUND_FULL` with `evidence_status: "no_url_provided"` **before
  any LLM prompt or fetch** - the freelancer's own description alone can never
  release or split funds.
- **Enforceable delivery deadline**: window bounded at entry (1h..90d, both
  sides), deterministic message-derived clock (never a wall clock), late
  delivery refused at `deadline + 1s`, delivery at exactly `deadline` allowed,
  reclaim only strictly after the deadline, no exit before it; deliver/reclaim
  race closed by complementary timestamp bounds plus status prechecks.
- **Client-accessible exits**: `cancel_agreement` (pre-funding),
  `reclaim_undelivered` (post-deadline, full refund), `decline_agreement`
  (freelancer, client-favoring only). `STATUS_REFUNDED` now matches its
  documented meaning, and `cancelled` covers the never-funded case.
- **Accounting invariants**: single-settlement guard in `_credit_out`,
  `freelancer + client == escrow` per settlement, and the checkable identity
  `escrow_pool == unwithdrawn_credits + locked_escrow` via `get_accounting()`
  (asserted after every step in the direct suite and on-chain in the live
  reclaim run).
- Typed storage only (`TreeMap`/`DynArray`), `Agreement` dataclass, statuses as
  `str` (no `Enum` stored), new fields appended only.
- Money is atto-scale `u256` integer math throughout; **no float is ever used
  to represent or move funds** - settlement values come only from the integer
  label to percent mapping, and the deadline math is pure integer calendar
  arithmetic.
- Sender checks via `gl.message.sender_address` (the pinned SDK's name for the
  sender field; this SDK has no `sender_account` attribute); all rejections are
  `gl.vm.UserError` with prefixed messages - **no bare `Exception`**.
- Appeal path: exactly one re-trigger of `resolve_dispute`.
- `genvm-lint check` passes; **48 direct-mode tests** pass (deadline suite
  included); the integration suite contains a matching no-URL fail-closed test
  and a **live full-consensus reclaim test** at the production window floor.
- **CI** (`.github/workflows/escrow-ci.yml`) runs `genvm-lint check` plus the
  full direct-mode suite on every push and pull request; the live studionet
  suite is run manually because it signs real transactions and waits out a
  real 1-hour deadline.
- **Persistent studionet deployment** of this revision with early-reclaim
  revert, late-delivery revert, successful reclaim, and withdraw - all txs
  re-verified through the explorer JSON API.

## License

MIT - see [LICENSE](LICENSE).
