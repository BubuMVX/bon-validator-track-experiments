#!/usr/bin/env python3
"""
fuzzer.py — MultiversX security & stability fuzzer (raw payload mode)

Bypasses the multiversx-sdk validation layer entirely.
Transactions are crafted as raw JSON dicts, signed via Ed25519, and sent
directly to the gateway REST API — so the node/gateway itself is the
first line of validation.

Categories tested:
  1. Nonce     — overflow, underflow, gaps, replay
  2. Value     — u64/u128/u256 max, negative, float-as-string
  3. Gas       — gasLimit and gasPrice edge cases
  4. Data      — empty, null bytes, 10 KB, 100 KB, 1 MB, invalid UTF-8
  5. Signature — zeros, truncated, oversized, random, empty, unsigned
  6. Chain ID  — empty, wrong network, very long, null byte
  7. Version / Options — zero, oversized, reserved bits

Usage:
    python fuzzer.py --wallet ./bubu.pem --network https://gateway.battleofnodes.com
    python fuzzer.py --wallet ./bubu.pem --network https://gateway.battleofnodes.com --category signature
    python fuzzer.py --wallet ./bubu.pem --network https://gateway.battleofnodes.com --delay 0.1
"""

import argparse
import base64
import json
import os
import sys
import time
from collections import OrderedDict
from typing import Any, Optional

import requests
from multiversx_sdk import Account, Address
from multiversx_sdk.network_providers import ProxyNetworkProvider
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

# ── Constants ──────────────────────────────────────────────────────────────────

U64_MAX  = (1 << 64) - 1
U64_WRAP = (1 << 64)
U128_MAX = (1 << 128) - 1
U256_MAX = (1 << 256) - 1

GAS_NORMAL = 56_000

console = Console()

progress = Progress(
    SpinnerColumn(),
    TextColumn("[bold cyan]{task.description}"),
    BarColumn(bar_width=30),
    MofNCompleteColumn(),
    TextColumn("[dim]{task.fields[label]}"),
    TimeElapsedColumn(),
    console=console,
    transient=False,
)


# ── Result tracking ────────────────────────────────────────────────────────────

class Results:
    def __init__(self):
        self.rows: list[tuple[str, str, str, str]] = []
        self.accepted = 0
        self.rejected = 0
        self.build_err = 0

    def add(self, category: str, label: str, status: str, detail: str):
        self.rows.append((category, label, status, detail))
        if status == "ACCEPTED":
            self.accepted += 1
        elif status == "REJECTED":
            self.rejected += 1
        else:
            self.build_err += 1


results = Results()


# ── Raw payload builder ────────────────────────────────────────────────────────

def _build_signing_payload(
    sender: str,
    receiver: str,
    nonce: Any,
    value: Any,
    gas_price: Any,
    gas_limit: Any,
    data: Optional[bytes],
    chain_id: Any,
    version: Any,
    options: Any,
) -> bytes:
    """
    Replicates TransactionComputer._to_dictionary + _dict_to_json
    but WITHOUT the _ensure_fields() validation gate.

    - value   → always str(value) as the protocol expects
    - data    → base64 encoded if non-empty, omitted otherwise
    - version → omitted if falsy (0 / None)
    - options → omitted if falsy
    """
    d: dict[str, Any] = OrderedDict()
    d["nonce"]    = nonce
    d["value"]    = str(value)
    d["receiver"] = receiver
    d["sender"]   = sender
    d["gasPrice"] = gas_price
    d["gasLimit"] = gas_limit
    if data:
        d["data"] = base64.b64encode(data).decode()
    d["chainID"]  = chain_id
    if version:
        d["version"] = version
    if options:
        d["options"] = options
    return json.dumps(d, separators=(",", ":")).encode("utf-8")


def _build_http_body(
    sender: str,
    receiver: str,
    nonce: Any,
    value: Any,
    gas_price: Any,
    gas_limit: Any,
    data: Optional[bytes],
    chain_id: Any,
    version: Any,
    options: Any,
    signature_hex: str,
) -> dict:
    """
    Replicates Transaction.to_dictionary but without validation.
    Mirrors the exact field set the gateway expects.
    """
    return {
        "nonce":             nonce,
        "value":             str(value),
        "receiver":          receiver,
        "sender":            sender,
        "senderUsername":    "",
        "receiverUsername":  "",
        "gasPrice":          gas_price,
        "gasLimit":          gas_limit,
        "data":              base64.b64encode(data).decode() if data else "",
        "chainID":           chain_id,
        "version":           version,
        "options":           options,
        "guardian":          "",
        "signature":         signature_hex,
        "guardianSignature": "",
        "relayer":           "",
        "relayerSignature":  "",
    }


def raw_send(network_url: str, body: dict) -> str:
    """POST directly to /transaction/send — bypass ProxyNetworkProvider entirely."""
    url = f"{network_url.rstrip('/')}/transaction/send"
    resp = requests.post(url, json=body, timeout=10)
    payload = resp.json()
    if payload.get("code") != "successful":
        raise Exception(payload.get("error") or payload)
    return payload["data"]["txHash"]


def send_raw(
    account: Account,
    network_url: str,
    category: str,
    label: str,
    delay: float,
    task_id: Any = None,
    *,
    sender: Optional[str] = None,
    receiver: Optional[str] = None,
    nonce: Any = 0,
    value: Any = 1,
    gas_price: Any = 1_000_000_000,
    gas_limit: Any = GAS_NORMAL,
    data: Optional[bytes] = b"fuzz",
    chain_id: Any = "1",
    version: Any = 1,
    options: Any = 0,
    override_sig: Optional[str] = None,
    skip_sig: bool = False,
) -> None:
    if task_id is not None:
        progress.update(task_id, label=label)

    addr = account.address.to_bech32()
    _sender   = sender   or addr
    _receiver = receiver or addr

    try:
        signing_payload = _build_signing_payload(
            sender=_sender, receiver=_receiver,
            nonce=nonce, value=value,
            gas_price=gas_price, gas_limit=gas_limit,
            data=data, chain_id=chain_id,
            version=version, options=options,
        )

        if override_sig is not None:
            sig_hex = override_sig
        elif skip_sig:
            sig_hex = ""
        else:
            sig_hex = account.sign(signing_payload).hex()

        body = _build_http_body(
            sender=_sender, receiver=_receiver,
            nonce=nonce, value=value,
            gas_price=gas_price, gas_limit=gas_limit,
            data=data, chain_id=chain_id,
            version=version, options=options,
            signature_hex=sig_hex,
        )

        tx_hash = raw_send(network_url, body)
        results.add(category, label, "ACCEPTED", tx_hash)

    except Exception as exc:
        err = str(exc)
        results.add(category, label, "REJECTED", err[:100])

    if task_id is not None:
        progress.advance(task_id)

    time.sleep(delay)


# ── Section header helper ──────────────────────────────────────────────────────

def section(title: str) -> None:
    console.print(f"\n[bold cyan]━━  {title}  ━━[/bold cyan]")


# ── Test suites ────────────────────────────────────────────────────────────────

def test_nonce(account, network_url, chain_id, nonce, delay):

    cases = [
        ("valid (current_nonce)",       dict(nonce=nonce)),
        ("nonce + 1 (gap=1)",           dict(nonce=nonce + 1)),
        ("nonce + 10000 (large gap)",   dict(nonce=nonce + 10_000)),
        ("nonce - 1 (replay)",          dict(nonce=max(0, nonce - 1))),
        ("u64_max (2^64 - 1)",          dict(nonce=U64_MAX)),
        ("u64_wrap (2^64)",             dict(nonce=U64_WRAP)),
        ("u64_wrap + 1 (2^64+1)",       dict(nonce=U64_WRAP + 1)),
        ("nonce + 2^64 (mod wrap)",     dict(nonce=nonce + U64_WRAP)),
        ("nonce + 2*2^64",              dict(nonce=nonce + 2 * U64_WRAP)),
        ("negative nonce (-1)",         dict(nonce=-1)),
        ("negative nonce (-2^63)",      dict(nonce=-(1 << 63))),
    ]
    task = progress.add_task("nonce        ", total=len(cases), label="")
    for label, kwargs in cases:
        send_raw(account=account, network_url=network_url, category="nonce",
                 label=label, delay=delay, task_id=task, chain_id=chain_id, **kwargs)


def test_value(account, network_url, chain_id, nonce, delay):

    cases = [
        ("value = 0",                   dict(value=0)),
        ("value = 1",                   dict(value=1)),
        ("value = u64_max",             dict(value=U64_MAX)),
        ("value = 2^64 (u64_wrap)",     dict(value=U64_WRAP)),
        ("value = u128_max",            dict(value=U128_MAX)),
        ("value = u256_max",            dict(value=U256_MAX)),
        ("value = -1 (negative)",       dict(value=-1)),
        ("value = 'abc' (non-numeric)", dict(value="abc")),
        ("value = 1.5 (float string)",  dict(value="1.5")),
    ]
    task = progress.add_task("value        ", total=len(cases), label="")
    for label, kwargs in cases:
        send_raw(account=account, network_url=network_url, category="value",
                 label=label, delay=delay, task_id=task, chain_id=chain_id, nonce=nonce, **kwargs)


def test_gas(account, network_url, chain_id, nonce, delay):

    cases = [
        ("gasLimit = 0",             dict(gas_limit=0)),
        ("gasLimit = 1",             dict(gas_limit=1)),
        ("gasLimit = u64_max",       dict(gas_limit=U64_MAX)),
        ("gasLimit = 2^64 (wrap)",   dict(gas_limit=U64_WRAP)),
        ("gasLimit = -1",            dict(gas_limit=-1)),
        ("gasPrice = 0",             dict(gas_price=0)),
        ("gasPrice = 1 (below min)", dict(gas_price=1)),
        ("gasPrice = u64_max",       dict(gas_price=U64_MAX)),
        ("gasPrice = -1",            dict(gas_price=-1)),
    ]
    task = progress.add_task("gas          ", total=len(cases), label="")
    for label, kwargs in cases:
        send_raw(account=account, network_url=network_url, category="gas",
                 label=label, delay=delay, task_id=task, chain_id=chain_id, nonce=nonce, **kwargs)


def test_data(account, network_url, chain_id, nonce, delay):

    cases = [
        ("data = empty (b'')",           b""),
        ("data = null bytes (32×\\x00)", b"\x00" * 32),
        ("data = invalid UTF-8",         bytes(range(128, 256))),
        ("data = 10 KB",                 os.urandom(10_240)),
        ("data = 100 KB",                os.urandom(102_400)),
        ("data = 1 MB",                  os.urandom(1_048_576)),
    ]
    task = progress.add_task("data         ", total=len(cases), label="")
    for label, d in cases:
        send_raw(account=account, network_url=network_url, category="data",
                 label=label, delay=delay, task_id=task, chain_id=chain_id, nonce=nonce, data=d)


def test_signature(account, network_url, chain_id, nonce, delay):

    cases_override = [
        ("sig = 64 zero bytes",      dict(override_sig="00" * 64)),
        ("sig = 32 bytes (short)",   dict(override_sig="00" * 32)),
        ("sig = 128 bytes (long)",   dict(override_sig="ff" * 128)),
        ("sig = 1 byte",             dict(override_sig="01")),
        ("sig = random 64 bytes",    dict(override_sig=os.urandom(64).hex())),
        ("sig = empty string",       dict(override_sig="")),
        ("no sig (skip_sig=True)",   dict(skip_sig=True)),
    ]
    task = progress.add_task("signature    ", total=len(cases_override), label="")
    for label, kwargs in cases_override:
        send_raw(account=account, network_url=network_url, category="signature",
                 label=label, delay=delay, task_id=task, chain_id=chain_id, nonce=nonce, **kwargs)


def test_chain_id(account, network_url, chain_id, nonce, delay):

    cases = [
        ("chainID = valid",          dict(chain_id=chain_id)),
        ("chainID = empty string",   dict(chain_id="")),
        ("chainID = '1' (mainnet)",  dict(chain_id="1")),
        ("chainID = 'T' (testnet)",  dict(chain_id="T")),
        ("chainID = 'D' (devnet)",   dict(chain_id="D")),
        ("chainID = wrong (ZZZ)",    dict(chain_id="ZZZ")),
        ("chainID = 1000 chars",     dict(chain_id="X" * 1000)),
        ("chainID = null byte",      dict(chain_id="\x00")),
        ("chainID = integer 1",      dict(chain_id=1)),
    ]
    task = progress.add_task("chain_id     ", total=len(cases), label="")
    for label, kwargs in cases:
        send_raw(account=account, network_url=network_url, category="chain_id",
                 label=label, delay=delay, task_id=task, nonce=nonce, **kwargs)


def test_version_options(account, network_url, chain_id, nonce, delay):

    cases = [
        ("version=0, options=0",     dict(version=0,       options=0)),
        ("version=2, options=0",     dict(version=2,       options=0)),
        ("version=255",              dict(version=255,     options=0)),
        ("version=65535",            dict(version=65535,   options=0)),
        ("version=-1",               dict(version=-1,      options=0)),
        ("options=0xFF",             dict(version=1,       options=0xFF)),
        ("options=0xFFFF",           dict(version=1,       options=0xFFFF)),
        ("version=0, options=1",     dict(version=0,       options=1)),
        ("version=u64_max",          dict(version=U64_MAX, options=0)),
    ]
    task = progress.add_task("version/opts ", total=len(cases), label="")
    for label, kwargs in cases:
        send_raw(account=account, network_url=network_url, category="version/options",
                 label=label, delay=delay, task_id=task, chain_id=chain_id, nonce=nonce, **kwargs)


# ── Results display ────────────────────────────────────────────────────────────

def print_results() -> None:
    console.print()
    table = Table(
        title="Results",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold white",
        title_style="bold white",
    )
    table.add_column("Category",  style="dim cyan",  no_wrap=True, min_width=16)
    table.add_column("Test case", style="white",     min_width=32)
    table.add_column("Status",    style="bold",      no_wrap=True, min_width=10)
    table.add_column("Detail",    style="dim",       min_width=20)

    STATUS_STYLE = {
        "ACCEPTED": "[bold green]ACCEPTED[/bold green]",
        "REJECTED": "[bold red]REJECTED[/bold red]",
        "ERR":      "[bold yellow]ERR     [/bold yellow]",
    }

    for category, label, status, detail in results.rows:
        table.add_row(
            category,
            label,
            STATUS_STYLE.get(status, status),
            detail,
        )

    console.print(table)
    console.print(
        Panel(
            f"[green]ACCEPTED : {results.accepted}[/green]   "
            f"[red]REJECTED : {results.rejected}[/red]   "
            f"[yellow]ERR      : {results.build_err}[/yellow]   "
            f"[white]TOTAL    : {len(results.rows)}[/white]",
            title="Summary",
            box=box.ROUNDED,
        )
    )


# ── Main ───────────────────────────────────────────────────────────────────────

CATEGORIES = ("nonce", "value", "gas", "data", "signature", "chain_id", "version")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MultiversX raw-payload security fuzzer",
    )
    parser.add_argument("--wallet",   required=True, help="PEM wallet file")
    parser.add_argument("--network",  required=True, help="MultiversX gateway URL")
    parser.add_argument(
        "--delay", type=float, default=0.3,
        help="Seconds between sends (default: 0.3)",
    )
    parser.add_argument(
        "--category", choices=CATEGORIES, default=None,
        help="Run only one category (default: all)",
    )
    args = parser.parse_args()

    # ── Load wallet ────────────────────────────────────────────────────────────
    from pathlib import Path
    pem_path = Path(args.wallet)
    if not pem_path.exists():
        console.print(f"[red]Error:[/red] PEM not found: {args.wallet}")
        sys.exit(1)

    account = Account.new_from_pem(pem_path)

    # ── Connect ────────────────────────────────────────────────────────────────
    provider = ProxyNetworkProvider(args.network)
    try:
        network_config = provider.get_network_config()
        chain_id = network_config.chain_id
    except Exception as e:
        console.print(f"[red]Error:[/red] cannot reach network: {e}")
        sys.exit(1)

    try:
        current_nonce = provider.get_account(account.address).nonce
    except Exception as e:
        console.print(f"[red]Error:[/red] cannot fetch nonce: {e}")
        sys.exit(1)

    console.print(Panel(
        f"[bold]Wallet :[/bold]  {account.address.to_bech32()}\n"
        f"[bold]Network:[/bold]  {args.network}  [dim](chain_id={chain_id})[/dim]\n"
        f"[bold]Nonce  :[/bold]  {current_nonce}\n"
        f"[bold]Mode   :[/bold]  [yellow]raw HTTP POST — SDK validation bypassed[/yellow]\n"
        f"[bold]Delay  :[/bold]  {args.delay}s between sends",
        title="[bold cyan]MultiversX fuzzer[/bold cyan]",
        box=box.ROUNDED,
    ))

    # ── Run suites ─────────────────────────────────────────────────────────────
    kw = dict(
        account=account,
        network_url=args.network,
        chain_id=chain_id,
        nonce=current_nonce,
        delay=args.delay,
    )

    suites = {
        "nonce":    test_nonce,
        "value":    test_value,
        "gas":      test_gas,
        "data":     test_data,
        "signature": test_signature,
        "chain_id": test_chain_id,
        "version":  test_version_options,
    }

    with progress:
        if args.category:
            suites[args.category](**kw)
        else:
            for fn in suites.values():
                fn(**kw)

    print_results()


if __name__ == "__main__":
    main()
