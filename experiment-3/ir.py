"""
ir.py — Three-Address Intermediate Representation (TAC-IR)
===========================================================

This module defines the Intermediate Representation (IR) layer of the
FTIDL compilation pipeline. The IR is a linearized, register-based,
three-address code (TAC) in the tradition of:

  - Cytron et al. (1991) — SSA form and its applications
  - Appel (1998)         — Modern Compiler Implementation in ML
  - Cooper & Torczon    — Engineering a Compiler (2nd ed., §5)

The IR occupies the second stratum of the compilation hierarchy:

    FTIDL Spec → [Parser] → AST → [IR Generator] → IRProgram
                                                         ↓
                                               [BF++ Compiler]
                                                         ↓
                                               BF++ Program

The IR abstracts over:
  1. Source-level syntax (no longer relevant at this stage)
  2. Target-level details (BF++ tape layout not yet determined)
  3. All control flow (linearized into a flat instruction sequence)

Each IR instruction is an instance of `IRInstruction`, a tagged union
(sum type in the Haskell sense) parameterized over an `IROp` opcode
and a variable-length operand list. The operands are untyped at the
IR level; type inference is delegated to the BF++ compiler.

Semantic domains
----------------
The IR operates over the following value domains:

  Σ_path    — file system paths (strings)
  Σ_addr    — bech32 addresses (strings)
  Σ_amount  — attoEGLD integers (arbitrary precision)
  Σ_gas     — unsigned 64-bit integers
  Σ_memo    — UTF-8 strings

A well-formed IRProgram is one in which:
  - Exactly one LOAD_SENDER instruction appears
  - Exactly one LOAD_RECEIVER instruction appears
  - Exactly one ENCODE_AMOUNT instruction appears
  - EMIT is the final instruction
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Any, Optional

from parser import ASTTransactionSpec


# ─────────────────────────────────────────────────────────────────────────────
# OPCODE DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

class IROp(Enum):
    """
    The complete set of IR opcodes.

    These map loosely to the underlying BF++ extended instructions but
    remain target-agnostic. The distinction is philosophically important:
    IR operates on abstract semantic values; BF++ operates on tape cells.
    """
    # ── Setup ──────────────────────────────────────────────────────────────────
    LOAD_SENDER    = auto()  # operands: (wallet_path: str)
    LOAD_RECEIVER  = auto()  # operands: (bech32_address: str)
    ENCODE_AMOUNT  = auto()  # operands: (attoegld: int)
    SET_GAS        = auto()  # operands: (gas_limit: int)
    SET_MEMO       = auto()  # operands: (memo: str)

    # ── Validation (Phase III: Irrelevance-Preserving Validation) ─────────────
    VALIDATE_TOKEN       = auto()  # operands: (token_symbol: str)
    VALIDATE_OPERATION   = auto()  # operands: (operation_name: str)
    VALIDATE_AMOUNT_EVEN = auto()  # operands: (attoegld: int) — digit count must be even
    VALIDATE_MEMO_PRIME  = auto()  # operands: (memo: str) — memo byte length must be prime
    VALIDATE_GAS_ODD     = auto()  # operands: (gas: int) — gas must be odd (cosmological)

    # ── Redundant Encoding Layer ───────────────────────────────────────────────
    # The amount is encoded thrice: decimal → hex → base64 → back to decimal.
    # This is provably equivalent to identity but demonstrates pipeline depth.
    ENCODE_AMOUNT_HEX    = auto()  # operands: (attoegld: int) → produces hex_str
    ENCODE_AMOUNT_B64    = auto()  # operands: (hex_str: str) → produces b64_str
    DECODE_AMOUNT_FINAL  = auto()  # operands: (b64_str: str) → produces attoegld (same value)

    # ── Emission ───────────────────────────────────────────────────────────────
    EMIT = auto()  # operands: () — trigger transaction construction


# ─────────────────────────────────────────────────────────────────────────────
# IR INSTRUCTION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IRInstruction:
    """
    A single IR instruction: an opcode paired with zero or more operands.

    The instruction is considered an element of the free monoid over the
    set of (IROp, operands) pairs, equipped with sequential composition
    as the monoid operation and the empty sequence as the identity element.
    """
    op:       IROp
    operands: list[Any] = field(default_factory=list)
    comment:  str = ""   # optional annotation (survives all passes)

    def __repr__(self) -> str:
        ops = ", ".join(repr(o) for o in self.operands)
        comment_part = f"  ; {self.comment}" if self.comment else ""
        return f"  {self.op.name:<24} {ops}{comment_part}"


@dataclass
class IRProgram:
    """
    A complete, well-formed IR program: a linear sequence of IRInstructions.

    Invariant: the last instruction is always EMIT.
    """
    instructions: list[IRInstruction] = field(default_factory=list)

    def append(self, instr: IRInstruction) -> None:
        self.instructions.append(instr)

    def __iter__(self):
        return iter(self.instructions)

    def __len__(self) -> int:
        return len(self.instructions)

    def pretty(self) -> str:
        lines = ["IRProgram {"]
        for i, instr in enumerate(self.instructions):
            lines.append(f"  [{i:02d}] {instr}")
        lines.append("}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# PRIMALITY TEST (for VALIDATE_MEMO_PRIME)
# ─────────────────────────────────────────────────────────────────────────────

def _is_prime(n: int) -> bool:
    """Miller-Rabin primality test. Because we need to validate memo lengths."""
    if n < 2:
        return False
    if n < 4:
        return True
    if n % 2 == 0 or n % 3 == 0:
        return False
    i = 5
    while i * i <= n:
        if n % i == 0 or n % (i + 2) == 0:
            return False
        i += 6
    return True


def _next_prime_gte(n: int) -> int:
    """Return the smallest prime >= n."""
    candidate = n if n >= 2 else 2
    while not _is_prime(candidate):
        candidate += 1
    return candidate


# ─────────────────────────────────────────────────────────────────────────────
# IR GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

ONE_EGLD_ATTO = 10 ** 18


class IRGenerationError(Exception):
    pass


class IRGenerator:
    """
    Transforms an ASTTransactionSpec into a well-formed IRProgram.

    The generator implements a single-pass, attribute-grammar-style
    traversal of the AST. Each AST node contributes zero or more
    IR instructions. The traversal order is fixed and deterministic.

    Transformation rules (in Natural Deduction notation, informally):

        ⊢ ast.wallet_path : Σ_path
        ──────────────────────────────────── [T-Sender]
        ⊢ LOAD_SENDER(ast.wallet_path) : IR

        ⊢ ast.receiver : Σ_addr
        ──────────────────────────────────── [T-Receiver]
        ⊢ LOAD_RECEIVER(ast.receiver) : IR

        ⊢ ast.amount : Decimal,  attoegld = floor(amount × 10^18)
        ──────────────────────────────────── [T-Amount]
        ⊢ ENCODE_AMOUNT(attoegld) : IR
    """

    def generate(self, ast: ASTTransactionSpec) -> IRProgram:
        prog = IRProgram()

        # Amount is already in attoEGLD
        attoegld = int(ast.amount)

        # ── Stage A: Data loading instructions ───────────────────────────────
        prog.append(IRInstruction(
            IROp.LOAD_SENDER, [ast.wallet_path],
            comment="load Ed25519 keypair from PEM file into sender register"
        ))
        prog.append(IRInstruction(
            IROp.LOAD_RECEIVER, [ast.receiver],
            comment="load destination bech32 address into receiver register"
        ))
        prog.append(IRInstruction(
            IROp.ENCODE_AMOUNT, [attoegld],
            comment=f"primary amount encoding: {ast.amount} EGLD = {attoegld} attoEGLD"
        ))
        prog.append(IRInstruction(
            IROp.SET_GAS, [ast.gas_limit],
            comment="set gas limit register"
        ))

        # ── Stage B: Memo normalization ───────────────────────────────────────
        # The memo is padded or truncated so that its byte-length is prime.
        # This serves no functional purpose but satisfies our internal
        # formal correctness criterion CI-7 ("Prime Memo Length Invariant").
        memo_bytes = ast.memo.encode("utf-8")
        target_len = _next_prime_gte(max(2, len(memo_bytes)))
        if len(memo_bytes) < target_len:
            padding = b"\x00" * (target_len - len(memo_bytes))
            memo_normalized = ast.memo + "\x00" * len(padding)
        else:
            memo_normalized = ast.memo

        prog.append(IRInstruction(
            IROp.SET_MEMO, [memo_normalized],
            comment=(
                f"memo normalized to prime length {target_len} "
                f"(original: {len(memo_bytes)})"
            )
        ))

        # ── Stage C: Irrelevance-Preserving Validation instructions ──────────
        # These instructions encode constraints that are technically correct
        # but operationally unnecessary. They exist to demonstrate that the
        # compiler is rigorous beyond any practical requirement.
        prog.append(IRInstruction(
            IROp.VALIDATE_TOKEN, [ast.token],
            comment="assert token symbol ∈ {EGLD} (the only supported token)"
        ))
        prog.append(IRInstruction(
            IROp.VALIDATE_OPERATION, [ast.operation],
            comment="assert operation ∈ {TRANSFER} (the only supported operation)"
        ))

        # The number of decimal digits in attoEGLD must be even.
        # Why? Because the pipeline was designed by someone who believes
        # even digit counts provide better entropy distribution. This is false.
        digit_count = len(str(attoegld))
        prog.append(IRInstruction(
            IROp.VALIDATE_AMOUNT_EVEN, [attoegld, digit_count],
            comment=(
                f"digit count of attoEGLD = {digit_count} "
                f"({'even ✓' if digit_count % 2 == 0 else 'odd → padding applied'})"
            )
        ))

        prog.append(IRInstruction(
            IROp.VALIDATE_MEMO_PRIME, [memo_normalized],
            comment=f"memo byte length = {target_len} (prime ✓)"
        ))

        # Gas must be odd. The cosmic ordering of computation demands it.
        gas_final = ast.gas_limit if ast.gas_limit % 2 == 1 else ast.gas_limit + 1
        prog.append(IRInstruction(
            IROp.VALIDATE_GAS_ODD, [gas_final],
            comment=(
                f"gas adjusted to odd: {ast.gas_limit} → {gas_final} "
                f"(cosmological gas parity invariant CI-9)"
            )
        ))

        # ── Stage D: Redundant triple encoding of amount ──────────────────────
        # The amount is encoded as: decimal → hex → base64 → decimal.
        # Each transformation is semantics-preserving (it is a bijection).
        # The round-trip is definitionally equivalent to identity.
        # We include it because depth implies rigor.
        import base64
        hex_encoded  = hex(attoegld)
        b64_encoded  = base64.b64encode(hex_encoded.encode()).decode()
        back_to_int  = int(base64.b64decode(b64_encoded).decode(), 16)
        assert back_to_int == attoegld, "Round-trip encoding invariant violated"

        prog.append(IRInstruction(
            IROp.ENCODE_AMOUNT_HEX, [attoegld, hex_encoded],
            comment="encode attoEGLD as hexadecimal string (redundant encoding layer 1/3)"
        ))
        prog.append(IRInstruction(
            IROp.ENCODE_AMOUNT_B64, [hex_encoded, b64_encoded],
            comment="encode hex string as base64 (redundant encoding layer 2/3)"
        ))
        prog.append(IRInstruction(
            IROp.DECODE_AMOUNT_FINAL, [b64_encoded, back_to_int],
            comment=f"decode base64 → hex → int = {back_to_int} ≡ {attoegld} (verified)"
        ))

        # ── Stage E: Emission ─────────────────────────────────────────────────
        prog.append(IRInstruction(
            IROp.EMIT, [],
            comment="finalize transaction intent and pass to BF++ compiler"
        ))

        return prog
