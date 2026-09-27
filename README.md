# genlayer-freelance-escrow-arbitration

A standalone **GenLayer Intelligent Contract**: a freelance escrow in which
disputes are settled by **LLM-based arbitration validated by GenLayer
validator consensus** — not by either party's claim and not by a single
leader's model output.

Submitter entry for GenLayer's builder program, "Intelligent Contract"
category. Contract-only submission (no frontend/backend).

**Live evidence** — the corrected contract is deployed and driven end-to-end
on studionet (see
[Studionet deployment & evidence](#studionet-deployment--evidence);
explorer: <https://explorer-studio.genlayer.com/>):

- Contract: [`0xaAa1690AFDDC2c2b5889340B2B8C9BD35Bf1f976`](https://explorer-studio.genlayer.com/address/0xaAa1690AFDDC2c2b5889340B2B8C9BD35Bf1f976)
- Deploy tx: [`0x99ed6eaa81930f9e7e0c62d8461a8930640de5a8842153f6431aa919795ffe75`](https://explorer-studio.genlayer.com/tx/0x99ed6eaa81930f9e7e0c62d8461a8930640de5a8842153f6431aa919795ffe75)
- Arbitration tx (independent URL fetch + exact-label LLM consensus): [`0xf14b906ac25d15911c4128b4e3957e930ba3ac78bcf48ccd4844c5b56ff69d1b`](https://explorer-studio.genlayer.com/tx/0xf14b906ac25d15911c4128b4e3957e930ba3ac78bcf48ccd4844c5b56ff69d1b)

## The use case

1. A **client** drafts an agreement with a written spec and a deal amount,
   then **funds the escrow** with native tokens (atto-scale `u256`).
2. The **freelancer** submits the delivered work (text, description, or link).
3. Either:
   - the client **approves** → funds are released in full, or
   - either party **raises a dispute** → the contract runs **on-chain
     arbitration**: every evaluator independently FETCHES the delivered
     artifact (when the delivery included a URL) and an LLM grades that
     fetched evidence against the original spec. The outcome must be exactly
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
     `evidence_status` (`fetched` / `unreachable` / `description_only`).
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
- **Deliveries with no URL** (pure description text) remain allowed, stamped
  `evidence_status: "description_only"` in the audit history. **This is a
  deliberately lower-assurance path**: the LLM can only weigh the
  submitter's own words against the spec, so there is nothing external to
  independently verify, and a well-written lie is harder to catch. The
  reasoning for still allowing it: many real deliverables (private repos,
  design files handed over out-of-band, IRL consulting) legitimately have no
  public URL, and both parties already agreed to the split-label economics
  with full knowledge that only description-based evidence exists. The
  status is preserved in `arbitration_history` so the weaker evidentiary
  basis is always auditable, and clients who want hard verification can
  write "deliver as a public URL" into the spec itself.

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

**Coverage (29 tests):** happy path with no dispute, dispute → full release,
full refund, and each split label with its exact 25/50/75% payout, unauthorized-sender
rejections for every role, the appeal path (one re-trigger, second
rejected), state-machine guards, value validation, deterministic URL
extraction from deliveries, **evidence fetching** (fetched content proven to
reach the prompt; 404/500 ⇒ deterministic `REFUND_FULL` fallback without the
LLM being consulted; description-only deliveries stamped as such), and
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
   judgment.

> **Live verification status:** both integration tests above were re-run
> green against **studio.genlayer.com** against the REDESIGNED contract
> (real GenVM, real validators, real independent URL fetches + LLM
> arbitration with exact-label consensus — 2 passed in 165s). Deploy,
> schema pull, and every write method executed through full consensus on
> the pinned runner.

## Studionet deployment & evidence

A **persistent, standalone deployment** of the REDESIGNED contract
(independent of the ephemeral per-test deployments `gltest` creates
internally) was made to studionet from the CLI and driven through the **full
escrow lifecycle: real URL evidence fetching + exact-label LLM consensus**.
All transactions below were re-verified against the explorer's own index
(`/address/…` JSON payload) and every one shows status **FINALIZED**; they
are all clickable in the tables.

**Network:** studionet — "Genlayer Studio Network", chainId **61999**, RPC
`https://studio.genlayer.com/api` (explorer: <https://explorer-studio.genlayer.com/>)
**Deployed contract address:** [`0xaAa1690AFDDC2c2b5889340B2B8C9BD35Bf1f976`](https://explorer-studio.genlayer.com/address/0xaAa1690AFDDC2c2b5889340B2B8C9BD35Bf1f976)

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

- Deploy tx: [`0x99ed6eaa81930f9e7e0c62d8461a8930640de5a8842153f6431aa919795ffe75`](https://explorer-studio.genlayer.com/tx/0x99ed6eaa81930f9e7e0c62d8461a8930640de5a8842153f6431aa919795ffe75) — FINALIZED; RPC confirms the tx carries the pinned-runner header as its first source bytes

**2 — A raw `genlayer write` (smoke test, id `job-cli-2`):**

- `create_agreement` tx: [`0x1b3eff0d2834504a16504e8ff2f0fc0a77ade825ce6176a0e3bb08e82aad06e7`](https://explorer-studio.genlayer.com/tx/0x1b3eff0d2834504a16504e8ff2f0fc0a77ade825ce6176a0e3bb08e82aad06e7) — execution SUCCESS, FINALIZED

**3 — Full lifecycle via the genlayer-py SDK** ([scripts/studionet_e2e_demo.py](scripts/studionet_e2e_demo.py)):

`genlayer write` cannot attach native `msg.value`, so the value-bearing
`fund_escrow` (and the whole flow) was driven through the SDK, which sends real
consensus transactions. Run:

```bash
set -a; . ./.env; set +a          # loads GENLAYER_PRIVATE_KEY only
python scripts/studionet_e2e_demo.py \
    --contract-address 0xaAa1690AFDDC2c2b5889340B2B8C9BD35Bf1f976 \
    --freelancer-keystore ~/.genlayer/keystores/cli-freelancer.json \
    --freelancer-password-stdin
```

The delivery submitted was
`"https://example.com - hero, features, contact form delivered"` — a
**false claim** (example.com is a placeholder page). This is exactly the
case the reviewer flagged, and the on-chain outcome proves the fix works
end to end:

| Step | Method (signer) | Tx hash (clickable — explorer) | Result |
|---|---|---|---|
| 1 | `create_agreement` (client) | [`0xfffa2a436fe46595ff609992f57110129bdc33599b89b0f9f1eaa367a5f20461`](https://explorer-studio.genlayer.com/tx/0xfffa2a436fe46595ff609992f57110129bdc33599b89b0f9f1eaa367a5f20461) | agreement stored, status `created` |
| 2 | `fund_escrow` +5 GEN (client) | [`0x1dcfbfe7da48f5db8940e4e0a57f62fde8e727c68f22a70377b3eb696f7ab4b7`](https://explorer-studio.genlayer.com/tx/0x1dcfbfe7da48f5db8940e4e0a57f62fde8e727c68f22a70377b3eb696f7ab4b7) | status `funded`, escrow pool credited |
| 3 | `submit_delivery` (freelancer) | [`0xd7b3aa5c9e68b6ce494ba21559700f903f722fe175d10511660eb0aa484c28c6`](https://explorer-studio.genlayer.com/tx/0xd7b3aa5c9e68b6ce494ba21559700f903f722fe175d10511660eb0aa484c28c6) | status `delivered`, `delivery_url=https://example.com` extracted |
| 4 | `raise_dispute` (client) | [`0xd6acdabacbe2ab6d0db8a323ce1fad5a3b48b5d77c655a318ff5b0835482d23e`](https://explorer-studio.genlayer.com/tx/0xd6acdabacbe2ab6d0db8a323ce1fad5a3b48b5d77c655a318ff5b0835482d23e) | status `disputed` |
| 5 | `resolve_dispute` (client) | [`0xf14b906ac25d15911c4128b4e3957e930ba3ac78bcf48ccd4844c5b56ff69d1b`](https://explorer-studio.genlayer.com/tx/0xf14b906ac25d15911c4128b4e3957e930ba3ac78bcf48ccd4844c5b56ff69d1b) | **every evaluator fetched the URL itself, LLM judged the FETCHED content, validators agreed on the exact label `REFUND_FULL`**, status `arbitrated` |
| 6 | `accept_resolution` (client) | [`0xa053bd1e916b030bc3507222e649654242e737d4e5ad92a0002f326e32ba6536`](https://explorer-studio.genlayer.com/tx/0xa053bd1e916b030bc3507222e649654242e737d4e5ad92a0002f326e32ba6536) | settlement credited to payout ledger |

Final on-chain state read back after step 6 (asserted by the script):

```json
{
  "status": "refunded",
  "judgment_decision": "REFUND_FULL",
  "judgment_percent": 0,
  "appeal_count": 0,
  "delivery_url": "https://example.com"
}
```

arbitration history record: `decision=REFUND_FULL`,
**`evidence_status=fetched`** — the artifact really was retrieved during
consensus. `escrow_pool = 5000000000000000000`, `freelancer_credit = 0`,
`client_credit = 5000000000000000000`, and the script asserts
`freelancer_credit + client_credit == escrow_amount` plus
`judgment_percent == 0` (the exact payout derived from the label).

> Note what this run demonstrates: the delivery description *claimed* the
> spec was fulfilled, but the contract did not trust the claim — the
> evaluators fetched `https://example.com`, saw a placeholder domain page,
> and independently reached the **same exact label** `REFUND_FULL`, so the
> client got 100% back. Under the old tolerance-based design a 15-point band
> could have absorbed this kind of disagreement; now the transaction only
> finalized because two independent judgments (fetch + LLM) matched exactly
> on the label and evidence status.

**Explorer verification** (re-checked after this run): the contract page
index lists all 8 transactions above (deploy, smoke write, 6-step
lifecycle), each with `"status":"FINALIZED"`.

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
- ✅ Typed storage only (`TreeMap`/`DynArray`), `Agreement` dataclass, statuses as `str` (no `Enum` stored), fields append-only.
- ✅ Money is atto-scale `u256` integer math throughout; **no float is ever used to represent or move funds** — after the redesign the contract contains **no `float()` at all**: settlement values come only from the integer label→percent mapping.
- ✅ Sender checks via `gl.message.sender_address` (the pinned SDK's name for the sender field; this SDK has no `sender_account` attribute); all rejections are `gl.vm.UserError` with prefixed messages — **no bare `Exception`**.
- ✅ Appeal path: exactly one re-trigger of `resolve_dispute`.
- ✅ `genvm-lint check` passes; 29 direct-mode tests pass; **both full-consensus integration tests pass live on StudioNet** (real validators independently fetched + re-judged and agreed on the exact label).
- ✅ **Persistent studionet deployment** `0xaAa1690AFDDC2c2b5889340B2B8C9BD35Bf1f976` (the corrected contract) driven through the entire lifecycle (deploy → create → fund w/ 5 GEN → deliver w/ URL → dispute → **fetch + exact-label LLM consensus arbitration** → settle), all tx FINALIZED on the explorer — see [Studionet deployment & evidence](#studionet-deployment--evidence).

## License

MIT — see [LICENSE](LICENSE).
