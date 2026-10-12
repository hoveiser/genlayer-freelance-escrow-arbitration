#!/usr/bin/env python3
"""Deploy contracts/freelance_escrow.py to studionet as a PERSISTENT contract.

Usage (client key via GENLAYER_PRIVATE_KEY only, never printed):

    set -a; . ./.env; set +a
    python scripts/deploy_studionet.py

Prints the deploy tx hash and the new contract address. The source is sent
verbatim: GenLayer stores it in the deploy transaction (base64, retrievable)
and every validator executes exactly those bytes.
"""
import ast
import hashlib
import json
import os
import sys

from eth_account import Account
from genlayer_py import create_client
from genlayer_py.chains import studionet

ENDPOINT = "https://studio.genlayer.com/api"
EXPLORER = "https://explorer-studio.genlayer.com"
CONTRACT_FILE = os.environ.get("DEPLOY_CONTRACT_FILE", "contracts/freelance_escrow.py")


def load_client_key():
    key = os.environ.get("GENLAYER_PRIVATE_KEY", "")
    if not key and os.path.exists(".env"):
        for line in open(".env"):
            line = line.strip().rstrip("\r")
            if line.startswith("GENLAYER_PRIVATE_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        sys.exit("ERROR: set GENLAYER_PRIVATE_KEY (never hardcoded)")
    return key.strip()


def main():
    account = Account.from_key(load_client_key())
    client = create_client(chain=studionet, account=account, endpoint=ENDPOINT)
    bal = int(client.get_balance(account.address)) / 10**18
    print(f"client={account.address} balance={bal:.4f} GEN")

    with open(CONTRACT_FILE, "r") as f:
        code = f.read()
    src_sha = hashlib.sha256(code.encode()).hexdigest()
    print(f"source={CONTRACT_FILE} bytes={len(code)} sha256={src_sha}")

    tx_hash = client.deploy_contract(code=code, account=account)
    tx = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    if not tx.startswith("0x"):
        tx = "0x" + tx
    print(f"deploy_tx={tx}\nexplorer: {EXPLORER}/tx/{tx}")

    receipt = client.wait_for_transaction_receipt(tx_hash)
    result_name = receipt.get("result_name") or receipt.get("status_name")
    addr = receipt.get("recipient") or receipt.get("to_address")
    data = receipt.get("data")
    if isinstance(data, str) and "contract_address" in data:
        try:
            # ast.literal_eval parses the receipt's dict STRING as data only,
            # never executes it (no eval on RPC-returned text).
            addr = ast.literal_eval(data).get("contract_address", addr)
        except Exception:
            pass
    print(f"result={result_name}")
    print(f"contract_address={addr}")
    if not addr:
        sys.exit("could not extract contract address from receipt")
    print(f"explorer: {EXPLORER}/address/{addr}")
    pool = client.read_contract(address=addr, function_name="get_escrow_pool")
    print(f"get_escrow_pool()={pool}")
    print("DEPLOY_JSON=" + json.dumps({"address": addr, "deploy_tx": tx,
                                       "source_sha256": src_sha}))


if __name__ == "__main__":
    main()
