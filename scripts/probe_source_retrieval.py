#!/usr/bin/env python3
"""Check whether studionet exposes the deployed contract SOURCE, so the
repo-vs-deploy comparison can be done honestly (see README provenance).

Tries, in order:
  1. get_contract_schema_for_code(code) - does the RPC answer schema queries
     for raw source at all (proves the source-embedding path)?
  2. get_contract_schema(address) - the deployed address's schema.
  3. get_code / eth_getCode - EVM-style bytecode retrieval (usually empty on
     GenLayer: intelligent contracts store Python source, not EVM bytecode).
Prints what each returns; leaks nothing sensitive.
"""
import os
import sys

from eth_account import Account
from genlayer_py import create_client
from genlayer_py.chains import studionet

ENDPOINT = "https://studio.genlayer.com/api"
ENV_FILE = ".env"
CONTRACT_FILE = "contracts/freelance_escrow.py"
ADDRESS = sys.argv[1] if len(sys.argv) > 1 else ""

key = os.environ.get("GENLAYER_PRIVATE_KEY", "")
if not key:
    for line in open(ENV_FILE):
        line = line.strip().rstrip("\r")
        if line.startswith("GENLAYER_PRIVATE_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
acct = Account.from_key(key.strip())
client = create_client(chain=studionet, account=acct, endpoint=ENDPOINT)

code = open(CONTRACT_FILE).read()

print("[1] get_contract_schema_for_code(source):")
try:
    s = client.get_contract_schema_for_code(code)
    names = sorted((s.get("methods") or {}).keys()) if isinstance(s, dict) else s
    print("    OK, methods from source:", names)
except Exception as e:
    print("    FAILED:", type(e).__name__, str(e)[:200])

if ADDRESS:
    print(f"[2] get_contract_schema({ADDRESS}):")
    try:
        s = client.get_contract_schema(address=ADDRESS)
        names = sorted((s.get("methods") or {}).keys()) if isinstance(s, dict) else s
        print("    OK, deployed methods:", names)
    except Exception as e:
        print("    FAILED:", type(e).__name__, str(e)[:200])

    print(f"[3] eth_getCode({ADDRESS}):")
    try:
        raw = client.w3.eth.get_code(ADDRESS)
        print(f"    returned {len(raw)} bytes; first 60 hex:", raw[:60].hex())
    except Exception as e:
        print("    FAILED:", type(e).__name__, str(e)[:200])
