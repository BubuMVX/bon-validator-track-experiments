# Challenge 8 — Transaction Field Fuzzing on MultiversX BoN Testnet

> Submission for the BoN Supernova validator challenge.

---

## Methodology — SDK bypass

All test suites share a common approach: **transactions are crafted as raw JSON payloads and submitted directly to the gateway's REST endpoint (`POST /transaction/send`), bypassing the `multiversx-sdk` Python library entirely.**

The SDK performs field validation before even constructing a `Transaction` object (e.g. it raises a Python exception if `nonce < 0` or `chainID` is empty). This means that without the bypass, most edge cases would never reach the network and the test would exercise the SDK, not the node.

The raw pipeline used is:

1. Build an `OrderedDict` matching the SDK's signing format (as observed in `TransactionComputer._to_dictionary`)
2. Serialize to compact JSON (`separators=(",", ":")`) — no validation
3. Sign the raw bytes with the wallet's Ed25519 key via `account.sign(bytes)`
4. Assemble the full HTTP body (matching `Transaction.to_dictionary`)
5. `POST` directly to `{gateway}/transaction/send` via `requests`

This ensures the **gateway and node are the first and only validation layer** for every test case.

Source: [`fuzzer.py`](fuzzer.py)

---

## Test

### Title
Transaction field fuzzing — integer boundaries, payload edge cases, and signature bypass attempts

### Hypothesis
The MultiversX gateway and node must robustly validate every field of an incoming transaction before accepting it into the mempool. Three categories of fields are tested:

- **Numeric fields** (`nonce`, `value`, `gasLimit`, `gasPrice`) map to `uint64` or `*big.Int` in the Go node. Values overflowing their type could cause silent wrap-around, fee computation overflow, or a node panic.
- **Protocol fields** (`data`, `chainID`, `version`, `options`) may have loose or missing validation. Oversized payloads could exhaust mempool memory; unexpected `chainID` values could enable cross-network replay; invalid `version`/`options` combinations could bypass guardian logic.
- **Cryptographic signature** must be verified mathematically, not just by length. An incomplete check would allow anyone to submit transactions on behalf of any address.

### Setup
- Single wallet on the BoN testnet (`wallet.pem`)
- Network: `https://gateway.battleofnodes.com`
- Raw HTTP POST to `/transaction/send` (SDK bypassed)
- Self-transfer (sender = receiver), native EGLD, `data = b"fuzz"`
- For signature test suite: the transaction is signed correctly, then the `signature` field is **overwritten** before sending

### Command example

```bash
python fuzzer.py --wallet wallet.pem --network https://gateway.battleofnodes.com
```

---

## Test suite 1 — Integer overflow on numeric fields

### Nonce (11 cases)

| Label | Nonce value | Hypothesis |
|---|---|---|
| valid (current_nonce) | on-chain nonce | Reference — must be accepted |
| nonce + 1 (gap=1) | current + 1 | Pending in mempool — accepted |
| nonce + 10000 (large gap) | current + 10 000 | Should be queued or rejected as too far ahead |
| nonce - 1 (replay) | current - 1 | Must be rejected — already used |
| u64_max (2^64 - 1) | 18 446 744 073 709 551 615 | Max valid `uint64` — should be rejected as impossible to reach |
| u64_wrap (2^64) | 18 446 744 073 709 551 616 | Overflows `uint64` — JSON unmarshal should fail |
| u64_wrap + 1 | 2^64 + 1 | Same |
| nonce + 2^64 | current + 2^64 | Same value mod 2^64 as current nonce — would pass if the node truncates |
| nonce + 2*2^64 | current + 2*2^64 | Double wrap |
| negative nonce (-1) | -1 | Negative `uint64` — must be rejected |
| negative nonce (-2^63) | -9 223 372 036 854 775 808 | Minimum `int64` value serialized as negative integer |

### Value (9 cases)

| Label | Value | Hypothesis |
|---|---|---|
| value = 0 | 0 | Valid zero-value transfer |
| value = 1 | 1 | Minimum positive transfer |
| value = u64_max | 2^64 - 1 | Max `uint64` — exceeds any realistic balance |
| value = 2^64 | 2^64 | Overflows `uint64`; node uses `*big.Int` for value — may be accepted as a string but should fail balance check |
| value = u128_max | 2^128 - 1 | Far exceeds total EGLD supply |
| value = u256_max | 2^256 - 1 | Extremely large bigint as decimal string |
| value = -1 | "-1" | Negative value string — must be rejected |
| value = 'abc' | "abc" | Non-numeric string — must be rejected at JSON parse |
| value = 1.5 | "1.5" | Float string — must be rejected (EGLD is integer-only) |

### Gas (9 cases)

| Label | gasLimit / gasPrice | Hypothesis |
|---|---|---|
| gasLimit = 0 | 0 | Below minimum — must be rejected |
| gasLimit = 1 | 1 | Below minimum for any tx |
| gasLimit = u64_max | 2^64 - 1 | Overflow risk in fee computation |
| gasLimit = 2^64 | 2^64 | JSON unmarshal into `uint64` must fail |
| gasLimit = -1 | -1 | Negative `uint64` — must be rejected |
| gasPrice = 0 | 0 | Below minimum gas price — must be rejected |
| gasPrice = 1 | 1 | Below the 1 000 000 000 minimum |
| gasPrice = u64_max | 2^64 - 1 | Fee = gasLimit × gasPrice — overflow risk in fee computation |
| gasPrice = -1 | -1 | Negative — must be rejected |

### Expected behavior
All out-of-range values should be **rejected** with a clear error message. The critical risk is a value being silently truncated (accepted with wrong effective semantics) or causing a node-side panic.

---

## Test suite 2 — Payload size and protocol field edge cases

### Data field (5 cases)

| Label | Data | Hypothesis |
|---|---|---|
| data = null bytes (32×\x00) | 32 null bytes | Binary data — should be handled as opaque bytes |
| data = invalid UTF-8 | `bytes(range(128, 256))` | Non-UTF-8 bytes base64-encoded — gateway may attempt UTF-8 decode |
| data = 10 KB | 10 240 random bytes | Within typical limits |
| data = 100 KB | 102 400 random bytes | Near or above the node's transaction size limit |
| data = 1 MB | 1 048 576 random bytes | Should be hard-rejected — tests whether oversized payloads can reach the node or exhaust gateway buffers |

### Chain ID (9 cases)

| Label | chainID | Hypothesis |
|---|---|---|
| valid | actual chain ID | Reference |
| empty string | `""` | Must be rejected — no chain binding |
| "1" (mainnet) | `"1"` | Wrong chain — must be rejected to prevent replay |
| "T" (testnet) | `"T"` | Wrong chain |
| "D" (devnet) | `"D"` | Wrong chain |
| "ZZZ" (unknown) | `"ZZZ"` | Unknown chain — must be rejected |
| 1000 chars | `"X" × 1000` | Oversized string — buffer handling |
| null byte | `"\x00"` | Non-printable character in chain identifier |
| integer 1 | `1` (not a string) | Wrong JSON type — Go's JSON unmarshal expects a string |

### Version and Options (9 cases)

| Label | version | options | Hypothesis |
|---|---|---|---|
| version=0 | 0 | 0 | Below minimum version (1) — may be rejected or treated as v1 |
| version=2 | 2 | 0 | Version 2 is defined; should be valid |
| version=255 | 255 | 0 | Unknown future version — how does the node handle it? |
| version=65535 | 65535 | 0 | Large version — field is `uint32` in proto |
| version=-1 | -1 | 0 | Negative version |
| options=0xFF | 1 | 255 | Reserved bits set — node should reject unknown option bits |
| options=0xFFFF | 1 | 65535 | All option bits set |
| version=0, options=1 | 0 | 1 | Options require version ≥ 2 — should be rejected |
| version=u64_max | 2^64-1 | 0 | Overflow on version field |

### Expected behavior
- `data` beyond the protocol limit should be **hard-rejected** at the gateway, not forwarded to the node.
- `chainID` mismatches should be **rejected** to prevent cross-network replay attacks.
- Unknown `version` values should be **rejected** or handled gracefully, never cause a node crash.
- Reserved `options` bits should be **rejected** to prevent bypassing future guarded-transaction logic.

---

## Test suite 3 — Cryptographic signature manipulation (7 cases)

| Label | Signature value | Hypothesis |
|---|---|---|
| sig = 64 zero bytes | `00` × 64 | All-zero signature — mathematically invalid but correct length |
| sig = 32 bytes (short) | `00` × 32 | Half-length signature — fails length check |
| sig = 128 bytes (long) | `ff` × 128 | Double-length — fails length check |
| sig = 1 byte | `01` | Single byte — clearly invalid |
| sig = random 64 bytes | 64 random bytes | Correct length but wrong value — must fail Ed25519 verify |
| sig = empty string | `""` | Empty signature field — must be rejected |
| no sig (skip_sig=True) | field omitted | Missing signature entirely — must be rejected |

### Expected behavior
Every case except a valid signature should be **rejected**. The critical failure mode would be the node accepting a zero-length or all-zero signature — which would constitute a complete authentication bypass allowing anyone to send transactions from any address.

---

## Summary

| Test suite | Categories | Cases | Key risk tested |
|---|---|---|---|
| 1 — Integer overflow | nonce, value, gas | 29 | Silent type truncation / fee overflow / panic |
| 2 — Payload & protocol fields | data, chainID, version, options | 23 | Mempool exhaustion / cross-chain replay / option bypass |
| 3 — Signature manipulation | signature | 7 | Authentication bypass |
| **Total** | **7** | **59** | |

All results (ACCEPTED / REJECTED / error message) are logged and tabulated by `fuzzer.py`.
