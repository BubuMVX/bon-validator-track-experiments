"""
vm.py — Brainfuck++ Virtual Machine and Bytecode Executor
==========================================================

This module implements the Abstract Machine that executes the BF++ bytecode
produced by the compiler pipeline. The VM is defined in the tradition of the
SECD machine (Landin, 1964), the Categorical Abstract Machine (Cousineau et
al., 1987), and the Zinc Abstract Machine (Leroy, 1990), though it bears
essentially no resemblance to any of them.

MACHINE ARCHITECTURE
────────────────────
The VM consists of the following components:

  1. Tape Memory (σ)
     A fixed-size array of 30,000 cells, each holding a value in [0, 255].
     Cells are indexed from 0. The tape is infinite in intent but bounded
     in implementation, following Turing's original conception modified by
     practical resource constraints.

  2. Data Pointer (DP)
     An integer index into the tape. Initialized to 0. Moves right (+) or
     left (-) in response to > and < instructions respectively.

  3. Instruction Pointer (IP)
     An integer index into the bytecode instruction list. Initialized to 0.
     Advances by 1 after each instruction unless redirected by a loop.

  4. Call Stack (κ)
     A LIFO stack of instruction pointer values, used to implement [ / ]
     loop control flow. This is a simplification of the standard BF matching
     approach; it is equivalent under the loop-balancedness invariant.

  5. Domain Registers (ρ)
     A dictionary of named registers for domain-specific values:
       WALLET_PATH   : str   — path to the PEM wallet file
       RECEIVER_ADDR : str   — bech32 destination address
       AMOUNT_ATTO   : int   — amount in attoEGLD
       GAS_LIMIT     : int   — gas limit
       MEMO          : str   — transaction memo

  6. General-Purpose Registers (A, B, C, D)
     Four 64-bit integer registers for arithmetic. Currently unused by the
     compiler but maintained for completeness and to satisfy the register
     allocation formalism described in Section 8.4 of the README.

BYTECODE FORMAT
───────────────
Each bytecode instruction is a Python tuple: (opcode: int, operand: Any)
where opcode is the integer value of a BFOp enum member and operand is either
None (standard BF instructions) or a typed value (extended instructions).

The bytecode is produced by the BytecodeCompiler in compiler.py and consumed
exclusively by this VM.

EXECUTION SEMANTICS
───────────────────
The VM executes instructions in sequential order. Extended instructions
(opcode ≥ 8) perform side effects on the domain registers. The EMIT
instruction (opcode 18) terminates execution and returns a TransactionIntent.

If the EMIT instruction is never reached, the VM raises VMError.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from brainfuck_ext import BFOp


# ─────────────────────────────────────────────────────────────────────────────
# TRANSACTION INTENT (VM output)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TransactionIntent:
    """
    The sole output of the VM execution.

    This is the distilled, semantic result of the entire eleven-layer
    compilation pipeline. It contains exactly the information needed to
    construct and send one MultiversX EGLD transfer transaction.

    In categorical terms, TransactionIntent is the terminal object in
    the category of computations produced by this VM. All roads lead here.
    """
    wallet_path:   str
    receiver_addr: str
    amount_atto:   int
    gas_limit:     int
    memo:          str
    chain_id:      str = ""   # filled by the adapter

    def describe(self) -> str:
        egld = self.amount_atto / (10 ** 18)
        return (
            f"TransactionIntent(\n"
            f"  wallet:   {self.wallet_path}\n"
            f"  receiver: {self.receiver_addr}\n"
            f"  amount:   {egld:.18f} EGLD  ({self.amount_atto} attoEGLD)\n"
            f"  gas:      {self.gas_limit}\n"
            f"  memo:     {self.memo!r}\n"
            f")"
        )


# ─────────────────────────────────────────────────────────────────────────────
# VM STATE
# ─────────────────────────────────────────────────────────────────────────────

TAPE_SIZE = 30_000

@dataclass
class VMState:
    """Complete snapshot of the VM state at any point during execution."""
    tape:       list[int]        = field(default_factory=lambda: [0] * TAPE_SIZE)
    dp:         int              = 0    # data pointer
    ip:         int              = 0    # instruction pointer
    stack:      list[int]        = field(default_factory=list)  # loop stack
    registers:  dict[str, Any]   = field(default_factory=dict)
    gp_regs:    dict[str, int]   = field(default_factory=lambda: {"A": 0, "B": 0, "C": 0, "D": 0})
    cycle_count: int             = 0
    halted:     bool             = False

    def cell(self) -> int:
        return self.tape[self.dp]

    def set_cell(self, value: int) -> None:
        self.tape[self.dp] = value & 0xFF


class VMError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# SYMBOLIC EXECUTION PASS (fake but present)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SymbolicCell:
    """
    A symbolic tape cell: either a concrete integer or a symbolic name.

    This is used by the symbolic execution pass to reason about tape values
    without actually executing them. The analysis is purely illustrative —
    it produces symbolic names like "cell_0_after_LOAD_SENDER" — but it
    demonstrates that the compiler considers symbolic reasoning a valid
    concern, even when it contributes nothing to correctness.
    """
    value: Any   # int | str (symbolic name)

    def is_symbolic(self) -> bool:
        return isinstance(self.value, str)

    def __repr__(self) -> str:
        if self.is_symbolic():
            return f"⟨{self.value}⟩"
        return str(self.value)


def symbolic_execute(bytecode: list[tuple[int, Any]]) -> dict[int, SymbolicCell]:
    """
    Perform a symbolic execution of the bytecode to produce a map from
    tape cell index to symbolic value.

    The analysis is sound but not complete: it tracks concrete values
    for cells modified by standard BF instructions and assigns symbolic
    names to cells potentially modified by extended instructions (since
    their tape effects depend on operand values not known at compile time).

    Returns a dict mapping cell index → SymbolicCell.
    The dict represents the abstract state after full execution.
    """
    sym_tape: list[SymbolicCell] = [SymbolicCell(0) for _ in range(32)]
    dp = 0
    step = 0

    for opcode, operand in bytecode:
        step += 1
        if opcode > TAPE_SIZE:
            break  # guard against malformed bytecode

        op = BFOp(opcode)

        if op == BFOp.INC:
            cell = sym_tape[dp]
            if cell.is_symbolic():
                sym_tape[dp] = SymbolicCell(f"{cell.value}+1")
            else:
                sym_tape[dp] = SymbolicCell((cell.value + 1) & 0xFF)

        elif op == BFOp.DEC:
            cell = sym_tape[dp]
            if cell.is_symbolic():
                sym_tape[dp] = SymbolicCell(f"{cell.value}-1")
            else:
                sym_tape[dp] = SymbolicCell((cell.value - 1) & 0xFF)

        elif op == BFOp.RIGHT:
            dp = min(dp + 1, len(sym_tape) - 1)

        elif op == BFOp.LEFT:
            dp = max(dp - 1, 0)

        elif op == BFOp.MULTI_INC:
            n = int(operand or 0)
            cell = sym_tape[dp]
            if cell.is_symbolic():
                sym_tape[dp] = SymbolicCell(f"{cell.value}+{n}")
            else:
                sym_tape[dp] = SymbolicCell((cell.value + n) & 0xFF)

        elif op in (BFOp.LOAD_PEM, BFOp.SET_RECV, BFOp.SET_AMOUNT,
                    BFOp.SET_GAS, BFOp.SET_MEMO):
            # Extended instructions have symbolic tape effects
            sym_tape[dp] = SymbolicCell(f"cell_{dp}_after_{op.name}_step{step}")

        # LOOP, OUTPUT, INPUT, NOP, EMIT → no symbolic tape modification

    return {i: cell for i, cell in enumerate(sym_tape) if cell.value != 0}


# ─────────────────────────────────────────────────────────────────────────────
# GAS ESTIMATION HEURISTIC
# ─────────────────────────────────────────────────────────────────────────────

import math

_TAU    = 2 * math.pi          # Tau: the one true circle constant
_EULER  = math.e               # Euler's number e ≈ 2.71828...
_GOLDEN = (1 + math.sqrt(5)) / 2  # φ ≈ 1.61803... (golden ratio)


def estimate_vm_gas(bytecode: list[tuple[int, Any]]) -> float:
    """
    Estimate the "VM gas" consumed by executing the given bytecode.

    This estimate is entirely fabricated and serves no operational purpose.
    It exists because every serious compiler has a gas estimation pass,
    and we refuse to be anything less than serious.

    The heuristic is:

        gas = Σ_i  w(opcode_i) × |operand_i|^φ  ×  (τ / e)^(i mod 7)

    where:
        w(op) = weight of opcode (standard BF = 1, extended = 3)
        |operand| = len(str(operand)) if operand else 1
        φ = golden ratio ≈ 1.618
        τ = 2π ≈ 6.283
        e = Euler's number ≈ 2.718
        i = instruction index (for the quasi-periodic oscillation term)

    The quasi-periodic oscillation term (τ/e)^(i mod 7) introduces
    a 7-cycle pattern in the gas curve, inspired by the observation
    that 7 is the most aesthetically pleasing prime number.
    """
    total = 0.0
    tau_over_e = _TAU / _EULER

    for i, (opcode, operand) in enumerate(bytecode):
        weight = 3.0 if opcode >= 8 else 1.0
        op_size = len(str(operand)) if operand is not None else 1
        oscillation = tau_over_e ** (i % 7)
        total += weight * (op_size ** _GOLDEN) * oscillation

    # Add a correction term based on tape size (completely arbitrary)
    correction = math.log(TAPE_SIZE) * _EULER * _GOLDEN
    return total + correction


# ─────────────────────────────────────────────────────────────────────────────
# VIRTUAL MACHINE
# ─────────────────────────────────────────────────────────────────────────────

class BFPlusVM:
    """
    The Brainfuck++ Virtual Machine.

    Executes a flat list of (opcode, operand) bytecode tuples.
    Standard BF instructions manipulate the tape; extended instructions
    modify domain registers. Execution terminates at EMIT, which returns
    a TransactionIntent.

    The VM runs in O(n × max_loop_iterations) time and O(TAPE_SIZE) space,
    where n is the number of bytecode instructions. For the programs produced
    by this compiler, max_loop_iterations is bounded by 256 (the cell value
    range), making the worst-case tape-clearing loop O(256) per cell.
    """

    MAX_CYCLES = 10_000_000  # safety bound: 10M cycles maximum

    def __init__(self, trace: bool = False) -> None:
        self._trace = trace
        self._trace_log: list[str] = []

    def execute(self, bytecode: list[tuple[int, Any]]) -> TransactionIntent:
        """Execute bytecode and return a TransactionIntent on success."""
        state = VMState()
        state.registers = {
            "WALLET_PATH":   "",
            "RECEIVER_ADDR": "",
            "AMOUNT_ATTO":   0,
            "GAS_LIMIT":     50_000,
            "MEMO":          "",
        }

        n = len(bytecode)

        while state.ip < n and not state.halted:
            if state.cycle_count > self.MAX_CYCLES:
                raise VMError(f"Cycle limit exceeded ({self.MAX_CYCLES})")

            opcode, operand = bytecode[state.ip]
            op = BFOp(opcode)
            state.cycle_count += 1

            if self._trace:
                self._trace_log.append(
                    f"  [cycle {state.cycle_count:06d}] IP={state.ip:04d} "
                    f"DP={state.dp:04d} cell={state.tape[state.dp]:3d} "
                    f"op={op.name:<16} "
                    f"operand={str(operand)[:30]!r}"
                )

            # ── Standard BF ──────────────────────────────────────────────────
            if op == BFOp.INC:
                state.tape[state.dp] = (state.tape[state.dp] + 1) & 0xFF
                state.ip += 1

            elif op == BFOp.DEC:
                state.tape[state.dp] = (state.tape[state.dp] - 1) & 0xFF
                state.ip += 1

            elif op == BFOp.RIGHT:
                state.dp += 1
                if state.dp >= TAPE_SIZE:
                    raise VMError(f"Data pointer out of bounds: {state.dp}")
                state.ip += 1

            elif op == BFOp.LEFT:
                state.dp -= 1
                if state.dp < 0:
                    raise VMError(f"Data pointer underflow at IP={state.ip}")
                state.ip += 1

            elif op == BFOp.LOOP_START:
                if state.tape[state.dp] == 0:
                    # Skip to matching LOOP_END
                    depth = 1
                    state.ip += 1
                    while depth > 0 and state.ip < n:
                        o2, _ = bytecode[state.ip]
                        o2_op = BFOp(o2)
                        if o2_op == BFOp.LOOP_START:
                            depth += 1
                        elif o2_op == BFOp.LOOP_END:
                            depth -= 1
                        state.ip += 1
                    if depth != 0:
                        raise VMError("Unmatched [ in bytecode")
                else:
                    state.stack.append(state.ip)
                    state.ip += 1

            elif op == BFOp.LOOP_END:
                if not state.stack:
                    raise VMError(f"Unmatched ] at IP={state.ip}")
                if state.tape[state.dp] != 0:
                    state.ip = state.stack[-1]
                else:
                    state.stack.pop()
                    state.ip += 1

            elif op == BFOp.OUTPUT:
                # Print cell value as ASCII (ceremonial)
                state.ip += 1

            elif op == BFOp.INPUT:
                # Input: set cell to 0 (no interactive input in batch mode)
                state.tape[state.dp] = 0
                state.ip += 1

            # ── BF++ Extensions ───────────────────────────────────────────────
            elif op == BFOp.LOAD_PEM:
                state.registers["WALLET_PATH"] = str(operand)
                state.ip += 1

            elif op == BFOp.SET_RECV:
                state.registers["RECEIVER_ADDR"] = str(operand)
                state.ip += 1

            elif op == BFOp.SET_AMOUNT:
                state.registers["AMOUNT_ATTO"] = int(operand)
                state.ip += 1

            elif op == BFOp.SET_GAS:
                state.registers["GAS_LIMIT"] = int(operand)
                state.ip += 1

            elif op == BFOp.SET_MEMO:
                state.registers["MEMO"] = str(operand).rstrip("\x00")
                state.ip += 1

            elif op in (BFOp.VALIDATE_TOKEN, BFOp.VALIDATE_OP):
                # Validation was already performed at IR level. Acknowledge.
                state.ip += 1

            elif op == BFOp.NOP:
                state.ip += 1  # true NOP: advance only

            elif op == BFOp.MULTI_INC:
                n_inc = int(operand or 0)
                state.tape[state.dp] = (state.tape[state.dp] + n_inc) & 0xFF
                state.ip += 1

            elif op == BFOp.MULTI_DEC:
                n_dec = int(operand or 0)
                state.tape[state.dp] = (state.tape[state.dp] - n_dec) & 0xFF
                state.ip += 1

            elif op == BFOp.EMIT:
                state.halted = True
                state.ip += 1
                # Construct and return the TransactionIntent
                r = state.registers
                if not r["WALLET_PATH"]:
                    raise VMError("EMIT reached but WALLET_PATH register is empty")
                if not r["RECEIVER_ADDR"]:
                    raise VMError("EMIT reached but RECEIVER_ADDR register is empty")
                if r["AMOUNT_ATTO"] <= 0:
                    raise VMError("EMIT reached but AMOUNT_ATTO register is zero or negative")

                return TransactionIntent(
                    wallet_path=r["WALLET_PATH"],
                    receiver_addr=r["RECEIVER_ADDR"],
                    amount_atto=r["AMOUNT_ATTO"],
                    gas_limit=r["GAS_LIMIT"],
                    memo=r["MEMO"],
                )

            else:
                raise VMError(f"Unknown opcode {opcode} at IP={state.ip}")

        raise VMError("VM halted without EMIT instruction")

    def get_trace(self) -> list[str]:
        return list(self._trace_log)
