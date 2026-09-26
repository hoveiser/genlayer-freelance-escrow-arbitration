# genlayer-freelance-escrow-arbitration

A standalone **GenLayer Intelligent Contract**: a freelance escrow in which
disputes are settled by **LLM-based arbitration validated by GenLayer
validator consensus** — not by either party's claim and not by a single
leader's model output.

Submitter entry for GenLayer's builder program, "Intelligent Contract"
category. Contract-only submission (no frontend/backend).

## The use case

1. A **client** drafts an agreement with a written spec and a deal amount,
   then **funds the escrow** with native tokens (atto-scale `u256`).
2. The **freelancer** submits the delivered work (text, description, or link).
3. Either:
   - the client **approves** → funds are released in full, or
   - either party **raises a dispute** → the contract runs **on-chain
     arbitration**: an LLM grades the delivered work against the original
     spec, and the outcome (`release` / `refund` / percentage `split`) is
     adopted only if GenLayer **validators reach consensus** on it.
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

- **`strict_eq` is impossible.** Two byte-identical executions of the same
  LLM prompt can return different text; requiring exact equality would
  deadlock consensus forever.
- **Leader-output-only validation is rejected.** A validator that merely
  checks the leader's answer "is well-formed JSON with an allowed status"
  proves formatting, not agreement — the leader alone would be deciding who
  gets the money. This contract does not do that.
- **Genuine comparative validation** (custom validator function via
  `gl.vm.run_nondet_unsafe`):
  1. The leader runs `_run_arbitration_prompt(spec, work, dispute_reason)` —
     a pure function of on-chain state, so every participant builds the
     *exact same prompt*.
  2. The output is pushed through one deterministic **normalization funnel**
     (key aliasing, synonym mapping, percent coercion, and canonicalization
     so that e.g. `split @ 100%` becomes `release`). The fields compared are
     then a pure function of the model's semantic intent.
  3. **Each validator independently re-runs the same prompt itself** and
     compares only the **decision fields**:
       - `decision` (`release`/`refund`/`split`) must match **exactly**, and
       - `freelancer_percent` must agree within an **explicit integer
         tolerance** (`SPLIT_TOLERANCE_PERCENT = 15`) because a split ratio
         is a continuum where small LLM wobble is economically harmless.
- **Errors force leader rotation.** If the leader's LLM call fails or its
  output cannot be parsed, `_normalize_judgment` raises a `[LLM_ERROR]`-
  prefixed `gl.vm.UserError`, and the validator **disagrees** (`return
  False`) rather than accepting broken output — consensus re-runs with a new
  leader. A validator whose own re-run fails also disagrees. Silent agreement
  on garbage is never possible.
- Money never touches the non-deterministic path: payouts are computed
  deterministically from consensus-agreed integer fields
  (`escrow_atto * percent // 100`).

Every one of these rules is annotated with inline `WHY` comments at the point
in the code where it applies.

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
├── judgment_decision: str              # release|refund|split|none
├── judgment_percent: u256              # freelancer's share, 0..100
├── judgment_analysis: str
├── created_at/funded_at/delivered_at/disputed_at/resolved_at: str
│                                       # from consensus message datetime
├── appeal_count: u256                  # max 1
├── arbitration_nonce: u256
├── arbitration_history: DynArray[str]  # json.dumps of every judgment, kept
│                                       # even after appeal (auditable trail)
└── client_credit_atto / freelancer_credit_atto: u256
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
| `submit_delivery(id, delivered_work)` | write | freelancer only |
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

**Coverage:** happy path with no dispute, dispute → full release, dispute →
full refund, dispute → split (60/40), unauthorized-sender rejections for
every role, the appeal path (one re-trigger, second rejected), state-machine
guards, value validation, and LLM-output resilience (synonyms, stringified
numbers, unparseable output aborting the transaction).

> **Important caveat (also in the test file headers):** Direct mode executes
> only the **leader** path of `run_nondet_unsafe`. The comparative validator
> — independent LLM re-execution, decision-field comparison with tolerance,
> error → disagreement → leader rotation — is **not** exercised by these
> tests. Consensus behavior is covered by the integration tests below.

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

1. `resolve_dispute` passes **full leader+validator consensus** (acceptance
   implies independent validators' normalized judgments agreed on the
   decision label and the percent within tolerance), and
2. the **appeal** re-triggers consensus exactly once, the second appeal
   transaction fails execution, and settlement splits per the appealed
   judgment.

> **Live verification status:** both integration tests above were run green
> against **studio.genlayer.com** (real GenVM, real validators, real LLM
> arbitration prompts — 2 passed, ~2.6 min). Deploy, schema pull, and every
> write method executed through full consensus on the pinned runner.

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
- ✅ Dispute judgment uses a **custom comparative validator** (`run_nondet_unsafe`): validators re-run the same prompt and compare `decision` (exact) + `percent` (±15 tolerance). **No `strict_eq` on LLM output.**
- ✅ **No leader-output-only/schema validation** anywhere on the judgment path.
- ✅ LLM errors / unparseable output ⇒ validator returns `False` ⇒ leader rotation.
- ✅ Typed storage only (`TreeMap`/`DynArray`), `Agreement` dataclass, statuses as `str` (no `Enum` stored), fields append-only.
- ✅ Money is atto-scale `u256` integer math throughout; **no float is ever used to represent or move funds**. (The only `float()` in the file transiently coerces the LLM's percent *text* — e.g. `"60"`/`"60.0"` — to an int via `int(round(...))`, then clamps to 0-100; it never touches the balance ledger.)
- ✅ Sender checks via `gl.message.sender_address` (the pinned SDK's name for the sender field; this SDK has no `sender_account` attribute); all rejections are `gl.vm.UserError` with prefixed messages — **no bare `Exception`**.
- ✅ Appeal path: exactly one re-trigger of `resolve_dispute`.
- ✅ `genvm-lint check` passes; 22 direct-mode tests pass; **both full-consensus integration tests pass live on StudioNet** (real validators independently re-ran the arbitration LLM prompt and agreed).

## License

MIT — see [LICENSE](LICENSE).
