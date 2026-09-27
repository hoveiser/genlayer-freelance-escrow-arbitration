#!/usr/bin/env python3
"""End-to-end demo of FreelanceEscrow against a PERSISTENT studionet deployment.

This script drives a real deploy (or binds to an existing address) and runs a
full escrow lifecycle on studio.genlayer.com through the genlayer-py SDK:

    create_agreement -> fund_escrow (native value) -> submit_delivery
    -> raise_dispute -> resolve_dispute (independent URL fetch + LLM
       consensus on one EXACT fixed settlement label) -> accept_resolution

It uses TWO accounts so the sender checks are genuinely exercised:
  * CLIENT     - private key read from the GENLAYER_PRIVATE_KEY env var
                 (or --client-key-file), signs create/fund/dispute/resolve.
  * FREELANCER - decrypted from a genlayer CLI keystore file, signs delivery.

Nothing is hardcoded: no keys, no passwords embedded. Example (see README
"Studionet evidence"):

    set -a; . ./.env; set +a
    python scripts/studionet_e2e_demo.py --contract-address 0x... \
        --freelancer-keystore ~/.genlayer/keystores/cli-freelancer.json \
        --freelancer-password-stdin

Never print or commit the keys.
"""

import argparse
import json
import os
import sys
import time

from eth_account import Account
from genlayer_py import create_client
from genlayer_py.chains import studionet

ENDPOINT = "https://studio.genlayer.com/api"
EXPLORER = "https://explorer-studio.genlayer.com"
AMOUNT_ATTO = 5 * 10**18  # 5 GEN at atto scale

# The only settlement outcomes the contract can produce (exact, label-derived).
VALID_JUDGMENTS = {
    "RELEASE_FULL": 100,
    "REFUND_FULL": 0,
    "SPLIT_QUARTER": 25,
    "SPLIT_HALF": 50,
    "SPLIT_THREE_QUARTER": 75,
}

SPEC = ("Build a single-page landing site with exactly three sections: hero, "
        "features, and contact form; deliver as a public URL.")
# The URL inside the delivery is fetched INDEPENDENTLY by every evaluator
# during arbitration - the submitter's words alone never decide the payout.
DELIVERABLE = "https://example.com - hero, features, contact form delivered"
DISPUTE_REASON = "Client claims the contact form section is missing from the page"


def _load_client_key(args):
    if os.environ.get("GENLAYER_PRIVATE_KEY"):
        return os.environ["GENLAYER_PRIVATE_KEY"]
    if args.client_key_file:
        return open(os.path.expanduser(args.client_key_file)).read().strip()
    sys.exit("ERROR: set GENLAYER_PRIVATE_KEY or pass --client-key-file")


def _receipt_ok(receipt):
    try:
        lr = receipt["consensus_data"]["leader_receipt"][0]
        return lr.get("execution_result") == "SUCCESS", lr
    except (KeyError, IndexError):
        return False, {}


def _send(client, account, address, fn, args=None, value=0):
    tx_hash = client.write_contract(
        address=address, function_name=fn, account=account,
        value=value, args=args or [],
    )
    receipt = client.wait_for_transaction_receipt(tx_hash)
    ok, leader = _receipt_ok(receipt)
    votes = (receipt.get("consensus_data") or {}).get("validator_votes")
    print(f"  {fn:18s} tx={tx_hash.hex() if hasattr(tx_hash,'hex') else tx_hash} "
          f"execution={'SUCCESS' if ok else 'FAILED'} validator_votes={votes}")
    if not ok:
        print("    leader stderr:", (leader.get("stderr") or "").strip()[:400])
        sys.exit(f"transaction {fn} failed on-chain")
    return tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--contract-address", help="existing deployment to reuse")
    p.add_argument("--contract-file", default="contracts/freelance_escrow.py")
    p.add_argument("--client-key-file", help="file holding the client private key")
    p.add_argument("--freelancer-keystore", required=True)
    p.add_argument("--freelancer-password-stdin", action="store_true",
                   help="read the keystore password from stdin")
    p.add_argument("--freelancer-password-env", default="FREELANCER_KEYSTORE_PASSWORD")
    args = p.parse_args()

    client_key = _load_client_key(args)
    client_account = Account.from_key(client_key)
    pw = (sys.stdin.readline().strip() if args.freelancer_password_stdin
          else os.environ.get(args.freelancer_password_env, ""))
    with open(os.path.expanduser(args.freelancer_keystore)) as f:
        freelancer_key = Account.decrypt(f.read(), pw)
    freelancer_account = Account.from_key(freelancer_key)

    client = create_client(chain=studionet, account=client_account, endpoint=ENDPOINT)
    print(f"network=studionet  client={client_account.address}  "
          f"freelancer={freelancer_account.address}")

    address = args.contract_address
    if not address:
        # Bind by deploying only when no address was given (deploy path omitted
        # here: the README documents the `genlayer deploy` CLI as the deployer).
        sys.exit("pass --contract-address (deploy via: genlayer deploy ...)")
    print(f"contract={address}  explorer: {EXPLORER}/address/{address}")

    aid = f"job-{int(time.time()) % 100000}"
    hashes = {}
    try:  # get_agreement reverts for unknown ids - an error means the id is free
        ag0 = client.read_contract(address=address, function_name="get_agreement",
                                   args=[aid])
        assert not ag0, "agreement id collision (impossible with time-based id)"
    except AssertionError:
        raise
    except Exception:
        pass

    hashes["create_agreement"] = _send(
        client, client_account, address, "create_agreement",
        args=[aid, freelancer_account.address, "Landing page", SPEC, AMOUNT_ATTO])
    hashes["fund_escrow"] = _send(
        client, client_account, address, "fund_escrow", args=[aid], value=AMOUNT_ATTO)
    hashes["submit_delivery"] = _send(
        client, freelancer_account, address, "submit_delivery", args=[aid, DELIVERABLE])
    hashes["raise_dispute"] = _send(
        client, client_account, address, "raise_dispute", args=[aid, DISPUTE_REASON])
    hashes["resolve_dispute"] = _send(  # <-- full LLM validator consensus
        client, client_account, address, "resolve_dispute", args=[aid])
    hashes["accept_resolution"] = _send(
        client, client_account, address, "accept_resolution", args=[aid])

    ag = client.read_contract(address=address, function_name="get_agreement", args=[aid])
    pool = client.read_contract(address=address, function_name="get_escrow_pool")
    cred_f = client.read_contract(address=address, function_name="get_withdrawable",
                                  args=[freelancer_account.address])
    cred_c = client.read_contract(address=address, function_name="get_withdrawable",
                                  args=[client_account.address])
    print("\nfinal agreement state:")
    print(json.dumps({k: ag[k] for k in
                      ("status", "judgment_decision", "judgment_percent",
                       "appeal_count", "delivery_url")}, indent=2))
    record = json.loads(ag["arbitration_history"][-1])
    print(f"arbitration record: decision={record['decision']} "
          f"evidence_status={record['evidence_status']}")
    assert record["decision"] in VALID_JUDGMENTS, "label outside fixed set!"
    assert ag["judgment_decision"] == record["decision"]
    assert ag["judgment_percent"] == VALID_JUDGMENTS[record["decision"]], \
        "stored percent must EXACTLY equal the label-derived payout"
    print(f"escrow_pool={pool}  freelancer_credit={cred_f}  client_credit={cred_c}")
    assert cred_f + cred_c == AMOUNT_ATTO, "payout must equal the escrow exactly"
    print("\nOK: full lifecycle executed on studionet with real consensus.")
    print("TX_HASHES=" + json.dumps(hashes))


if __name__ == "__main__":
    main()
