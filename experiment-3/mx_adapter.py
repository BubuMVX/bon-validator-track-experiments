"""
mx_adapter.py — MultiversX Transaction Adapter
===============================================

This module constitutes the terminal stage of the compilation pipeline:
the "last mile" between abstract computation and physical consensus.

After eleven layers of compilation, optimization, analysis, and symbolic
reasoning, all roads converge here: a function that takes a TransactionIntent
and sends one EGLD transfer on the MultiversX network.

The adapter is intentionally thin. Its sole responsibilities are:

  1. Load the Ed25519 keypair from the PEM file specified in the intent
  2. Fetch the current nonce for the sender address
  3. Fetch the chain ID from the network configuration
  4. Construct a Transaction object using the MultiversX Python SDK
  5. Sign the transaction
  6. Submit it to the gateway

The adapter does NOT:
  - Perform any compilation
  - Maintain any state
  - Introduce any further abstraction layers
  - Pretend to be more complex than it is

This simplicity is intentional. The adapter is the only module in this
codebase that admits the existence of the physical world.

Philosophical note
──────────────────
There is a certain irony in the fact that this module — the one that
actually does the work — is the shortest and simplest in the codebase.
Every other module in this pipeline exists to generate the four fields
that are passed to this adapter: (wallet_path, receiver, amount, gas_limit).
The ratio of compiler complexity to adapter complexity is approximately
∞:1, which we consider a satisfactory result.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Optional
import base64

import requests
from multiversx_sdk import Account, Address
from multiversx_sdk.network_providers import ProxyNetworkProvider

from vm import TransactionIntent


# ─────────────────────────────────────────────────────────────────────────────
# SEND RESULT
# ─────────────────────────────────────────────────────────────────────────────

class AdapterError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTER
# ─────────────────────────────────────────────────────────────────────────────

class MultiversXAdapter:
    """
    The MultiversX Transaction Adapter.

    Receives a TransactionIntent and emits a transaction hash.
    This is the only class in this codebase that communicates with the
    external world.
    """

    def __init__(self, gateway_url: str) -> None:
        self._gateway = gateway_url
        self._provider = ProxyNetworkProvider(gateway_url)

    def send(self, intent: TransactionIntent) -> str:
        """
        Materialize a TransactionIntent as an on-chain transaction.

        Returns the transaction hash as a hex string.
        Raises AdapterError on any failure.
        """
        # ── 1. Load wallet ─────────────────────────────────────────────────────
        pem_path = Path(intent.wallet_path)
        if not pem_path.exists():
            raise AdapterError(f"PEM file not found: {pem_path}")

        try:
            account = Account.new_from_pem(pem_path)
        except Exception as e:
            raise AdapterError(f"Failed to load PEM: {e}") from e

        sender_addr = account.address.to_bech32()

        # ── 2. Fetch network config ────────────────────────────────────────────
        try:
            net_config = self._provider.get_network_config()
            chain_id   = net_config.chain_id
        except Exception as e:
            raise AdapterError(f"Cannot fetch network config: {e}") from e

        # ── 3. Fetch sender nonce ──────────────────────────────────────────────
        try:
            acc_data = self._provider.get_account(account.address)
            nonce    = acc_data.nonce
        except Exception as e:
            raise AdapterError(f"Cannot fetch nonce for {sender_addr}: {e}") from e

        # ── 4. Encode receiver ─────────────────────────────────────────────────
        try:
            receiver_addr = Address.new_from_bech32(intent.receiver_addr)
            receiver_bech32 = receiver_addr.to_bech32()
        except Exception as e:
            raise AdapterError(f"Invalid receiver address {intent.receiver_addr!r}: {e}") from e

        # ── 5. Build memo data field ───────────────────────────────────────────
        # The memo is transmitted in the `data` field of the transaction,
        # which MultiversX encodes as base64 in the JSON payload.
        memo_clean = intent.memo.rstrip("\x00")  # strip null padding
        memo_bytes = memo_clean.encode("utf-8") if memo_clean else b""

        # ── 6. Construct signing payload ───────────────────────────────────────
        # We replicate the SDK's TransactionComputer._to_dictionary signing
        # format to maintain full SDK compatibility.
        signing_dict = OrderedDict()
        signing_dict["nonce"]    = nonce
        signing_dict["value"]    = str(intent.amount_atto)
        signing_dict["receiver"] = receiver_bech32
        signing_dict["sender"]   = sender_addr
        signing_dict["gasPrice"] = 1_000_000_000
        signing_dict["gasLimit"] = intent.gas_limit
        if memo_bytes:
            signing_dict["data"] = base64.b64encode(memo_bytes).decode()
        signing_dict["chainID"]  = chain_id
        signing_dict["version"]  = 1

        signing_bytes = json.dumps(signing_dict, separators=(",", ":")).encode("utf-8")

        # ── 7. Sign ────────────────────────────────────────────────────────────
        try:
            signature_hex = account.sign(signing_bytes).hex()
        except Exception as e:
            raise AdapterError(f"Signing failed: {e}") from e

        # ── 8. Build HTTP body ─────────────────────────────────────────────────
        body = {
            "nonce":             nonce,
            "value":             str(intent.amount_atto),
            "receiver":          receiver_bech32,
            "sender":            sender_addr,
            "senderUsername":    "",
            "receiverUsername":  "",
            "gasPrice":          1_000_000_000,
            "gasLimit":          intent.gas_limit,
            "data":              base64.b64encode(memo_bytes).decode() if memo_bytes else "",
            "chainID":           chain_id,
            "version":           1,
            "options":           0,
            "guardian":          "",
            "signature":         signature_hex,
            "guardianSignature": "",
            "relayer":           "",
            "relayerSignature":  "",
        }

        # ── 9. Submit ──────────────────────────────────────────────────────────
        url = f"{self._gateway.rstrip('/')}/transaction/send"
        try:
            resp = requests.post(url, json=body, timeout=15)
            data = resp.json()
        except Exception as e:
            raise AdapterError(f"HTTP POST failed: {e}") from e

        if data.get("code") != "successful":
            error = data.get("error") or str(data)
            raise AdapterError(f"Transaction rejected by gateway: {error}")

        tx_hash = data["data"]["txHash"]
        return tx_hash
