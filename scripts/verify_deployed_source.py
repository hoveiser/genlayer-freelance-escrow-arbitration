#!/usr/bin/env python3
"""Compare the repo contract source against what is deployed on studionet.

WHAT THE CHAIN EXPOSES (measured against live studionet, not assumed):
  * `eth_getTransactionByHash` on the DEPLOY tx returns structured data whose
    `data.contract_code` field is the base64 of the EXACT source the network
    stores and executes for this address. That makes a byte-level repo-vs-
    deployed comparison possible on-chain.
  * `eth_getCode(address)` returns 0 bytes: GenLayer intelligent contracts do
    not store EVM bytecode; the canonical source record lives in the deploy
    transaction above.
  * `gen_getContractSchema(address)` / `gen_getContractSchemaForCode(source)`
    let anyone re-derive the deployed interface from the repo file.

Checks:
  1. sha256(base64-decoded deploy tx contract_code) == sha256(repo file)
  2. deployed schema method set == schema derived from the repo file

Usage (read-only, no keys):
    python scripts/verify_deployed_source.py ADDRESS DEPLOY_TX_HASH \
        [--contract-file contracts/freelance_escrow.py]
"""
import argparse
import base64
import hashlib
import json
import sys
import urllib.request

RPC = "https://studio.genlayer.com/api"
EXPLORER = "https://explorer-studio.genlayer.com"


def rpc(method, params):
    req = urllib.request.Request(
        RPC, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                              "params": params}).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "genlayer-verify/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def _to_dict(obj):
    """web3 returns AttributeDict / nested structures; normalize to dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "_asdict"):
        return obj._asdict()
    try:
        return dict(obj)
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("address")
    p.add_argument("deploy_tx")
    p.add_argument("--contract-file", default="contracts/freelance_escrow.py")
    args = p.parse_args()

    src = open(args.contract_file, "r", encoding="utf-8").read()
    src_sha = hashlib.sha256(src.encode()).hexdigest()
    print(f"repo source: {args.contract_file} sha256={src_sha}")

    # 1. Deploy tx carries the stored source as base64 in data.contract_code.
    tx = _to_dict(rpc("eth_getTransactionByHash", [args.deploy_tx])) or {}
    data = _to_dict(tx.get("data")) or {}
    code_b64 = None
    if isinstance(data, dict):
        code_b64 = data.get("contract_code")
    deployed_ok = False
    if code_b64:
        deployed_src = base64.b64decode(code_b64).decode("utf-8")
        dep_sha = hashlib.sha256(deployed_src.encode()).hexdigest()
        deployed_ok = dep_sha == src_sha
        print(f"deployed source (from tx): sha256={dep_sha} "
              f"bytes={len(deployed_src)}")
        print(f"[1] byte-level source match: {deployed_ok}")
    else:
        print("[1] deploy tx did not expose contract_code "
              f"(data keys: {list(data)[:8] if isinstance(data, dict) else type(data)}); "
              "falling back to schema comparison only")

    # 2. Schema comparison (deployed vs repo-derived), both network-computed.
    def method_names(schema):
        s = schema if isinstance(schema, dict) else json.loads(schema)
        return sorted((s.get("methods") or {}).keys())

    schema_ok = False
    try:
        deployed = rpc("gen_getContractSchema", [args.address])
        from_repo = rpc("gen_getContractSchemaForCode", [src])
        d_m, r_m = method_names(deployed), method_names(from_repo)
        schema_ok = d_m == r_m
        print(f"[2] deployed methods: {d_m}")
        print(f"    repo-derived    : {r_m}")
        print(f"    schema match: {schema_ok}")
    except Exception as e:
        print(f"[2] schema RPC unavailable on this network: {str(e)[:160]}")

    # Context: eth_getCode is empty on GenLayer (no EVM bytecode) - honest note.
    try:
        code = rpc("eth_getCode", [args.address, "latest"])
        print(f"[i] eth_getCode = {code!r} (source lives in the deploy tx, "
              "not as account code)")
    except Exception:
        pass

    print(f"deploy tx: {EXPLORER}/tx/{args.deploy_tx}")
    print(f"address:   {EXPLORER}/address/{args.address}")

    if deployed_ok and schema_ok:
        print("VERIFIED: on-chain source bytes and deployed interface both "
              "match the repo file exactly.")
        sys.exit(0)
    if deployed_ok:
        print("VERIFIED at byte level (source match); schema check inconclusive.")
        sys.exit(0)
    print("NOT FULLY VERIFIED: review the outputs above.")
    sys.exit(1)


if __name__ == "__main__":
    main()
