#!/usr/bin/env python3
"""
cli.py — Cross-Shard EGLD Ping-Pong Experiment Runner
MultiversX Battle of Nodes — Challenge 8, Experiment 2

Usage:
    python cli.py init --network <gateway_url> --master-pem <path> [--api <api_url>] [--reset]
    python cli.py run  --network <gateway_url> [--api <api_url>] [--profile <name>]

Full documentation: README.md
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Third-party ───────────────────────────────────────────────────────────────
import requests
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

# ── MultiversX SDK ────────────────────────────────────────────────────────────
from multiversx_sdk import (
    Account,
    Address,
    SmartContractTransactionsFactory,
    TransactionsFactoryConfig,
    TransactionComputer,
    ProxyNetworkProvider,
)
from multiversx_sdk.abi import Abi
from multiversx_sdk.wallet import UserPEM, UserSecretKey

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent
WALLETS_DIR   = BASE_DIR / "wallets"
STATE_DIR     = BASE_DIR / "state"
LOGS_DIR      = BASE_DIR / "logs"
STATE_FILE    = STATE_DIR / "config.json"
CONTRACT_WASM = BASE_DIR / "contracts" / "ping_pong_forwarder.wasm"
CONTRACT_ABI  = BASE_DIR / "contracts" / "ping_pong_forwarder.abi.json"

# 1 EGLD in attoEGLD
ONE_EGLD = 10 ** 18

# Funding amount per wallet (1 EGLD)
FUND_AMOUNT = ONE_EGLD

# Minimum EGLD kept in wallet to cover gas fees (0.1 EGLD)
GAS_RESERVE = ONE_EGLD // 10

# Gas limits
GAS_DEPLOY          = 60_000_000
GAS_SET_PEER        = 10_000_000
GAS_START_PING_PONG = 100_000_000

# Target shards
TARGET_SHARDS = [0, 1, 2]

# Poll intervals
TX_POLL_INTERVAL  = 3   # seconds between tx status checks
TX_POLL_TIMEOUT   = 120 # seconds before giving up on a tx
EVENT_POLL_INTERVAL = 5  # seconds between event polls
EVENT_POLL_TIMEOUT  = 600 # seconds max wait for hop events

console = Console()


# ═════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def now_ms() -> int:
    """Current UTC timestamp in milliseconds."""
    return int(time.time() * 1000)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_correlation_id() -> str:
    """Generate a unique 16-byte hex correlation ID."""
    return secrets.token_hex(16)


def log_human(log_path: Path, message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")


def log_jsonl(jsonl_path: Path, record: dict) -> None:
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def load_state() -> dict:
    if not STATE_FILE.exists():
        console.print("[red]Error:[/red] state/config.json not found. Run 'init' first.")
        sys.exit(1)
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def get_shard_for_address(address: Address, provider: ProxyNetworkProvider) -> int:
    """
    Use the SDK/network to determine the shard of an address.
    We query the gateway's /address/<bech32> endpoint which returns
    the shard in the account data.
    Falls back to address-computed shard if the account doesn't exist yet.
    """
    bech32 = address.to_bech32()
    url = f"{provider.url}/address/{bech32}"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("code") == "successful":
            # The gateway returns shardID in account data
            account_data = data.get("data", {}).get("account", {})
            if "shardID" in account_data:
                return int(account_data["shardID"])
    except Exception:
        pass

    # Fallback: compute shard from address bytes (last byte masks)
    # MultiversX shard assignment: last 3 bytes of pubkey, modulo num_shards
    # This is an approximation — the SDK does not expose this directly
    raw = address.get_public_key()
    shard_id = int.from_bytes(raw[-3:], "big") % 3
    return shard_id


def wait_for_tx(provider: ProxyNetworkProvider, tx_hash: str, label: str = "") -> dict:
    """Poll until a transaction is finalized. Returns the tx result dict."""
    deadline = time.time() + TX_POLL_TIMEOUT
    while time.time() < deadline:
        try:
            url = f"{provider.url}/transaction/{tx_hash}?withResults=true"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if data.get("code") == "successful":
                tx_data = data.get("data", {}).get("transaction", {})
                status = tx_data.get("status", "")
                if status in ("success", "fail", "invalid"):
                    return tx_data
        except Exception:
            pass
        time.sleep(TX_POLL_INTERVAL)

    console.print(f"[yellow]Warning:[/yellow] tx {tx_hash} ({label}) did not finalize within {TX_POLL_TIMEOUT}s")
    return {}


def get_account_nonce(provider: ProxyNetworkProvider, address: Address) -> int:
    url = f"{provider.url}/address/{address.to_bech32()}/nonce"
    resp = requests.get(url, timeout=10)
    data = resp.json()
    return data["data"]["nonce"]


def send_transaction_raw(provider: ProxyNetworkProvider, tx_dict: dict) -> str:
    """POST a signed transaction dict to the gateway. Returns tx_hash."""
    url = f"{provider.url}/transaction/send"
    resp = requests.post(url, json=tx_dict, timeout=15)
    data = resp.json()
    if data.get("code") != "successful":
        raise RuntimeError(f"Transaction rejected: {data.get('error') or data}")
    return data["data"]["txHash"]


def build_and_sign_tx(
    account: Account,
    receiver: str,
    value: int,
    gas_limit: int,
    data: bytes,
    chain_id: str,
    nonce: int,
) -> dict:
    """Build a transaction dictionary, sign it, and return the HTTP body."""
    import base64
    from collections import OrderedDict

    d = OrderedDict()
    d["nonce"]    = nonce
    d["value"]    = str(value)
    d["receiver"] = receiver
    d["sender"]   = account.address.to_bech32()
    d["gasPrice"] = 1_000_000_000
    d["gasLimit"] = gas_limit
    if data:
        d["data"] = base64.b64encode(data).decode()
    d["chainID"]  = chain_id
    d["version"]  = 1

    signing_bytes = json.dumps(d, separators=(",", ":")).encode("utf-8")
    sig_hex = account.sign(signing_bytes).hex()

    return {
        "nonce":             nonce,
        "value":             str(value),
        "receiver":          receiver,
        "sender":            account.address.to_bech32(),
        "senderUsername":    "",
        "receiverUsername":  "",
        "gasPrice":          1_000_000_000,
        "gasLimit":          gas_limit,
        "data":              base64.b64encode(data).decode() if data else "",
        "chainID":           chain_id,
        "version":           1,
        "options":           0,
        "guardian":          "",
        "signature":         sig_hex,
        "guardianSignature": "",
        "relayer":           "",
        "relayerSignature":  "",
    }


def abi_encode_start_ping_pong(correlation_id: str, max_hops: int) -> bytes:
    """
    ABI-encode arguments for startPingPong(correlation_id: ManagedBuffer, max_hops: u64).
    MultiversX ABI encoding for SC call data:
      functionName@arg1_hex@arg2_hex
    ManagedBuffer → raw hex of bytes
    u64           → big-endian hex, no leading zeros (min 1 byte)
    """
    cid_hex = correlation_id.encode().hex()
    hops_hex = format(max_hops, 'x') if max_hops > 0 else '00'
    if len(hops_hex) % 2 != 0:
        hops_hex = '0' + hops_hex
    call_data = f"startPingPong@{cid_hex}@{hops_hex}"
    return call_data.encode()


def abi_encode_set_peer(peer_address: Address) -> bytes:
    """ABI-encode argument for setPeer(peer_address: ManagedAddress)."""
    peer_hex = peer_address.get_public_key().hex()
    call_data = f"setPeer@{peer_hex}"
    return call_data.encode()


def deploy_contract_data(wasm_bytes: bytes) -> bytes:
    """
    Build the deploy transaction data field.
    Format: <wasm_hex>@0500@0000
    @0500 = VM type (WASM VM = 05, reserved = 00)
    @0000 = upgradeable flag
    """
    wasm_hex = wasm_bytes.hex()
    return f"{wasm_hex}@0500@0000".encode()


def deploy_contract_with_init(wasm_bytes: bytes, peer_address: Address) -> bytes:
    """
    Build deploy data with init argument: peer_address.
    Format: <wasm_hex>@0500@0000@<peer_hex>
    """
    wasm_hex = wasm_bytes.hex()
    peer_hex = peer_address.get_public_key().hex()
    return f"{wasm_hex}@0500@0000@{peer_hex}".encode()


def get_deployed_contract_address(tx_result: dict) -> Optional[str]:
    """Extract the deployed contract address from a deploy transaction result."""
    logs = tx_result.get("logs", {})
    events = logs.get("events", [])
    for event in events:
        identifier = event.get("identifier", "")
        if identifier == "SCDeploy":
            topics = event.get("topics", [])
            if topics:
                import base64
                raw = base64.b64decode(topics[0] + "==")
                addr = Address(raw, "erd")
                return addr.to_bech32()
    # Alternative: smartContractResults
    scrs = tx_result.get("smartContractResults", [])
    for scr in scrs:
        if scr.get("isSmartContractResult") and scr.get("receiver", "").startswith("erd1"):
            # The first SCR with a new SC address is typically the deployed one
            nonce_val = scr.get("nonce", -1)
            if nonce_val == 0:
                return scr.get("receiver", "")
    return None


# ═════════════════════════════════════════════════════════════════════════════
# INIT COMMAND
# ═════════════════════════════════════════════════════════════════════════════

def cmd_init(args: argparse.Namespace) -> None:
    """
    Initialize wallets, fund them, deploy contracts on each shard,
    wire up the ring topology, and save state.
    """
    console.rule("[bold cyan]INIT — Cross-Shard Ping-Pong Setup[/bold cyan]")

    # ── Guard: state already exists ───────────────────────────────────────────
    if STATE_FILE.exists() and not args.reset:
        console.print(
            "[red]Error:[/red] state/config.json already exists.\n"
            "Use [bold]--reset[/bold] to overwrite."
        )
        sys.exit(1)

    # ── Create directories ────────────────────────────────────────────────────
    WALLETS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Validate contract artifacts ───────────────────────────────────────────
    if not CONTRACT_WASM.exists():
        console.print(f"[red]Error:[/red] {CONTRACT_WASM} not found.\nBuild the contract first: mxpy contract build")
        sys.exit(1)
    if not CONTRACT_ABI.exists():
        console.print(f"[red]Error:[/red] {CONTRACT_ABI} not found.")
        sys.exit(1)

    wasm_bytes = CONTRACT_WASM.read_bytes()
    console.print(f"[green]✓[/green] Contract WASM loaded: {len(wasm_bytes):,} bytes")

    # ── Connect to network ────────────────────────────────────────────────────
    provider = ProxyNetworkProvider(args.network)
    try:
        net_config = provider.get_network_config()
        chain_id = net_config.chain_id
        console.print(f"[green]✓[/green] Connected to network: [bold]{args.network}[/bold]  chain={chain_id}")
    except Exception as e:
        console.print(f"[red]Error:[/red] Cannot connect to gateway: {e}")
        sys.exit(1)

    # ── Load master wallet ────────────────────────────────────────────────────
    master_pem_path = Path(args.master_pem)
    if not master_pem_path.exists():
        console.print(f"[red]Error:[/red] Master PEM not found: {args.master_pem}")
        sys.exit(1)

    master = Account.new_from_pem(master_pem_path)
    master_addr = master.address.to_bech32()

    try:
        master_account = provider.get_account(master.address)
        master_balance = int(str(master_account.balance))
        master_nonce   = master_account.nonce
    except Exception as e:
        console.print(f"[red]Error:[/red] Cannot fetch master account: {e}")
        sys.exit(1)

    required_egld = 3 * FUND_AMOUNT
    console.print(
        f"[green]✓[/green] Master wallet: [bold]{master_addr}[/bold]\n"
        f"   Balance: {master_balance / ONE_EGLD:.4f} EGLD  |  Nonce: {master_nonce}\n"
        f"   Required to fund: {required_egld / ONE_EGLD:.1f} EGLD"
    )

    if master_balance < required_egld + GAS_RESERVE:
        console.print(
            f"[red]Error:[/red] Insufficient master balance.\n"
            f"Need at least {(required_egld + GAS_RESERVE) / ONE_EGLD:.2f} EGLD, "
            f"have {master_balance / ONE_EGLD:.4f} EGLD"
        )
        sys.exit(1)

    # ── Generate wallets — one per shard ──────────────────────────────────────
    console.print("\n[bold]Step 1/5:[/bold] Generating wallets (one per shard 0, 1, 2)...")

    shard_wallets: dict[int, dict] = {}  # shard_id → {account, pem_path, ...}
    max_attempts = 500
    attempts = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Generating shard wallets...", total=3)

        while len(shard_wallets) < 3 and attempts < max_attempts:
            attempts += 1

            # Generate a new random keypair
            secret_key_bytes = os.urandom(32)
            secret_key = UserSecretKey(secret_key_bytes)
            pem = UserPEM(label="generated", secret_key=secret_key)

            public_key = secret_key.generate_public_key()
            address = Address(public_key.buffer, "erd")

            # Detect shard using SDK/network
            shard_id = get_shard_for_address(address, provider)

            if shard_id not in shard_wallets:
                pem_filename = f"wallet_shard{shard_id}.pem"
                pem_path = WALLETS_DIR / pem_filename
                pem.save(pem_path)

                shard_wallets[shard_id] = {
                    "address":  address.to_bech32(),
                    "shard_id": shard_id,
                    "pem_path": str(pem_path),
                    "account":  Account.new_from_pem(pem_path),
                }

                progress.advance(task)
                console.print(
                    f"   [green]✓[/green] Shard {shard_id}: {address.to_bech32()}"
                    f"  [dim](attempt #{attempts})[/dim]"
                )

    if len(shard_wallets) < 3:
        console.print(f"[red]Error:[/red] Could not generate wallets for all 3 shards after {max_attempts} attempts")
        sys.exit(1)

    console.print(f"[green]✓[/green] All 3 shard wallets generated in {attempts} attempts")

    # ── Fund wallets from master ───────────────────────────────────────────────
    console.print("\n[bold]Step 2/5:[/bold] Funding wallets (1 EGLD each)...")

    current_nonce = master_nonce
    fund_hashes: list[str] = []

    for shard_id in sorted(shard_wallets.keys()):
        wallet_info = shard_wallets[shard_id]
        target_addr = wallet_info["address"]

        tx_body = build_and_sign_tx(
            account=master,
            receiver=target_addr,
            value=FUND_AMOUNT,
            gas_limit=50_000,
            data=b"fund",
            chain_id=chain_id,
            nonce=current_nonce,
        )

        try:
            tx_hash = send_transaction_raw(provider, tx_body)
            fund_hashes.append(tx_hash)
            wallet_info["fund_tx_hash"] = tx_hash
            console.print(
                f"   [green]✓[/green] Funded shard {shard_id} wallet: "
                f"{tx_hash}  [dim]({FUND_AMOUNT / ONE_EGLD:.1f} EGLD)[/dim]"
            )
            current_nonce += 1
        except Exception as e:
            console.print(f"   [red]✗[/red] Failed to fund shard {shard_id}: {e}")
            sys.exit(1)

    # Wait for funding transactions
    console.print("   Waiting for funding transactions to finalize...")
    for i, (shard_id, tx_hash) in enumerate(zip(sorted(shard_wallets.keys()), fund_hashes)):
        console.print(f"   Waiting for fund tx {tx_hash[:16]}...")
        result = wait_for_tx(provider, tx_hash, f"fund shard {shard_id}")
        status = result.get("status", "unknown")
        if status != "success":
            console.print(f"   [red]Warning:[/red] Fund tx for shard {shard_id} status: {status}")

    console.print("[green]✓[/green] Funding complete")

    # ── Deploy contracts ───────────────────────────────────────────────────────
    console.print("\n[bold]Step 3/5:[/bold] Deploying contracts...")

    # We deploy with a placeholder peer (self address, will be corrected in set_peer step).
    # This is necessary because we don't know all addresses until all contracts are deployed.
    contract_addresses: dict[int, str] = {}
    deploy_hashes: dict[int, str] = {}

    for shard_id in sorted(shard_wallets.keys()):
        wallet_info = shard_wallets[shard_id]
        deployer: Account = wallet_info["account"]

        # Fetch nonce for deployer
        try:
            deployer_account = provider.get_account(deployer.address)
            deployer_nonce = deployer_account.nonce
        except Exception as e:
            console.print(f"   [red]✗[/red] Cannot fetch nonce for shard {shard_id} wallet: {e}")
            sys.exit(1)

        # Deploy with a placeholder peer = zero address
        # The peer will be set via setPeer() after all deployments
        placeholder_peer = Address(bytes(32), "erd")
        deploy_data = deploy_contract_with_init(wasm_bytes, placeholder_peer)

        tx_body = build_and_sign_tx(
            account=deployer,
            receiver="erd1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq6gq4hu",  # SC deploy receiver
            value=0,
            gas_limit=GAS_DEPLOY,
            data=deploy_data,
            chain_id=chain_id,
            nonce=deployer_nonce,
        )

        try:
            tx_hash = send_transaction_raw(provider, tx_body)
            deploy_hashes[shard_id] = tx_hash
            console.print(
                f"   [green]✓[/green] Deploy tx for shard {shard_id}: {tx_hash}"
            )
        except Exception as e:
            console.print(f"   [red]✗[/red] Deploy failed for shard {shard_id}: {e}")
            sys.exit(1)

    # Wait for deploy transactions and extract contract addresses
    console.print("   Waiting for deploy transactions to finalize...")
    for shard_id, tx_hash in deploy_hashes.items():
        console.print(f"   Waiting for deploy tx {tx_hash[:16]}...")
        result = wait_for_tx(provider, tx_hash, f"deploy shard {shard_id}")
        status = result.get("status", "unknown")
        if status != "success":
            console.print(f"   [yellow]Warning:[/yellow] Deploy for shard {shard_id} status={status}")

        contract_addr = get_deployed_contract_address(result)
        if not contract_addr:
            # Try to derive it from the sender address + nonce
            # MultiversX: contract address = hash(sender_pubkey + nonce)
            console.print(f"   [yellow]Warning:[/yellow] Could not extract contract address from tx logs for shard {shard_id}")
            console.print(f"   Trying to fetch from transaction SCRs...")
            # Give it one more attempt via API
            time.sleep(5)
            result2 = wait_for_tx(provider, tx_hash, f"deploy shard {shard_id} retry")
            contract_addr = get_deployed_contract_address(result2)

        if contract_addr:
            contract_addresses[shard_id] = contract_addr
            shard_wallets[shard_id]["contract_address"] = contract_addr
            shard_wallets[shard_id]["deploy_tx_hash"] = tx_hash
            console.print(
                f"   [green]✓[/green] Shard {shard_id} contract: [bold]{contract_addr}[/bold]"
            )
        else:
            console.print(
                f"   [red]✗[/red] Could not determine contract address for shard {shard_id}.\n"
                f"   Check tx {tx_hash} manually and update state/config.json"
            )
            shard_wallets[shard_id]["contract_address"] = "UNKNOWN"
            shard_wallets[shard_id]["deploy_tx_hash"] = tx_hash

    # ── Wire ring topology via setPeer ─────────────────────────────────────────
    console.print("\n[bold]Step 4/5:[/bold] Wiring ring topology (setPeer)...")
    console.print("   Ring: shard0 → shard1 → shard2 → shard0")

    set_peer_hashes: dict[int, str] = {}

    for shard_id in sorted(contract_addresses.keys()):
        # Each contract's peer = contract on the next shard (cyclically)
        next_shard = (shard_id + 1) % 3
        if next_shard not in contract_addresses:
            console.print(f"   [yellow]Skip:[/yellow] Next shard {next_shard} has no contract address")
            continue

        peer_addr_str = contract_addresses[next_shard]
        if peer_addr_str == "UNKNOWN":
            console.print(f"   [yellow]Skip:[/yellow] Peer contract for shard {shard_id} is UNKNOWN")
            continue

        peer_address = Address.new_from_bech32(peer_addr_str)
        call_data = abi_encode_set_peer(peer_address)

        wallet_info = shard_wallets[shard_id]
        caller: Account = wallet_info["account"]

        try:
            caller_account = provider.get_account(caller.address)
            caller_nonce = caller_account.nonce
        except Exception as e:
            console.print(f"   [red]✗[/red] Cannot fetch nonce for shard {shard_id}: {e}")
            continue

        contract_addr = contract_addresses[shard_id]

        tx_body = build_and_sign_tx(
            account=caller,
            receiver=contract_addr,
            value=0,
            gas_limit=GAS_SET_PEER,
            data=call_data,
            chain_id=chain_id,
            nonce=caller_nonce,
        )

        try:
            tx_hash = send_transaction_raw(provider, tx_body)
            set_peer_hashes[shard_id] = tx_hash
            console.print(
                f"   [green]✓[/green] setPeer shard {shard_id} → shard {next_shard}: {tx_hash}"
            )
        except Exception as e:
            console.print(f"   [red]✗[/red] setPeer failed for shard {shard_id}: {e}")

    console.print("   Waiting for setPeer transactions...")
    for shard_id, tx_hash in set_peer_hashes.items():
        result = wait_for_tx(provider, tx_hash, f"setPeer shard {shard_id}")
        status = result.get("status", "unknown")
        console.print(f"   shard {shard_id} setPeer: {status}")

    # ── Save state ─────────────────────────────────────────────────────────────
    console.print("\n[bold]Step 5/5:[/bold] Saving state...")

    state = {
        "network":    args.network,
        "api":        getattr(args, "api", None),
        "chain_id":   chain_id,
        "timestamp":  now_iso(),
        "shards": {}
    }

    for shard_id in sorted(shard_wallets.keys()):
        info = shard_wallets[shard_id]
        next_shard = (shard_id + 1) % 3
        state["shards"][str(shard_id)] = {
            "shard_id":        shard_id,
            "wallet_address":  info["address"],
            "pem_path":        info["pem_path"],
            "contract_address": info.get("contract_address", "UNKNOWN"),
            "deploy_tx_hash":   info.get("deploy_tx_hash", ""),
            "fund_tx_hash":     info.get("fund_tx_hash", ""),
            "set_peer_tx_hash": set_peer_hashes.get(shard_id, ""),
            "peer_shard":       next_shard,
        }

    save_state(state)
    console.print(f"[green]✓[/green] State saved to [bold]{STATE_FILE}[/bold]")

    # ── Summary ────────────────────────────────────────────────────────────────
    table = Table(title="Deployment Summary", box=box.ROUNDED, header_style="bold white")
    table.add_column("Shard",    style="bold cyan",  no_wrap=True)
    table.add_column("Wallet",   style="white",      min_width=20)
    table.add_column("Contract", style="green",      min_width=20)
    table.add_column("Peer→",    style="dim",        no_wrap=True)

    for shard_id in sorted(shard_wallets.keys()):
        info = state["shards"][str(shard_id)]
        table.add_row(
            str(shard_id),
            info["wallet_address"][:20] + "...",
            info["contract_address"][:20] + "..." if info["contract_address"] != "UNKNOWN" else "[red]UNKNOWN[/red]",
            f"shard {info['peer_shard']}",
        )

    console.print(table)
    console.print(Panel(
        "[green]Init complete.[/green]\n"
        "Run experiments with: [bold]python cli.py run --network <url>[/bold]",
        box=box.ROUNDED,
    ))


# ═════════════════════════════════════════════════════════════════════════════
# EXPERIMENT ENGINE
# ═════════════════════════════════════════════════════════════════════════════

class HopRecord:
    """Represents a single observed hop in the ping-pong chain."""
    __slots__ = (
        "timestamp_ms", "correlation_id", "hop_index",
        "contract_address", "shard_id", "tx_hash",
        "amount", "elapsed_since_start_ms",
        "elapsed_since_previous_hop_ms", "status",
    )

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def to_dict(self) -> dict:
        return {s: getattr(self, s, None) for s in self.__slots__}


class ExperimentResult:
    def __init__(
        self,
        profile: str,
        correlation_id: str,
        intended_hops: int,
        start_tx_hash: str,
        amount_attoegld: int,
        start_ts_ms: int,
    ):
        self.profile           = profile
        self.correlation_id    = correlation_id
        self.intended_hops     = intended_hops
        self.start_tx_hash     = start_tx_hash
        self.amount_attoegld   = amount_attoegld
        self.start_ts_ms       = start_ts_ms
        self.end_ts_ms: int    = 0
        self.hops: list[HopRecord] = []
        self.failure_reason: str = ""

    @property
    def observed_hops(self) -> int:
        return len(self.hops)

    @property
    def completion_ratio(self) -> float:
        if self.intended_hops == 0:
            return 0.0
        return min(1.0, self.observed_hops / self.intended_hops)

    @property
    def latencies_ms(self) -> list[int]:
        return [
            h.elapsed_since_previous_hop_ms
            for h in self.hops
            if h.elapsed_since_previous_hop_ms is not None and h.elapsed_since_previous_hop_ms > 0
        ]

    @property
    def avg_latency_ms(self) -> float:
        lats = self.latencies_ms
        return sum(lats) / len(lats) if lats else 0.0

    @property
    def p95_latency_ms(self) -> float:
        lats = sorted(self.latencies_ms)
        if not lats:
            return 0.0
        idx = int(0.95 * len(lats))
        return float(lats[min(idx, len(lats) - 1)])

    def total_duration_ms(self) -> int:
        if self.end_ts_ms and self.start_ts_ms:
            return self.end_ts_ms - self.start_ts_ms
        return 0

    def to_summary_dict(self) -> dict:
        return {
            "profile":           self.profile,
            "correlation_id":    self.correlation_id,
            "intended_hops":     self.intended_hops,
            "observed_hops":     self.observed_hops,
            "completion_ratio":  round(self.completion_ratio, 4),
            "start_tx_hash":     self.start_tx_hash,
            "amount_attoegld":   self.amount_attoegld,
            "start_ts_ms":       self.start_ts_ms,
            "end_ts_ms":         self.end_ts_ms,
            "total_duration_ms": self.total_duration_ms(),
            "avg_latency_ms":    round(self.avg_latency_ms, 2),
            "p95_latency_ms":    round(self.p95_latency_ms, 2),
            "failure_reason":    self.failure_reason,
            "hops":              [h.to_dict() for h in self.hops],
        }


# ── Event fetching ─────────────────────────────────────────────────────────────

def fetch_events_via_api(
    api_url: str,
    correlation_id: str,
    contract_addresses: list[str],
) -> list[dict]:
    """
    Query the MultiversX API for pingPongHop events matching the correlation_id.
    The API provides richer event data than the gateway.

    Returns a list of raw event dicts.
    """
    all_events = []
    cid_hex = correlation_id.encode().hex()

    for contract_addr in contract_addresses:
        url = (
            f"{api_url.rstrip('/')}/accounts/{contract_addr}/logs"
            f"?identifier=pingPongHop&size=100"
        )
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            logs = data if isinstance(data, list) else data.get("data", [])
            for log_entry in logs:
                events_in_log = log_entry.get("events", [])
                for ev in events_in_log:
                    topics = ev.get("topics", [])
                    if topics:
                        import base64
                        try:
                            first_topic_bytes = base64.b64decode(topics[0] + "==")
                            first_topic_str = first_topic_bytes.decode("utf-8", errors="replace")
                            if first_topic_str == correlation_id:
                                all_events.append({
                                    "event": ev,
                                    "contract": contract_addr,
                                    "tx_hash": log_entry.get("txHash", ""),
                                    "timestamp": log_entry.get("timestamp", 0),
                                })
                        except Exception:
                            pass
        except Exception:
            pass

    return all_events


def fetch_tx_events_via_gateway(
    provider: ProxyNetworkProvider,
    tx_hash: str,
) -> list[dict]:
    """
    Fetch events from a specific transaction via the gateway.
    Less rich than the API but always available.
    """
    events = []
    try:
        url = f"{provider.url}/transaction/{tx_hash}?withResults=true"
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if data.get("code") != "successful":
            return events

        tx_data = data.get("data", {}).get("transaction", {})
        logs = tx_data.get("logs", {})
        raw_events = logs.get("events", [])

        for ev in raw_events:
            if ev.get("identifier") == "pingPongHop":
                events.append({"event": ev, "tx_hash": tx_hash})

        # Also check SCRs for their events
        scrs = tx_data.get("smartContractResults", [])
        for scr in scrs:
            scr_logs = scr.get("logs", {})
            scr_events = scr_logs.get("events", []) if scr_logs else []
            for ev in scr_events:
                if ev.get("identifier") == "pingPongHop":
                    events.append({"event": ev, "tx_hash": scr.get("hash", tx_hash)})
    except Exception:
        pass

    return events


def parse_hop_event(
    raw: dict,
    start_ts_ms: int,
    prev_ts_ms: int,
    correlation_id: str,
) -> Optional[HopRecord]:
    """
    Parse a raw pingPongHop event into a HopRecord.

    Event structure (MultiversX ABI encoded):
    topics[0] = correlation_id (ManagedBuffer)
    topics[1] = hop_index (u64)
    topics[2] = status (ManagedBuffer)

    data = ABI-encoded: max_hops, sender, contract_address, next_peer, amount
    """
    import base64

    ev = raw.get("event", raw)
    topics = ev.get("topics", [])

    try:
        if len(topics) < 3:
            return None

        # Decode topic[0]: correlation_id
        t0 = base64.b64decode(topics[0] + "==")
        cid = t0.decode("utf-8", errors="replace")
        if cid != correlation_id:
            return None

        # Decode topic[1]: hop_index (u64 big-endian)
        t1 = base64.b64decode(topics[1] + "==")
        hop_index = int.from_bytes(t1, "big") if t1 else 0

        # Decode topic[2]: status string
        t2 = base64.b64decode(topics[2] + "==")
        status = t2.decode("utf-8", errors="replace")

        ts_ms = now_ms()
        elapsed_start  = ts_ms - start_ts_ms
        elapsed_prev   = ts_ms - prev_ts_ms if prev_ts_ms > 0 else 0

        return HopRecord(
            timestamp_ms=ts_ms,
            correlation_id=correlation_id,
            hop_index=hop_index,
            contract_address=raw.get("contract", ev.get("address", "")),
            shard_id=None,  # filled in by caller if known
            tx_hash=raw.get("tx_hash", ""),
            amount=None,
            elapsed_since_start_ms=elapsed_start,
            elapsed_since_previous_hop_ms=elapsed_prev,
            status=status,
        )
    except Exception:
        return None


def run_experiment(
    profile_name: str,
    max_hops: int,
    amount_attoegld: int,
    gas_limit: int,
    state: dict,
    provider: ProxyNetworkProvider,
    api_url: Optional[str],
    log_path: Path,
    jsonl_path: Path,
) -> ExperimentResult:
    """
    Run a single experiment iteration:
    1. Generate correlation_id
    2. Submit startPingPong to shard0 contract
    3. Poll for events until max_hops reached or timeout
    4. Return ExperimentResult
    """
    chain_id = state["chain_id"]
    shard0_info = state["shards"]["0"]

    correlation_id  = make_correlation_id()
    start_ts_ms     = now_ms()

    # Load the shard0 wallet
    pem_path = Path(shard0_info["pem_path"])
    account  = Account.new_from_pem(pem_path)

    contract_addr = shard0_info["contract_address"]

    # Build call data
    call_data = abi_encode_start_ping_pong(correlation_id, max_hops)

    # Fetch nonce
    try:
        acc = provider.get_account(account.address)
        nonce = acc.nonce
    except Exception as e:
        log_human(log_path, f"ERROR: cannot fetch nonce for shard0: {e}")
        result = ExperimentResult(
            profile=profile_name, correlation_id=correlation_id,
            intended_hops=max_hops, start_tx_hash="", amount_attoegld=amount_attoegld,
            start_ts_ms=start_ts_ms,
        )
        result.failure_reason = f"nonce fetch failed: {e}"
        return result

    tx_body = build_and_sign_tx(
        account=account,
        receiver=contract_addr,
        value=amount_attoegld,
        gas_limit=gas_limit,
        data=call_data,
        chain_id=chain_id,
        nonce=nonce,
    )

    try:
        tx_hash = send_transaction_raw(provider, tx_body)
    except Exception as e:
        log_human(log_path, f"ERROR: startPingPong tx failed: {e}")
        result = ExperimentResult(
            profile=profile_name, correlation_id=correlation_id,
            intended_hops=max_hops, start_tx_hash="", amount_attoegld=amount_attoegld,
            start_ts_ms=start_ts_ms,
        )
        result.failure_reason = f"tx submission failed: {e}"
        return result

    log_human(log_path, f"[{profile_name}] correlation_id={correlation_id} max_hops={max_hops} tx_hash={tx_hash}")
    log_jsonl(jsonl_path, {
        "event": "experiment_start",
        "profile": profile_name,
        "correlation_id": correlation_id,
        "max_hops": max_hops,
        "amount_attoegld": amount_attoegld,
        "gas_limit": gas_limit,
        "tx_hash": tx_hash,
        "timestamp_ms": start_ts_ms,
    })

    result = ExperimentResult(
        profile=profile_name,
        correlation_id=correlation_id,
        intended_hops=max_hops,
        start_tx_hash=tx_hash,
        amount_attoegld=amount_attoegld,
        start_ts_ms=start_ts_ms,
    )

    # All contract addresses for event polling
    all_contracts = [
        state["shards"][str(i)]["contract_address"]
        for i in range(3)
        if state["shards"][str(i)]["contract_address"] != "UNKNOWN"
    ]

    # ── Poll for events ────────────────────────────────────────────────────────
    seen_hops: set[int] = set()
    prev_ts_ms   = start_ts_ms
    deadline     = time.time() + EVENT_POLL_TIMEOUT
    last_hop_seen_at = time.time()
    stall_timeout = 60  # if no new hop seen in 60s, assume failure

    while time.time() < deadline:
        # Method 1: API (preferred — richer data)
        if api_url:
            raw_events = fetch_events_via_api(api_url, correlation_id, all_contracts)
            for raw_ev in raw_events:
                hop = parse_hop_event(raw_ev, start_ts_ms, prev_ts_ms, correlation_id)
                if hop and hop.hop_index not in seen_hops:
                    seen_hops.add(hop.hop_index)
                    result.hops.append(hop)
                    prev_ts_ms = hop.timestamp_ms
                    last_hop_seen_at = time.time()

                    log_human(log_path,
                        f"  hop={hop.hop_index} status={hop.status} "
                        f"elapsed={hop.elapsed_since_start_ms}ms contract={hop.contract_address[:20]}"
                    )
                    log_jsonl(jsonl_path, {"event": "hop", **hop.to_dict()})

        else:
            # Method 2: Gateway — poll the start tx and its SCRs
            raw_events = fetch_tx_events_via_gateway(provider, tx_hash)
            for raw_ev in raw_events:
                hop = parse_hop_event(raw_ev, start_ts_ms, prev_ts_ms, correlation_id)
                if hop and hop.hop_index not in seen_hops:
                    seen_hops.add(hop.hop_index)
                    result.hops.append(hop)
                    prev_ts_ms = hop.timestamp_ms
                    last_hop_seen_at = time.time()

                    log_human(log_path,
                        f"  hop={hop.hop_index} status={hop.status} "
                        f"elapsed={hop.elapsed_since_start_ms}ms"
                    )
                    log_jsonl(jsonl_path, {"event": "hop", **hop.to_dict()})

        # Check stop conditions
        stopped = any(h.status == "stopped" for h in result.hops)
        if stopped:
            log_human(log_path, f"  → chain completed (stopped event received)")
            break

        if len(seen_hops) >= max_hops:
            log_human(log_path, f"  → all {max_hops} hops observed")
            break

        # Stall detection
        if time.time() - last_hop_seen_at > stall_timeout and len(seen_hops) > 0:
            result.failure_reason = f"stalled after {len(seen_hops)} hops (no new hop in {stall_timeout}s)"
            log_human(log_path, f"  → {result.failure_reason}")
            break

        time.sleep(EVENT_POLL_INTERVAL)

    result.end_ts_ms = now_ms()

    # Determine failure reason if not complete
    if not result.failure_reason:
        if result.observed_hops < max_hops and not any(h.status == "stopped" for h in result.hops):
            result.failure_reason = f"incomplete: observed {result.observed_hops}/{max_hops} hops, no 'stopped' event"
        elif api_url is None:
            result.failure_reason = (
                "gateway-only mode: events not fully observable. "
                "Hop count may be underreported. Use --api for full visibility."
            ) if result.observed_hops < max_hops else ""

    log_human(log_path,
        f"[{profile_name}] DONE "
        f"observed={result.observed_hops}/{max_hops} "
        f"avg_lat={result.avg_latency_ms:.0f}ms "
        f"total={result.total_duration_ms()}ms"
    )
    log_jsonl(jsonl_path, {
        "event": "experiment_end",
        **result.to_summary_dict(),
    })

    return result


# ═════════════════════════════════════════════════════════════════════════════
# EXPERIMENT PROFILES
# ═════════════════════════════════════════════════════════════════════════════

PROFILES = {
    "baseline": {
        "description": "Basic correctness check — 5 hops, small amount",
        "runs": [
            {"max_hops": 5, "amount": ONE_EGLD // 10_000, "gas_limit": GAS_START_PING_PONG},
        ],
    },
    "hop_scaling": {
        "description": "Measure breaking point across hop counts",
        "runs": [
            {"max_hops": 5,   "amount": ONE_EGLD // 10_000, "gas_limit": GAS_START_PING_PONG},
            {"max_hops": 10,  "amount": ONE_EGLD // 10_000, "gas_limit": GAS_START_PING_PONG},
            {"max_hops": 20,  "amount": ONE_EGLD // 10_000, "gas_limit": GAS_START_PING_PONG},
            {"max_hops": 50,  "amount": ONE_EGLD // 10_000, "gas_limit": GAS_START_PING_PONG * 2},
            {"max_hops": 100, "amount": ONE_EGLD // 10_000, "gas_limit": GAS_START_PING_PONG * 4},
        ],
    },
    "gas_sensitivity": {
        "description": "Fixed 5 hops, varying gas_limit to find minimum viable gas",
        "runs": [
            {"max_hops": 5, "amount": ONE_EGLD // 10_000, "gas_limit": 10_000_000},
            {"max_hops": 5, "amount": ONE_EGLD // 10_000, "gas_limit": 20_000_000},
            {"max_hops": 5, "amount": ONE_EGLD // 10_000, "gas_limit": 50_000_000},
            {"max_hops": 5, "amount": ONE_EGLD // 10_000, "gas_limit": 100_000_000},
            {"max_hops": 5, "amount": ONE_EGLD // 10_000, "gas_limit": 250_000_000},
            {"max_hops": 5, "amount": ONE_EGLD // 10_000, "gas_limit": 500_000_000},
        ],
    },
}


# ═════════════════════════════════════════════════════════════════════════════
# RUN COMMAND
# ═════════════════════════════════════════════════════════════════════════════

def cmd_run(args: argparse.Namespace) -> None:
    console.rule("[bold cyan]RUN — Cross-Shard Ping-Pong Experiment[/bold cyan]")

    state = load_state()
    provider = ProxyNetworkProvider(args.network)

    try:
        net_config = provider.get_network_config()
        chain_id = net_config.chain_id
        console.print(f"[green]✓[/green] Connected: {args.network}  chain={chain_id}")
    except Exception as e:
        console.print(f"[red]Error:[/red] Cannot connect: {e}")
        sys.exit(1)

    api_url: Optional[str] = getattr(args, "api", None)
    if api_url:
        console.print(f"[green]✓[/green] API endpoint: {api_url} (full event observability)")
    else:
        console.print(
            "[yellow]⚠[/yellow] No --api provided. "
            "Running in gateway-only mode.\n"
            "  Limitation: gateway does not expose a cross-shard event search endpoint.\n"
            "  Events will be fetched from the originating tx only.\n"
            "  Hop count may be underreported for hops > 1.\n"
            "  For full visibility, provide --api https://api.battleofnodes.com"
        )

    # Determine which profiles to run
    profile_arg: Optional[str] = getattr(args, "profile", None)
    if profile_arg:
        if profile_arg not in PROFILES:
            console.print(f"[red]Error:[/red] Unknown profile '{profile_arg}'. Options: {list(PROFILES.keys())}")
            sys.exit(1)
        profiles_to_run = {profile_arg: PROFILES[profile_arg]}
    else:
        profiles_to_run = PROFILES

    # Setup log files
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path   = LOGS_DIR / f"run-{ts_tag}.log"
    jsonl_path = LOGS_DIR / f"run-{ts_tag}.jsonl"
    summary_json_path = LOGS_DIR / f"summary-{ts_tag}.json"
    summary_md_path   = LOGS_DIR / f"summary-{ts_tag}.md"

    log_human(log_path, f"=== Run started at {now_iso()} ===")
    log_human(log_path, f"Network: {args.network}")
    log_human(log_path, f"API: {api_url or 'none'}")
    log_human(log_path, f"Profiles: {list(profiles_to_run.keys())}")

    # Display topology
    table = Table(title="Ring Topology", box=box.ROUNDED, header_style="bold white")
    table.add_column("Shard", style="bold cyan", no_wrap=True)
    table.add_column("Wallet", style="white")
    table.add_column("Contract", style="green")
    table.add_column("→ Peer Shard", style="dim")

    for shard_id in sorted(state["shards"].keys(), key=int):
        info = state["shards"][shard_id]
        table.add_row(
            shard_id,
            info["wallet_address"][:24] + "...",
            info["contract_address"][:24] + "..." if info["contract_address"] != "UNKNOWN" else "[red]UNKNOWN[/red]",
            f"shard {info['peer_shard']}",
        )
    console.print(table)

    # ── Run all profiles ───────────────────────────────────────────────────────
    all_results: list[ExperimentResult] = []

    for profile_name, profile_config in profiles_to_run.items():
        console.rule(f"[bold yellow]Profile: {profile_name}[/bold yellow]")
        console.print(f"  {profile_config['description']}")

        runs = profile_config["runs"]

        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}"),
            BarColumn(bar_width=40),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"{profile_name}", total=len(runs))

            for i, run_cfg in enumerate(runs):
                max_hops = run_cfg["max_hops"]
                amount   = run_cfg["amount"]
                gas_limit = run_cfg["gas_limit"]

                progress.update(
                    task,
                    description=f"{profile_name} run {i+1}/{len(runs)}  hops={max_hops} gas={gas_limit:,}"
                )

                console.print(
                    f"\n  → Run {i+1}: max_hops={max_hops}  "
                    f"amount={amount / ONE_EGLD:.6f} EGLD  "
                    f"gas={gas_limit:,}"
                )

                result = run_experiment(
                    profile_name=f"{profile_name}_{i+1}",
                    max_hops=max_hops,
                    amount_attoegld=amount,
                    gas_limit=gas_limit,
                    state=state,
                    provider=provider,
                    api_url=api_url,
                    log_path=log_path,
                    jsonl_path=jsonl_path,
                )
                all_results.append(result)

                # Live metrics
                status_color = "green" if result.completion_ratio >= 0.8 else "yellow" if result.completion_ratio >= 0.5 else "red"
                console.print(
                    f"  [{status_color}]✓[/{status_color}] "
                    f"observed={result.observed_hops}/{max_hops} "
                    f"({result.completion_ratio:.0%})  "
                    f"avg_lat={result.avg_latency_ms:.0f}ms  "
                    f"p95_lat={result.p95_latency_ms:.0f}ms  "
                    f"total={result.total_duration_ms()}ms"
                )
                if result.failure_reason:
                    console.print(f"  [dim]failure_reason: {result.failure_reason}[/dim]")

                progress.advance(task)

                # Brief pause between runs to avoid nonce conflicts
                if i < len(runs) - 1:
                    time.sleep(5)

    # ── Write summaries ────────────────────────────────────────────────────────
    summary = {
        "timestamp": now_iso(),
        "network":   args.network,
        "api":       api_url,
        "profiles":  list(profiles_to_run.keys()),
        "results":   [r.to_summary_dict() for r in all_results],
    }

    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    _write_summary_md(summary_md_path, all_results, args.network, api_url)

    console.print(f"\n[green]✓[/green] Logs: {log_path}")
    console.print(f"[green]✓[/green] JSONL: {jsonl_path}")
    console.print(f"[green]✓[/green] Summary JSON: {summary_json_path}")
    console.print(f"[green]✓[/green] Summary MD: {summary_md_path}")

    # ── Final results table ────────────────────────────────────────────────────
    _print_results_table(all_results)


def _write_summary_md(
    path: Path,
    results: list[ExperimentResult],
    network: str,
    api_url: Optional[str],
) -> None:
    lines = [
        "# Ping-Pong Cross-Shard Experiment — Run Summary",
        "",
        f"**Date**: {now_iso()}",
        f"**Network**: {network}",
        f"**API**: {api_url or 'gateway-only'}",
        "",
        "## Results",
        "",
        "| Profile | Intended Hops | Observed Hops | Completion | Avg Lat (ms) | P95 Lat (ms) | Total (ms) | Failure |",
        "|---------|---------------|---------------|------------|-------------|-------------|-----------|---------|",
    ]
    for r in results:
        lines.append(
            f"| {r.profile} | {r.intended_hops} | {r.observed_hops} "
            f"| {r.completion_ratio:.0%} "
            f"| {r.avg_latency_ms:.0f} "
            f"| {r.p95_latency_ms:.0f} "
            f"| {r.total_duration_ms()} "
            f"| {r.failure_reason or '—'} |"
        )

    lines += [
        "",
        "## Hop Detail",
        "",
    ]

    for r in results:
        lines.append(f"### {r.profile} (correlation_id: `{r.correlation_id}`)")
        lines.append("")
        lines.append("| Hop | Status | Elapsed Start (ms) | Elapsed Prev (ms) | Contract |")
        lines.append("|-----|--------|--------------------|-------------------|---------|")
        for h in r.hops:
            lines.append(
                f"| {h.hop_index} | {h.status} "
                f"| {h.elapsed_since_start_ms} "
                f"| {h.elapsed_since_previous_hop_ms} "
                f"| `{str(h.contract_address)[:20]}...` |"
            )
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _print_results_table(results: list[ExperimentResult]) -> None:
    table = Table(title="Experiment Results", box=box.ROUNDED, header_style="bold white", show_lines=True)
    table.add_column("Profile",    style="bold cyan", no_wrap=True)
    table.add_column("Hops",       style="white",     no_wrap=True)
    table.add_column("Completion", style="bold",      no_wrap=True)
    table.add_column("Avg Lat",    style="white",     no_wrap=True)
    table.add_column("P95 Lat",    style="white",     no_wrap=True)
    table.add_column("Total",      style="dim",       no_wrap=True)
    table.add_column("Failure",    style="dim red",   min_width=20)

    for r in results:
        ratio = r.completion_ratio
        color = "green" if ratio >= 0.8 else "yellow" if ratio >= 0.5 else "red"
        table.add_row(
            r.profile,
            f"{r.observed_hops}/{r.intended_hops}",
            f"[{color}]{ratio:.0%}[/{color}]",
            f"{r.avg_latency_ms:.0f}ms",
            f"{r.p95_latency_ms:.0f}ms",
            f"{r.total_duration_ms()}ms",
            r.failure_reason[:60] if r.failure_reason else "—",
        )

    console.print(table)


# ═════════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Cross-Shard EGLD Ping-Pong Experiment Runner — MultiversX BoN",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--network", required=True,
        help="MultiversX gateway URL (e.g. https://gateway.battleofnodes.com)",
    )
    common.add_argument(
        "--api", default=None,
        help="MultiversX API URL (optional, enables full event observability)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    init_parser = subparsers.add_parser(
        "init", parents=[common],
        help="Generate wallets, fund them, deploy contracts, wire ring topology",
    )
    init_parser.add_argument(
        "--master-pem", required=True,
        help="Path to the master wallet PEM file (must have >= 3 EGLD)",
    )
    init_parser.add_argument(
        "--reset", action="store_true",
        help="Overwrite existing state/config.json",
    )

    # run
    run_parser = subparsers.add_parser(
        "run", parents=[common],
        help="Run ping-pong experiments using deployed contracts",
    )
    run_parser.add_argument(
        "--profile",
        choices=list(PROFILES.keys()),
        default=None,
        help=f"Run only this profile (default: all). Options: {list(PROFILES.keys())}",
    )

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
