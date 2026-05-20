"""V51B Pump v2 IDL constants — generated from vendored official IDL.

Source-of-truth: /root/piggy/data/pump_official_idl/pump.json
Asserted at module-load time against the official discriminators and account
counts. If the vendored IDL ever drifts from the published version, this
module will raise at import; that is intentional.
"""
from __future__ import annotations
import json, hashlib, os
from pathlib import Path
from solders.pubkey import Pubkey

_HERE = Path("/root/piggy/data/pump_official_idl")
_IDL_PATH = _HERE / "pump.json"

with _IDL_PATH.open() as _f:
    _IDL = json.load(_f)

_IX = {i["name"]: i for i in _IDL.get("instructions", [])}

BUY_V2_DISCRIMINATOR = bytes([184, 23, 238, 97, 103, 197, 211, 61])
SELL_V2_DISCRIMINATOR = bytes([93, 246, 130, 60, 231, 233, 64, 178])

assert "buy_v2" in _IX, "buy_v2 missing from IDL"
assert "sell_v2" in _IX, "sell_v2 missing from IDL"
assert bytes(_IX["buy_v2"]["discriminator"]) == BUY_V2_DISCRIMINATOR, "buy_v2 disc mismatch"
assert bytes(_IX["sell_v2"]["discriminator"]) == SELL_V2_DISCRIMINATOR, "sell_v2 disc mismatch"

BUY_V2_ACCOUNTS = [a["name"] for a in _IX["buy_v2"]["accounts"]]
SELL_V2_ACCOUNTS = [a["name"] for a in _IX["sell_v2"]["accounts"]]
assert len(BUY_V2_ACCOUNTS) == 27, f"buy_v2 acc count {len(BUY_V2_ACCOUNTS)} != 27"
assert len(SELL_V2_ACCOUNTS) == 26, f"sell_v2 acc count {len(SELL_V2_ACCOUNTS)} != 26"

BUY_V2_ACCOUNT_FLAGS = [(a["name"], a.get("writable", False), a.get("signer", False)) for a in _IX["buy_v2"]["accounts"]]
SELL_V2_ACCOUNT_FLAGS = [(a["name"], a.get("writable", False), a.get("signer", False)) for a in _IX["sell_v2"]["accounts"]]

BUY_V2_ARGS = [a["name"] for a in _IX["buy_v2"]["args"]]
SELL_V2_ARGS = [a["name"] for a in _IX["sell_v2"]["args"]]
assert BUY_V2_ARGS == ["amount", "max_sol_cost"], f"buy_v2 args mismatch: {BUY_V2_ARGS}"
assert SELL_V2_ARGS == ["amount", "min_sol_output"], f"sell_v2 args mismatch: {SELL_V2_ARGS}"

# Constant program IDs / well-known addresses
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_FEE_PROGRAM_ID = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
NATIVE_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

# Official fee recipient sets (from FEE_RECIPIENTS.md)
NORMAL_FEE_RECIPIENTS = [
    "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV",
    "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ",
    "7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX",
    "9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz",
    "AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY",
    "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM",
    "FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz",
    "G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP",
]
RESERVED_FEE_RECIPIENTS_MAYHEM = [
    "GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS",
    "4budycTjhs9fD6xw62VBducVTNgMgJJ5BgtKq7mAZwn6",
    "8SBKzEQU4nLSzcwF4a74F2iaUDQyTfjGndn6qUWBnrpR",
    "4UQeTP1T39KZ9Sfxzo3WR5skgsaP6NZa87BAkuazLEKH",
    "8sNeir4QsLsJdYpc9RZacohhK1Y5FLU3nC5LXgYB4aa6",
    "Fh9HmeLNUMVCvejxCtCL2DbYaRyBFVJ5xrWkLnMH6fdk",
    "463MEnMeGyJekNZFQSTUABBEbLnvMTALbT6ZmsxAbAdq",
    "6AUH3WEHucYZyC61hqpqYUWVto5qA5hjHuNQ32GNnNxA",
]
BUYBACK_FEE_RECIPIENTS = [
    "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD",
    "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
    "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
    "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
    "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6",
    "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
    "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
    "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
]

NORMAL_FEE_RECIPIENTS_PK = [Pubkey.from_string(s) for s in NORMAL_FEE_RECIPIENTS]
RESERVED_FEE_RECIPIENTS_MAYHEM_PK = [Pubkey.from_string(s) for s in RESERVED_FEE_RECIPIENTS_MAYHEM]
BUYBACK_FEE_RECIPIENTS_PK = [Pubkey.from_string(s) for s in BUYBACK_FEE_RECIPIENTS]

print(f"PGG2-V51B-IDL-ASSERT-PASS=true buy_v2_accs={len(BUY_V2_ACCOUNTS)} sell_v2_accs={len(SELL_V2_ACCOUNTS)}")
