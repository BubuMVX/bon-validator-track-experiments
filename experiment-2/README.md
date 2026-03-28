# Cross-Shard EGLD Ping-Pong Experiment
### MultiversX Battle of Nodes — Challenge 8, Experiment 2

---

## Abstract

This experiment deploys a ring of three smart contracts, one on each shard of the MultiversX testnet, and measures the behavior of cross-shard EGLD forwarding chains. A single transaction triggers a chain of `N` cross-shard hops that circulates EGLD through the ring until a hop limit is reached. Every hop emits an on-chain event, providing a complete observable trace of the execution.

---

## Motivation

MultiversX's sharded architecture processes transactions in parallel across shards. When a smart contract on shard 0 calls a contract on shard 1, the call is not synchronous — it is delivered as a Smart Contract Result (SCR) that is processed in the destination shard's next block. This creates measurable latency and introduces complexity around gas propagation, event visibility, and failure modes.

This experiment quantifies that behavior in a controlled, reproducible way.

---

## Hypothesis

1. **Latency per cross-shard hop is bounded** between 2–10 seconds depending on round time (~6s) and cross-shard SCR delivery overhead.
2. **Gas exhaustion is the primary failure mode** for long chains: each hop reduces available gas, and at some point the remaining gas is insufficient to forward to the next hop.
3. **hop_scaling** will show a non-linear relationship between `max_hops` and `total_duration` because each hop requires at least one full cross-shard round.
4. **The ring topology** (0→1→2→0→...) exercises all three inter-shard communication channels.

---

## Architecture

```
[Caller]
   │
   │  startPingPong(correlation_id, max_hops) + EGLD
   ▼
[Contract Shard 0]  ─────SCR──────►  [Contract Shard 1]  ─────SCR──────►  [Contract Shard 2]
       ▲                                                                           │
       └─────────────────────────────────SCR─────────────────────────────────────┘
                              (if max_hops not yet reached)
```

Each arrow is an asynchronous cross-shard SCR. The ring loops until `current_hop >= max_hops`.

### Hop flow

```
startPingPong(cid, N)  →  forward(cid, 1, N)  →  forward(cid, 2, N)  →  ...  →  forward(cid, N, N)  →  STOP
```

---

## Smart Contract Design

**File**: `contracts/ping_pong_forwarder.rs`

### Endpoints

| Endpoint | Payable | Access | Description |
|----------|---------|--------|-------------|
| `init(peer_address)` | No | Deploy | Set the next-hop peer contract address |
| `setPeer(peer_address)` | No | Owner only | Update peer after deployment |
| `startPingPong(correlation_id, max_hops)` | EGLD | Public | Start a new hop chain |
| `forward(correlation_id, current_hop, max_hops)` | EGLD | Public | Process one hop, forward to peer |
| `getPeer()` | — | View | Return current peer address |

### Events

Every hop emits a `pingPongHop` event with indexed topics:
- `correlation_id` — unique chain identifier (hex string)
- `hop_index` — 0-based hop number
- `status` — `"started"` | `"forwarded"` | `"stopped"`

Plus non-indexed data: `max_hops`, `sender`, `contract_address`, `next_peer`, `amount`.

### Cross-shard call pattern

The contract uses `transfer_execute` (fire-and-forget). This is intentional:

- **No callbacks**: Async callbacks require reserving callback gas and add a second cross-shard round trip per hop. This experiment avoids them to keep the hop chain simple and the gas consumption predictable.
- **Atomicity**: If the `transfer_execute` call fails (e.g. insufficient gas), the entire `forward()` execution is rolled back. The EGLD stays in the current contract.
- **Observability**: The calling contract emits its event *before* the outgoing SCR is dispatched, so events appear in the transaction of the shard processing the hop, not on the destination shard.

### Gas model

Each hop consumes approximately:
- ~500,000 gas for storage reads (`peer_address`)
- ~1,000,000 gas for event emission
- ~3,000,000 gas reserved for epilogue (`GAS_RESERVE_FOR_SELF`)
- Remaining gas forwarded to the next hop

Formula: `gas_for_next = gas_received - GAS_RESERVE_FOR_SELF`

This means gas decreases by ~3M per hop. With `GAS_START_PING_PONG = 100_000_000`, approximately 33 hops are theoretically possible before gas exhaustion.

### Safety bounds

| Constraint | Value |
|-----------|-------|
| `ABSOLUTE_MAX_HOPS` | 200 |
| `MIN_EGLD_AMOUNT` | 0.000001 EGLD |
| `GAS_RESERVE_FOR_SELF` | 3,000,000 |

---

## Python Runner Design

**File**: `cli.py`

### Architecture

```
cli.py
  ├── cmd_init()        — wallet generation, funding, deployment, ring wiring
  ├── cmd_run()         — profile execution, event polling, logging
  ├── run_experiment()  — single experiment iteration
  ├── ExperimentResult  — structured result with latency statistics
  └── HopRecord         — single hop observation
```

### Wallet generation

The `init` command generates wallets until exactly one wallet per shard is found. Shard detection uses the gateway's `/address/<bech32>` endpoint which returns the `shardID` field from the account data. This is the SDK-authoritative method — no manual bit-twiddling.

The expected number of random wallets to generate before finding all 3 shards is approximately `3 × H(3) ≈ 5.5` (coupon collector), so ~6 attempts on average.

### Event observability

Two modes are supported:

**API mode** (`--api <url>`):
- Queries `/accounts/<contract>/logs?identifier=pingPongHop`
- Returns events across all transactions touching the contract
- Full cross-shard visibility
- Recommended for production measurements

**Gateway-only mode** (no `--api`):
- Queries `/transaction/<tx_hash>?withResults=true`
- Returns events from the originating shard only
- Cross-shard hops may not be visible
- Suitable for basic testing but latency measurements will be incomplete

### Measurement pipeline

```
startPingPong tx submitted
     │
     ├── record start_ts_ms
     │
     └── poll loop (every 5s, up to 10min):
           ├── fetch pingPongHop events matching correlation_id
           ├── for each new hop: record timestamp, compute elapsed
           ├── stop when "stopped" event seen OR all N hops observed
           └── stall detection: break if no new hop in 60s
```

---

## Project Structure

```
experiment-2/
├── cli.py                          # Python CLI runner
├── requirements.txt                # Python dependencies
├── README.md                       # This file
├── contracts/
│   ├── ping_pong_forwarder.rs      # Rust smart contract source
│   ├── ping_pong_forwarder.abi.json
│   ├── ping_pong_forwarder.wasm    # (generated by mxpy contract build)
│   └── Cargo.toml
├── wallets/                        # Generated wallet PEM files (gitignored)
│   ├── wallet_shard0.pem
│   ├── wallet_shard1.pem
│   └── wallet_shard2.pem
├── state/
│   └── config.json                 # Deployment state
└── logs/
    ├── run-<timestamp>.log         # Human-readable run log
    ├── run-<timestamp>.jsonl       # Machine-readable hop records
    ├── summary-<timestamp>.json    # Structured summary
    └── summary-<timestamp>.md      # Markdown summary
```

---

## Setup

### Prerequisites

- Python 3.10+
- Rust toolchain + `wasm32-unknown-unknown` target
- `mxpy` (MultiversX CLI)

```bash
# Install mxpy
pip install mxpy

# Install Rust WASM target
rustup target add wasm32-unknown-unknown

# Install Python dependencies
pip install -r requirements.txt
```

### Build the contract

```bash
cd contracts/
mxpy contract build
# Output: ping_pong_forwarder.wasm
```

The `mxpy contract build` command compiles the Rust source to WASM using the `multiversx-sc-meta` build tool. It expects a standard MultiversX contract project structure. If building manually:

```bash
# Install sc-meta
cargo install multiversx-sc-meta --locked

# Build
sc-meta all build --path contracts/
```

---

## Commands

### `init`

```
python cli.py init \
  --network https://gateway.battleofnodes.com \
  --master-pem /path/to/master.pem \
  [--api https://api.battleofnodes.com] \
  [--reset]
```

**Arguments:**
- `--network` (required): Gateway URL
- `--master-pem` (required): PEM file with >= 3 EGLD
- `--api` (optional): API endpoint for better observability
- `--reset`: Overwrite existing `state/config.json`

**Steps performed:**
1. Generate 3 wallets (one per shard)
2. Fund each with 1 EGLD from master
3. Deploy `ping_pong_forwarder.wasm` from each wallet
4. Wire ring topology via `setPeer` calls
5. Save state to `state/config.json`

### `run`

```
python cli.py run \
  --network https://gateway.battleofnodes.com \
  [--api https://api.battleofnodes.com] \
  [--profile baseline|hop_scaling|gas_sensitivity]
```

**Arguments:**
- `--network` (required): Gateway URL
- `--api` (optional): API endpoint
- `--profile` (optional): Run only one profile (default: all three)

---

## Example Usage

```bash
# Full setup + run
python cli.py init \
  --network https://gateway.battleofnodes.com \
  --master-pem ./master.pem \
  --api https://api.battleofnodes.com

python cli.py run \
  --network https://gateway.battleofnodes.com \
  --api https://api.battleofnodes.com

# Run only baseline profile
python cli.py run \
  --network https://gateway.battleofnodes.com \
  --profile baseline

# Reset and re-deploy
python cli.py init \
  --network https://gateway.battleofnodes.com \
  --master-pem ./master.pem \
  --reset
```

---

## Experiment Profiles

### `baseline`

- `max_hops = 5`, `amount = 0.0001 EGLD`
- Validates correctness: all 5 hops must complete and a `stopped` event must appear
- Establishes baseline latency numbers

### `hop_scaling`

- Tests `max_hops = [5, 10, 20, 50, 100]`
- Identifies the breaking point (gas exhaustion or protocol limit)
- Each run uses the same amount but increasing gas_limit for longer chains
- Expected breaking point: around 30–40 hops with default gas limits

### `gas_sensitivity`

- Fixed `max_hops = 5`, varying `gas_limit = [10M, 20M, 50M, 100M, 250M, 500M]`
- Identifies minimum viable gas for a 5-hop chain
- Shows how gas propagates (linear decay per hop)

---

## Metrics Explanation

| Metric | Description |
|--------|-------------|
| `intended_hops` | `max_hops` passed to `startPingPong` |
| `observed_hops` | Number of `pingPongHop` events successfully observed |
| `completion_ratio` | `observed_hops / intended_hops` (capped at 1.0) |
| `avg_latency_ms` | Mean time between consecutive hop events |
| `p95_latency_ms` | 95th percentile inter-hop latency |
| `total_duration_ms` | Wall time from tx submission to last observed event |
| `failure_reason` | If incomplete: gas exhaustion, stall, or observability gap |

### Latency interpretation

`elapsed_since_previous_hop_ms` measures time between when the Python runner **observed** consecutive events. This is an approximation of actual cross-shard execution latency because:

1. Events are polled at 5-second intervals, adding up to 5s of measurement jitter
2. The timestamp is set client-side when the event is first seen, not at block finalization
3. With the API, block timestamps could be used for more accurate measurements (future improvement)

---

## Failure Modes

| Failure | Cause | Detection |
|---------|-------|-----------|
| Gas exhaustion | Remaining gas < GAS_RESERVE_FOR_SELF | Chain stops mid-execution, no `stopped` event |
| Insufficient payment | `amount < MIN_EGLD_AMOUNT` | `startPingPong` reverts immediately |
| Peer not configured | `setPeer` not called | `startPingPong` fails with "peer not configured" |
| Stall | Network congestion / node issue | 60s without a new hop event |
| Contract not deployed | WASM not built | Deploy tx fails |
| Observability gap | Gateway-only mode | Hops 2+ may not be visible |

---

## Limitations

1. **Event polling granularity**: The 5-second poll interval means latency measurements have ±5s jitter. The actual per-hop execution time is determined by block time (~6s) plus cross-shard SCR delivery overhead (~1 block = 6s), so theoretical minimum per-hop latency is ~6–12 seconds.

2. **Gateway-only observability**: Without the API endpoint, only events from the originating shard are easily retrievable. Cross-shard hops 2+ emit events on other shards, which require separate queries. Use `--api` for full visibility.

3. **Gas propagation approximation**: The `GAS_RESERVE_FOR_SELF = 3_000_000` constant is a conservative estimate. Actual gas consumption depends on storage layout, event size, and VM version. Measure with `mxpy` simulation for exact values.

4. **No callback acknowledgement**: The ring uses fire-and-forget. There is no confirmation that a hop was processed — only that the forwarding SCR was submitted. A failed hop (e.g. gas exhaustion on the receiving shard) will silently stop the chain.

5. **EGLD amount dilution**: The same EGLD is forwarded through all hops, so no value is created or destroyed (ignoring gas). The experiment is designed to be neutral with respect to value — only small amounts (0.0001 EGLD) are used.

---

## Safety Considerations

- All loops are bounded: `ABSOLUTE_MAX_HOPS = 200`
- Minimum payment enforced: `MIN_EGLD_AMOUNT = 10^12` attoEGLD
- Wallet generation is bounded: `max_attempts = 500`
- TX polling is bounded: `TX_POLL_TIMEOUT = 120s`
- Event polling is bounded: `EVENT_POLL_TIMEOUT = 600s`
- Stall detection: if no new hop in 60s, experiment is declared failed
- Master wallet balance is checked before funding
- No unbounded recursion anywhere in the contract or CLI

---

## State Schema (`state/config.json`)

```json
{
  "network": "https://gateway.battleofnodes.com",
  "api": "https://api.battleofnodes.com",
  "chain_id": "BON",
  "timestamp": "2024-01-01T00:00:00+00:00",
  "shards": {
    "0": {
      "shard_id": 0,
      "wallet_address": "erd1...",
      "pem_path": "wallets/wallet_shard0.pem",
      "contract_address": "erd1qqq...",
      "deploy_tx_hash": "abc123...",
      "fund_tx_hash": "def456...",
      "set_peer_tx_hash": "ghi789...",
      "peer_shard": 1
    },
    "1": { "...": "..." },
    "2": { "...": "..." }
  }
}
```

---

## BATTLE OF NODES SUBMISSION

### Title
Cross-Shard EGLD Ping-Pong Ring — Measuring Hop Latency, Gas Decay, and Cross-Shard Execution Limits

### Setup Description

Three smart contracts (`ping_pong_forwarder`) are deployed via the `init` command, one on each shard of the MultiversX BoN testnet. Each contract is funded with 1 EGLD from a master wallet. The contracts are wired in a ring: shard0 → shard1 → shard2 → shard0. The ring topology is configured via the `setPeer` owner endpoint immediately after deployment.

All deployment transactions, wallet addresses, and contract addresses are recorded in `state/config.json` for reproducibility.

### Actions Description

The `run` command executes three experiment profiles:

1. **baseline**: 5 hops, validates that the ring functions correctly and measures initial latency.
2. **hop_scaling**: 5/10/20/50/100 hops, finds the gas-imposed breaking point.
3. **gas_sensitivity**: 5 hops, 6 gas levels (10M–500M), determines minimum viable gas per hop.

Each profile iteration generates a unique `correlation_id`, submits `startPingPong` to the shard0 contract, then polls for `pingPongHop` events across all three contracts until the chain completes or times out.

### Evidence Description

Every run produces four output files in `logs/`:

- `run-<ts>.log` — timestamped human-readable event stream
- `run-<ts>.jsonl` — one JSON record per hop, suitable for analysis
- `summary-<ts>.json` — structured summary with latency statistics per experiment
- `summary-<ts>.md` — Markdown table of results for submission

The `pingPongHop` events emitted on-chain (visible via the MultiversX API) serve as immutable, verifiable proof that each hop executed on the correct shard with the correct correlation_id. These can be independently verified by querying:

```
GET https://api.battleofnodes.com/accounts/<contract_address>/logs?identifier=pingPongHop
```
