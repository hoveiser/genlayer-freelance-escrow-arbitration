#!/usr/bin/env python3
"""Drive the DELIVERY-DEADLINE reclaim path on a persistent studionet deploy.

Proves on-chain, through real validator consensus:
  1. fund (deadline starts, minimum PRODUCTION window of 1 hour);
  2. reclaim BEFORE the deadline REVERTS (early recovery is impossible);
  3. after the deadline: late submit_delivery REVERTS;
  4. reclaim_undelivered SUCCEEDS: full escrow credited to the client,
     freelancer stays at 0, accounting invariant holds;
  5. withdraw transfers the credit and the pool returns to its baseline.

Usage (keys via env/files only, never printed):

    set -a; . ./.env; set +a
    python scripts/studionet_reclaim_demo.py --contract-address 0x... \
        --freelancer-key-file ~/.gltest_freelancer_key

Runs ~1h (the window is the contract's production floor, not weakened).
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
AMOUNT_ATTO = 3 * 10**18
MIN_WINDOW = 3600  # the contract's own floor - not weakened for the demo


def _load_client_key():
    key = os.environ.get("GENLAYER_PRIVATE_KEY", "")
    if not key and os.path.exists(".env"):
        for line in open(".env"):
            line = line.strip().rstrip("\r")
            if line.startswith("GENLAYER_PRIVATE_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        sys.exit("ERROR: set GENLAYER_PRIVATE_KEY")
    return key.strip()


def _leader_ok(receipt):
    try:
        lr = receipt["consensus_data"]["leader_receipt"][0]
        return lr.get("execution_result") == "SUCCESS", lr
    except (KeyError, IndexError):
        return False, {}


def _send(client, account, address, fn, args=None, value=0, expect_fail=False):
    tx_hash = client.write_contract(
        address=address, function_name=fn, account=account,
        value=value, args=args or [])
    receipt = client.wait_for_transaction_receipt(tx_hash)
    ok, leader = _leader_ok(receipt)
    h = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    verdict = "FAILED (as intended)" if expect_fail else ("SUCCESS" if ok else "FAILED")
    print(f"  {fn:22s} tx={h} execution={verdict}")
    if expect_fail:
        if ok:
            sys.exit(f"ERROR: {fn} was expected to revert on-chain but succeeded")
    elif not ok:
        print("    leader stderr:", (leader.get("stderr") or "").strip()[:400])
        sys.exit(f"transaction {fn} failed on-chain")
    return h


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--contract-address", required=True)
    p.add_argument("--freelancer-key-file", required=True)
    args = p.parse_args()

    client_account = Account.from_key(_load_client_key())
    fkey = open(os.path.expanduser(args.freelancer_key_file)).read().strip()
    freelancer_account = Account.from_key(fkey)
    client = create_client(chain=studionet, account=client_account, endpoint=ENDPOINT)
    address = args.contract_address
    aid = f"reclaim-{int(time.time()) % 100000}"
    hashes = {}
    print(f"contract={address} client={client_account.address} "
          f"freelancer={freelancer_account.address}")

    hashes["create_agreement"] = _send(
        client, client_account, address, "create_agreement",
        args=[aid, freelancer_account.address, "Poster set",
              "Deliver 3 posters as a public URL gallery",
              AMOUNT_ATTO, MIN_WINDOW])
    hashes["fund_escrow"] = _send(
        client, client_account, address, "fund_escrow", args=[aid],
        value=AMOUNT_ATTO)
    ag = client.read_contract(address=address, function_name="get_agreement",
                              args=[aid])
    deadline = ag["delivery_deadline_ts"]
    print(f"  delivery_deadline_ts={deadline} "
          f"({ag['delivery_window_seconds']}s window, production floor)")

    # 2. Early reclaim must revert ON-CHAIN (reverts are real, executed txs).
    hashes["reclaim_early_reverted"] = _send(
        client, client_account, address, "reclaim_undelivered", args=[aid],
        expect_fail=True)
    pool_before = client.read_contract(address=address,
                                       function_name="get_escrow_pool")

    # 3. Wait past the REAL deadline (+margin for message-clock skew).
    print("  waiting out the delivery window...")
    while time.time() <= deadline + 120:
        time.sleep(60)

    # Late delivery must now revert.
    hashes["submit_delivery_late_reverted"] = _send(
        client, freelancer_account, address, "submit_delivery",
        args=[aid, "https://late.example/gallery"], expect_fail=True)

    # 4. Reclaim succeeds and refunds in full.
    hashes["reclaim_undelivered"] = _send(
        client, client_account, address, "reclaim_undelivered", args=[aid])
    ag = client.read_contract(address=address, function_name="get_agreement",
                              args=[aid])
    print("  state:", json.dumps({k: ag[k] for k in
                                  ("status", "judgment_decision",
                                   "judgment_percent")}))
    assert ag["status"] == "refunded" and ag["judgment_percent"] == 0
    record = json.loads(ag["arbitration_history"][-1])
    assert "deadline expired without delivery" in record["analysis"]
    cred_f = client.read_contract(address=address, function_name="get_withdrawable",
                                  args=[freelancer_account.address])
    assert cred_f == 0, "freelancer must never be paid on reclaim"
    acc = client.read_contract(address=address, function_name="get_accounting")
    print("  accounting:", json.dumps(acc, default=str))
    assert acc["invariant_holds"], f"accounting broken: {acc}"

    # Double reclaim must revert (already refunded).
    hashes["reclaim_second_reverted"] = _send(
        client, client_account, address, "reclaim_undelivered", args=[aid],
        expect_fail=True)

    # 5. Withdraw the reclaimed escrow.
    hashes["withdraw"] = _send(client, client_account, address, "withdraw")
    pool_after = client.read_contract(address=address,
                                      function_name="get_escrow_pool")
    acc = client.read_contract(address=address, function_name="get_accounting")
    assert int(pool_before) - int(pool_after) == AMOUNT_ATTO, \
        f"withdraw must move exactly the escrow ({pool_before} -> {pool_after})"
    assert acc["invariant_holds"], f"accounting broken: {acc}"
    print(f"  pool {pool_before} -> {pool_after} (escrow returned to client)")
    print("\nOK: reclaim lifecycle executed on studionet with real consensus.")
    print("TX_HASHES=" + json.dumps(hashes))
    print(f"explorer: {EXPLORER}/address/{address}")


if __name__ == "__main__":
    main()
