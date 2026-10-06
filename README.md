# genlayer-freelance-escrow-arbitration

A standalone **GenLayer Intelligent Contract**: a freelance escrow in which
disputes are settled by **LLM-based arbitration validated by GenLayer
validator consensus** — not by either party's claim and not by a single
leader's model output.

Submitter entry for GenLayer's builder program, "Intelligent Contract"
category. Contract-only submission (no frontend/backend).

**Live evidence** — the corrected (fail-closed) contract is deployed and
driven end-to-end on studionet (see
[Studionet deployment & evidence](#studionet-deployment--evidence);
explorer: <https://explorer-studio.genlayer.com/>):

- Contract: [`0x5EadC908deb2dc5Ad80a65f03105e4Ee9620EbFD`](https://explorer-studio.genlayer.com/address/0x5EadC908deb2dc5Ad80a65f03105e4Ee9620EbFD)
- Deploy tx: [`0xcf65806dc8e5a51695fc307ec76336336ef904b1d6a7e6ab642de947fe3b067e`](https://explorer-studio.genlayer.com/tx/0xcf65806dc8e5a51695fc307ec76336336ef904b1d6a7e6ab642de947fe3b067e)
- Fail-closed arbitration tx (no-URL delivery ⇒ deterministic `REFUND_FULL`, LLM never consulted): [`0x83959026b33431945be20ad4f79ab1bea2d26d0fe884935b18261a8b4b6a55f6`](https://explorer-studio.genlayer.com/tx/0x83959026b33431945be20ad4f79ab1bea2d26d0fe884935b18261a8b4b6a55f6)
- URL-fetch arbitration tx (independent fetch + exact-label LLM consensus): [`0xb6dcf2b578081c98d1d36266d821e09a87ee0ff6ef818b734ccc30a638770bca`](https://explorer-studio.genlayer.com/tx/0xb6dcf2b578081c98d1d36266d821e09a87ee0ff6ef818b734ccc30a638770bca)

## The use case

1. A **client** drafts an agreement with a written spec and a deal amount,
   then **funds the escrow** with native tokens (atto-scale `u256`).
2. The **freelancer** submits the delivered work (text, description, or link).
3. Either:
   - the client **approves** → funds are released in full, or
   - either party **raises a dispute** → the contract runs **on-chain
     arbitration**: every evaluator independently FETCHES the delivered
     artifact and an LLM grades that fetched evidence against the original
     spec — and if the delivery carried **no URL to fetch, the contract fails
     closed and refunds in full without ever consulting the LLM**. The outcome
     must be exactly
     one of five fixed settlement labels (`RELEASE_FULL` / `REFUND_FULL` /
     `SPLIT_QUARTER` / `SPLIT_HALF` / `SPLIT_THREE_QUARTER`), adopted only if
     GenLayer **validators reach consensus on the identical label**.
4. A party that disagrees with the outcome may **appeal exactly once**
   (re-triggers the full consensus evaluation). After settlement each party
   **withdraws** its share as a native-token transfer.

## Why GenLayer consensus is necessary

The arbitration decision moves real money. The input to the decision is an
LLM judgment — a non-deterministic, external, subjective signal that a
deterministic smart contract cannot evaluate and that a single trusted party
should not be allowed to author. If the contract merely stored whatever one
leader's LLM call returned, the leader (or anyone who can influence the
leader's model output) could unilaterally decide escrow outcomes. GenLayer is
what makes the settlement decision **reproducible enough for multiple
independent validators to agree on it**, while still allowing AI-mediated
subjective judgment.

## How the equivalence principle works here

The consensus-critical call is `_arbitrate()` in
[contracts/freelance_escrow.py](contracts/freelance_escrow.py). Design
decisions, in order:

- **`strict_eq` over the raw result is impossible.** Two byte-identical
  executions of the same LLM prompt / web fetch can return different text;
  requiring exact equality of raw outputs would deadlock consensus forever.
  Equivalence is therefore enforced on a **canonical, discrete result**, not
  on raw text.
- **Fixed, enumerable outcome set — no tolerance, anywhere.** The LLM must
  pick exactly one of five labels: `RELEASE_FULL` (100%), `REFUND_FULL` (0%),
  `SPLIT_QUARTER` (25%), `SPLIT_HALF` (50%), `SPLIT_THREE_QUARTER` (75%).
  The payout percent is **derived from the label** by a deterministic
  function (`_percent_for_decision`) and is **never parsed from LLM output**
  — any `percent`-like number the model invents is ignored. Because money is
  a pure function of the label, exact agreement on the label IS exact
  agreement on the transfer. The previous design's ±15-point percentage
  tolerance was removed on purpose: "close enough" is not a valid basis for
  moving funds.
- **Leader-output-only validation is rejected.** A validator that merely
  checks the leader's answer "is well-formed JSON with an allowed status"
  proves formatting, not agreement — the leader alone would be deciding who
  gets the money. This contract does not do that.
- **Genuine comparative validation** (custom validator function via
  `gl.vm.run_nondet_unsafe`):
  1. The leader runs `_run_arbitration_prompt(title, spec, work, url,
     dispute_reason)` — a pure function of on-chain state: it performs its
     own `gl.nondet.web.get` fetch of the delivery URL and its own
     `gl.nondet.exec_prompt` LLM call.
  2. The output is pushed through a deterministic **normalization funnel**
     that accepts ONLY the five fixed labels (after formatting-only case /
     space / hyphen normalization and key aliasing). Anything else raises
     `[LLM_ERROR]`.
  3. **Each validator independently re-runs that entire function — its own
     fetch, its own LLM call — and the validator rule requires an EXACT
     match** on both economic fields: `decision` (label) and
     `evidence_status` (`fetched` / `unreachable` / `no_url_provided`).
     Any mismatch → `False` → leader rotation. There is no numeric band,
     no rounding allowance, no "similar enough".
- **Errors force leader rotation.** If the leader's call fails or its output
  is malformed, `_handle_leader_error` re-runs the task rather than granting
  the doubt; a validator whose own re-run fails also disagrees. Silent
  agreement on garbage is never possible.
- Money never touches the non-deterministic path: payouts are computed
  deterministically from the consensus-agreed label
  (`escrow_atto * percent // 100`, percent ∈ {0, 25, 50, 75, 100}).

Every one of these rules is annotated with inline `WHY` comments at the point
in the code where it applies.

## Evidence acquisition & the refund-by-default policy

A submitted delivery description or link is **claim, not evidence**. The
contract no longer takes it on faith:

- `submit_delivery` deterministically extracts the first `http(s)://` URL
  from the delivery text into `delivery_url` (stored, viewable).
- During arbitration, **every evaluator independently fetches that URL**
  inside its own run (`_fetch_evidence` → `gl.nondet.web.get`). The leader
  never hands a fetched copy to validators, and validators never trust the
  submitter's self-description over the retrieved content. The fetched body
  (capped at `MAX_EVIDENCE_CHARS = 8000`) is embedded in the LLM prompt and
  explicitly weighted above the description.
- **Fetch failure is explicit, never a silent pass.** A non-200 response or
  a raised exception makes the evaluator return the deterministic fallback
  `{decision: REFUND_FULL, evidence_status: "unreachable"}` **without
  consulting the LLM**. If the leader can't fetch but a validator can, the
  two disagree → leader rotates and the fetch is retried. Only when the
  whole ring genuinely cannot retrieve the artifact do all evaluators
  independently converge on `REFUND_FULL` — money returns to the client,
  because **unverifiable work must not pay out**. (Volatile pages can cause
  repeated disagreement; that is fail-safe by design: while evaluators
  cannot agree on what the artifact is, no funds move.)
- **Deliveries with no URL FAIL CLOSED to a full refund.** A delivery that is
  only self-described text (no `http(s)://` link) gives every evaluator
  nothing external to fetch — the only "evidence" would be the freelancer's
  own words, exactly the unsubstantiated-claim path the reviewer flagged. The
  contract therefore short-circuits **before any LLM prompt or web fetch**:
  `_run_arbitration_prompt` returns
  `{decision: REFUND_FULL, evidence_status: "no_url_provided"}`
  deterministically. Because the branch depends only on the empty on-chain
  `delivery_url`, the leader and every validator independently reach the
  *same* result, so `REFUND_FULL` is genuine consensus, never a pass. **This
  replaces the previous lower-assurance "description-only" path, which could
  still release or split funds on the freelancer's say-so.**
- **To be paid, a freelancer must supply a fetchable URL** (public repo,
  deployed app, shared doc — any host the client can reach). The escrow only
  pays against evidence each validator can independently retrieve.

## Security / Fail-Closed Behavior

The arbitration path is **fail-closed by design**: funds reach the freelancer
only when there is independently verifiable evidence that the work exists and
meets the spec. Concretely:

- **Missing delivery URL ⇒ automatic `REFUND_FULL`.** If `delivery_url` is
  empty or whitespace-only, the contract refunds the client in full **before
  running any web fetch or LLM prompt** (`evidence_status:
  "no_url_provided"`). A self-described deliverable can never move money.
  The check is deterministic on-chain logic keyed on the stored `delivery_url`,
  so the leader and every validator reach the identical result and consensus
  enforces it — no single node can pay out on a bare claim.
- **Un-fetchable URL ⇒ `REFUND_FULL`** (`evidence_status: "unreachable"`),
  likewise without the LLM, when the whole ring cannot retrieve the artifact.
- **The LLM is consulted only when content was actually fetched**
  (`evidence_status: "fetched"`), and even then a payout requires leader and
  validator to agree on the **exact** settlement label.

Net effect: absent, unverifiable, or failed-to-fetch evidence always defaults
toward making the client whole — never toward paying out on an unsubstantiated
claim. Proven by the direct test
`test_no_url_delivery_fails_closed_to_refund_without_llm` and by the live
studionet no-URL lifecycle (real validator consensus, `resolve_dispute` →
`REFUND_FULL`/`no_url_provided`) documented below.

## Storage schema

Typed persistent fields only — `TreeMap` / `DynArray`, never raw `dict` /
`list`. Status is stored as `str`, never as an `Enum`. All amounts are
`u256` in **atto scale** (`value * 10**18`); no floats anywhere in
consensus-relevant code.

```
FreelanceEscrow (gl.Contract)
├── agreements:        TreeMap[str, Agreement]    # id -> agreement
├── agreement_ids:     DynArray[str]              # enumeration order
├── escrow_pool_atto:  u256                       # native tokens held
├── withdrawable_atto: TreeMap[str, u256]         # account-key (hex) -> credit
└── next_nonce:        u256                       # anti-replay / ordering

Agreement (@allow_storage @dataclass)   # new fields may ONLY be appended
├── id, client: Address, freelancer: Address, title, spec
├── escrow_atto: u256                   # atto scale
├── delivered_work: str                 # text/link submitted by freelancer
├── status: str                         # created|funded|delivered|disputed|
│                                       # arbitrated|settled|released|refunded
├── dispute_reason: str
├── judgment_decision: str              # RELEASE_FULL|REFUND_FULL|SPLIT_QUARTER|
│                                       # SPLIT_HALF|SPLIT_THREE_QUARTER|none
├── judgment_percent: u256              # DERIVED from the label (0/25/50/75/100)
├── judgment_analysis: str
├── created_at/funded_at/delivered_at/disputed_at/resolved_at: str
│                                       # from consensus message datetime
├── appeal_count: u256                  # max 1
├── arbitration_nonce: u256
├── arbitration_history: DynArray[str]  # json.dumps of every judgment incl.
│                                       # evidence_status, kept after appeal
├── client_credit_atto / freelancer_credit_atto: u256
└── delivery_url: str                   # APPENDED: first http(s) URL extracted
                                        # from delivered_work at submit time
```

Lifecycle: `created → funded → delivered → (released | disputed → arbitrated
→ [appeal: re-arbitrate ×1] → settled/released/refunded) → withdraw()`.
Settlement credits a ledger; `withdraw()` pays out via an emitted native
value transfer (`on="finalized"`), keeping the settlement decision and the
fund movement explicit and auditable.

## Public methods

| Method | Kind | Access rule |
|---|---|---|
| `create_agreement(id, freelancer, title, spec, escrow_amount_atto)` | write | caller becomes client |
| `fund_escrow(id)` | write | client only; `gl.message.value` must equal escrow exactly |
| `submit_delivery(id, delivered_work)` | write | freelancer only; first URL becomes independently fetchable evidence |
| `approve_delivery(id)` | write | client only → full release |
| `raise_dispute(id, reason)` | write | client or freelancer |
| `resolve_dispute(id)` | write | client or freelancer; second call = appeal (max 1) |
| `accept_resolution(id)` | write | client or freelancer; computes & credits payout |
| `withdraw()` | write | anyone with a credit; emits native transfer |
| `get_agreement(id)` / `list_agreements()` / `get_withdrawable(addr)` / `get_escrow_pool()` | view | — |

All violations raise `gl.vm.UserError` (the `gl.UserError` of this SDK
namespace) with `ESCROW:`-prefixed messages — never bare `Exception`.

## Repository layout

```
contracts/freelance_escrow.py     # the contract (pinned runner header, line 1)
tests/direct/                     # fast Direct-mode unit tests (no server)
tests/integration/                # full-consensus tests via gltest
gltest.config.yaml                # integration-test network/account config
requirements.txt                  # genlayer-test + genvm-linter + pytest
scripts/setup_direct_test_cache.sh# one-time local cache fix (see below)
scripts/studionet_e2e_demo.py     # drives the live studionet lifecycle (evidence)
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

Must report `ok` before anything else is trusted. (`genvm-lint check --json`
for machine-readable output.)

## Direct-mode tests (fast, no server)

```bash
scripts/setup_direct_test_cache.sh   # one-time; see note below
pytest tests/direct/ -v
```

**Coverage (30 tests):** happy path with no dispute, dispute → full release,
full refund, and each split label with its exact 25/50/75% payout, unauthorized-sender
rejections for every role, the appeal path (one re-trigger, second
rejected), state-machine guards, value validation, deterministic URL
extraction from deliveries, **evidence fetching** (fetched content proven to
reach the prompt; 404/500 ⇒ deterministic `REFUND_FULL` fallback without the
LLM being consulted), **fail-closed no-URL handling** (a delivery with no URL
— and a whitespace-only URL — refunds `REFUND_FULL` with `evidence_status:
"no_url_provided"` and never reaches the LLM, proven by a mock that would
have said otherwise), and
fixed-set output strictness (labels outside the five accepted values and
unparseable output revert the arbitration; LLM-supplied percent numbers are
ignored in favor of the label).

> **Important caveat (also in the test file headers):** Direct mode executes
> only the **leader** path of `run_nondet_unsafe`. The comparative validator
> — independent fetch + LLM re-execution and the EXACT label/evidence match
> (no tolerance) — is **not** exercised by these tests. Consensus behavior is
> covered by the integration tests below.

> **Cache note:** `genlayer-test`'s direct runner resolves the "latest" genvm
> release and looks for an unversioned `genvm-universal.tar.xz` asset; the
> current release ships it with a versioned filename, which yields an HTTP
> 404. `scripts/setup_direct_test_cache.sh` seeds the local cache from the
> bundle `genvm-lint` already downloaded, which contains the pinned runner
> this contract depends on.

## Integration tests (full consensus)

```bash
# Option A — hosted Studio (gasless, rate-limited 60 req/min per IP): put two
# account private keys in gltest.config.yaml (studionet section), then
gltest tests/integration/ -v -s --network studionet

# Option B — Testnet Bradbury (fund both accounts via
# https://testnet-faucet.genlayer.foundation/ and put their keys in
# gltest.config.yaml)
gltest tests/integration/ -v -s --network testnet_bradbury

# Option C — local Studio (`genlayer up`): full GenVM, Docker required.
```

> **GLSim caveat:** `glsim` (genlayer-test 0.29.x) is **not** a usable path for
> this contract: deploying the pinned runner generation either rejects the
> contract class (`@allow_storage` errors) or extracts an empty schema,
> because the simulator does not run the real GenVM runner. Use studionet /
> testnet / local Studio instead.

[tests/integration/test_escrow_consensus.py](tests/integration/test_escrow_consensus.py)
drives the real flow with **two different signing accounts** (client and
freelancer) and asserts:

1. `resolve_dispute` passes **full leader+validator consensus** — acceptance
   implies independently-executing validators each fetched the delivery URL,
   re-ran the LLM prompt, and agreed on the **exact same** settlement label
   and evidence status (there is no tolerance band left to agree "within"),
   and
2. the **appeal** re-triggers consensus exactly once, the second appeal
   transaction fails execution, and settlement splits per the appealed
   judgment, and
3. a **no-URL delivery fails closed**: `resolve_dispute` on a description-only
   delivery settles `REFUND_FULL` with `evidence_status: "no_url_provided"`
   through full validator consensus, without any evaluator ever reaching the
   LLM prompt or a URL fetch.

> **Live verification status:** the fail-closed and URL-fetch arbitration
> paths were verified against **studio.genlayer.com** this round through real
> validator-consensus transactions on the persistent deployment below — a
> 12-transaction, two-lifecycle end-to-end run (one delivery WITH a URL, one
> WITHOUT) in which every `resolve_dispute` finalized through the 5-validator
> consensus ring, and the no-URL one produced `REFUND_FULL` /
> `no_url_provided` on-chain. Deploy, schema pull, and every write method
> executed through full consensus on the pinned runner. The matching
> reproducible integration test
> (`test_no_url_delivery_fails_closed_via_consensus`) lives in the file above
> for anyone running `gltest` against studionet / testnet / local Studio.

## Studionet deployment & evidence

A **persistent, standalone deployment** of the corrected (fail-closed)
contract (independent of the ephemeral per-test deployments `gltest` creates
internally) was made to studionet from the CLI and driven through **two full
escrow lifecycles: (1) a URL delivery judged by real independent fetching +
exact-label LLM consensus, and (2) a no-URL delivery that FAILS CLOSED to a
deterministic refund**. All 13 transactions below were re-verified against the
explorer's own index (`/address/…` JSON payload) and every one shows status
**FINALIZED**; they are all clickable in the tables.

**Network:** studionet — "Genlayer Studio Network", chainId **61999**, RPC
`https://studio.genlayer.com/api` (explorer: <https://explorer-studio.genlayer.com/>)
**Deployed contract address:** [`0x5EadC908deb2dc5Ad80a65f03105e4Ee9620EbFD`](https://explorer-studio.genlayer.com/address/0x5EadC908deb2dc5Ad80a65f03105e4Ee9620EbFD)

**Accounts used (both real, distinct signers):**

| Role | Address | Signs |
|---|---|---|
| Client | `0x3de43AA2f7162c80af98abe78222aE0Cdf83c506` | create / fund / dispute / resolve / accept |
| Freelancer | `0xc603c2e0E58db94dB0A5754ba280cD6FB5Bfd669` | submit_delivery |

The client key was supplied only via the `GENLAYER_PRIVATE_KEY` environment
variable (`.env`, git-ignored — see [.gitignore](.gitignore)); it was never
hardcoded, logged, or committed. `fund_escrow` sent a real 5 GEN native value
transfer (`5000000000000000000` atto).

**1 — Deploy via the CLI:**

```bash
genlayer network set studionet
genlayer account use escrow-builder
echo "<keystore password>" | genlayer deploy --contract contracts/freelance_escrow.py
```

- Deploy tx: [`0xcf65806dc8e5a51695fc307ec76336336ef904b1d6a7e6ab642de947fe3b067e`](https://explorer-studio.genlayer.com/tx/0xcf65806dc8e5a51695fc307ec76336336ef904b1d6a7e6ab642de947fe3b067e) — FINALIZED; consensus `MAJORITY_AGREE` with 5 validators, and RPC confirms the tx carries the pinned-runner header as its first source bytes

**2 — Two full lifecycles via the genlayer-py SDK** ([scripts/studionet_e2e_demo.py](scripts/studionet_e2e_demo.py)):

`genlayer write` cannot attach native `msg.value`, so the value-bearing
`fund_escrow` (and the whole flow) was driven through the SDK, which sends real
consensus transactions. Run:

```bash
set -a; . ./.env; set +a          # loads GENLAYER_PRIVATE_KEY only
python scripts/studionet_e2e_demo.py \
    --contract-address 0x5EadC908deb2dc5Ad80a65f03105e4Ee9620EbFD \
    --freelancer-keystore ~/.genlayer/keystores/cli-freelancer.json \
    --freelancer-password-stdin
```

**Lifecycle 1 — delivery WITH a URL** (`"https://example.com - hero, features,
contact form delivered"`, a **false claim** since example.com is a placeholder
page). Every evaluator fetched the URL itself, judged the fetched content, and
reached the exact label:

| Step | Method (signer) | Tx hash (clickable — explorer) | Result |
|---|---|---|---|
| 1 | `create_agreement` (client) | [`0x72d84ab9dc7b12d445eb7144fd2c07b6a71df2489a4c81de2f9bcb72c48f1d3a`](https://explorer-studio.genlayer.com/tx/0x72d84ab9dc7b12d445eb7144fd2c07b6a71df2489a4c81de2f9bcb72c48f1d3a) | status `created` |
| 2 | `fund_escrow` +5 GEN (client) | [`0xe5524ffba7799930686734093e0661273e39654ed4cb8ecf74d60c9048c7c9fd`](https://explorer-studio.genlayer.com/tx/0xe5524ffba7799930686734093e0661273e39654ed4cb8ecf74d60c9048c7c9fd) | status `funded`, pool credited |
| 3 | `submit_delivery` (freelancer) | [`0x72005b68edaa435faa02f5761a800c9e2518232b7b64ad60f6e7f5a8e5fdffff`](https://explorer-studio.genlayer.com/tx/0x72005b68edaa435faa02f5761a800c9e2518232b7b64ad60f6e7f5a8e5fdffff) | `delivery_url=https://example.com` extracted |
| 4 | `raise_dispute` (client) | [`0x282f9b1285812928ae5c1766022aaf5fca188a89655df8bd45e0b737f39bde9b`](https://explorer-studio.genlayer.com/tx/0x282f9b1285812928ae5c1766022aaf5fca188a89655df8bd45e0b737f39bde9b) | status `disputed` |
| 5 | `resolve_dispute` (client) | [`0xb6dcf2b578081c98d1d36266d821e09a87ee0ff6ef818b734ccc30a638770bca`](https://explorer-studio.genlayer.com/tx/0xb6dcf2b578081c98d1d36266d821e09a87ee0ff6ef818b734ccc30a638770bca) | **evaluator-fetched content judged; exact-label consensus `REFUND_FULL`**, `evidence_status=fetched` |
| 6 | `accept_resolution` (client) | [`0x8900e0ec4dde5a5de98ed6fe525a8aeeccfe519bdd9d7e45dd087d5b4f578290`](https://explorer-studio.genlayer.com/tx/0x8900e0ec4dde5a5de98ed6fe525a8aeeccfe519bdd9d7e45dd087d5b4f578290) | settlement credited |

**Lifecycle 2 — delivery WITHOUT any URL (the FAIL-CLOSED proof):**

| Step | Method (signer) | Tx hash (clickable — explorer) | Result |
|---|---|---|---|
| 1 | `create_agreement` (client) | [`0xadd6ae9b093edb9a7ca739ca261f10b19d44b29b045e8a7fe92153e828cbf529`](https://explorer-studio.genlayer.com/tx/0xadd6ae9b093edb9a7ca739ca261f10b19d44b29b045e8a7fe92153e828cbf529) | status `created` |
| 2 | `fund_escrow` +5 GEN (client) | [`0xe42d2073924d273f658598beac781c75965c34c9584564706b66cb6e64cffbdd`](https://explorer-studio.genlayer.com/tx/0xe42d2073924d273f658598beac781c75965c34c9584564706b66cb6e64cffbdd) | status `funded` |
| 3 | `submit_delivery` (freelancer, **no URL**) | [`0xc1992cda29741ffe24befd541fbec9499293c98770ea083a6b0fbd0e5becd7a0`](https://explorer-studio.genlayer.com/tx/0xc1992cda29741ffe24befd541fbec9499293c98770ea083a6b0fbd0e5becd7a0) | `delivery_url=""` (nothing to fetch) |
| 4 | `raise_dispute` (client) | [`0x5674099e3e195a4a595449e808a7a8daf014b853d56290e148e15782dd52d79b`](https://explorer-studio.genlayer.com/tx/0x5674099e3e195a4a595449e808a7a8daf014b853d56290e148e15782dd52d79b) | status `disputed` |
| 5 | `resolve_dispute` (client) | [`0x83959026b33431945be20ad4f79ab1bea2d26d0fe884935b18261a8b4b6a55f6`](https://explorer-studio.genlayer.com/tx/0x83959026b33431945be20ad4f79ab1bea2d26d0fe884935b18261a8b4b6a55f6) | **FAIL CLOSED: deterministic `REFUND_FULL`, LLM never consulted**, `evidence_status=no_url_provided` |
| 6 | `accept_resolution` (client) | [`0x36305ad8c87a03d3dddfe6c5ba15b4ac14b0448ce8b35457d4eb683247486d14`](https://explorer-studio.genlayer.com/tx/0x36305ad8c87a03d3dddfe6c5ba15b4ac14b0448ce8b35457d4eb683247486d14) | client made whole |

Final on-chain state of the no-URL agreement (asserted by the script):

```json
{
  "status": "refunded",
  "judgment_decision": "REFUND_FULL",
  "judgment_percent": 0,
  "delivery_url": ""
}
```

…with `arbitration_history` record `decision=REFUND_FULL`,
**`evidence_status=no_url_provided`** — no evaluator ever fetched anything or
ran the LLM; the refund was the deterministic fail-closed outcome, adopted as
real consensus. The URL lifecycle likewise ended `REFUND_FULL` but with
**`evidence_status=fetched`** (evaluators retrieved the placeholder page and
judged it). Aggregate ledger after both lifecycles: `escrow_pool =
10000000000000000000`, `freelancer_credit = 0`,
`client_credit = 10000000000000000000`; the script asserts
`freelancer_credit + client_credit == 2 * escrow_amount` and each stored
percent equals the label-derived payout.

> Note what the two lifecycles together demonstrate: the contract pays the
> freelancer **only** against independently-fetched evidence. A false URL
> claim is caught because evaluators fetch and read the real content; a
> no-URL claim is caught because the contract refuses to judge self-described
> work at all and refunds by default. In neither case can an unsubstantiated
> description move money.

**Explorer verification** (re-checked after this run): the contract page
index lists all 13 transactions above (deploy + the two 6-step lifecycles),
each with `"status":"FINALIZED"`.

## Deploy (testnet)

```bash
npm install -g genlayer
genlayer network set testnet-bradbury     # or: studionet (gasless)
genlayer account create --name builder && genlayer account send <addr> 10gen  # faucet for testnets
genlayer deploy --contract contracts/freelance_escrow.py

genlayer call <address> get_escrow_pool
genlayer write  <address> create_agreement --args "job-1" 0x<FREELANCER> "Logo" "A vector logo, 3 revisions" 5000000000000000000
genlayer receipt <txHash> --stdout --stderr    # lifecycle status ≠ execution success
```

## Verification checklist (submission requirements)

- ✅ Line 1 is a pinned runner header — **no** `py-genlayer:test` / `:latest` / unversioned.
- ✅ Dispute judgment uses a **custom comparative validator** (`run_nondet_unsafe`): validators independently fetch the delivery URL, re-run the prompt, and require an **EXACT match on the fixed settlement label + evidence status**. **No tolerance anywhere; no `strict_eq` on raw LLM/web output.**
- ✅ **No leader-output-only/schema validation** anywhere on the judgment path; outputs outside the five fixed labels (or unparseable) ⇒ validator returns `False` ⇒ leader rotation.
- ✅ Delivered URLs are **fetched independently by every evaluator** (`gl.nondet.web.get` inside its own run — no shared fetched copy); unreachable artifacts fall back **by consensus** to `REFUND_FULL` (refund-by-default), never a silent pass.
- ✅ **Fail closed on missing evidence:** a delivery with no (or whitespace-only) URL returns `REFUND_FULL` with `evidence_status: "no_url_provided"` **before any LLM prompt or fetch** — the freelancer's own description alone can never release or split funds.
- ✅ Typed storage only (`TreeMap`/`DynArray`), `Agreement` dataclass, statuses as `str` (no `Enum` stored), fields append-only.
- ✅ Money is atto-scale `u256` integer math throughout; **no float is ever used to represent or move funds** — after the redesign the contract contains **no `float()` at all**: settlement values come only from the integer label→percent mapping.
- ✅ Sender checks via `gl.message.sender_address` (the pinned SDK's name for the sender field; this SDK has no `sender_account` attribute); all rejections are `gl.vm.UserError` with prefixed messages — **no bare `Exception`**.
- ✅ Appeal path: exactly one re-trigger of `resolve_dispute`.
- ✅ `genvm-lint check` passes; 30 direct-mode tests pass (including the new fail-closed no-URL and whitespace-URL cases); the integration suite contains a matching no-URL fail-closed consensus test.
- ✅ **Persistent studionet deployment** `0x5EadC908deb2dc5Ad80a65f03105e4Ee9620EbFD` (the corrected fail-closed contract) driven through **two full lifecycles** — one URL delivery (independent fetch + exact-label LLM consensus) and one no-URL delivery (**FAIL CLOSED**: `resolve_dispute` → `REFUND_FULL` / `no_url_provided` by real validator consensus) — all 13 txs FINALIZED on the explorer — see [Studionet deployment & evidence](#studionet-deployment--evidence).

## License

MIT — see [LICENSE](LICENSE).
