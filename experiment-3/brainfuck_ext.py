"""
brainfuck_ext.py — Brainfuck++ Language Definition and IR→BF++ Compiler
========================================================================

This module defines the Brainfuck++ (BF++) language, an extension of the
original Brainfuck esoteric programming language (Urban Müller, 1993) with
domain-specific opcodes for blockchain transaction construction.

Brainfuck++ preserves the original Turing-complete core of Brainfuck while
adding high-level "domain instructions" that abstract over the tape-memory
protocol for the specific task of EGLD transfer specification. This design
follows the "embedded domain-specific language" (EDSL) paradigm as discussed
in Hudak (1996), "Building Domain-Specific Embedded Languages."

ORIGINAL BRAINFUCK SEMANTICS
────────────────────────────
The canonical Brainfuck machine is defined over:
  - An infinite tape of cells, each holding an 8-bit unsigned integer
  - A data pointer (DP) pointing to the current cell
  - An instruction pointer (IP) advancing through the program

Instructions:
  +   increment cell at DP (mod 256)
  -   decrement cell at DP (mod 256)
  >   move DP right by 1
  <   move DP left by 1
  [   if cell[DP] == 0, jump to matching ]
  ]   if cell[DP] != 0, jump to matching [
  .   output cell[DP] as ASCII
  ,   read one byte into cell[DP]

BRAINFUCK++ EXTENSIONS
──────────────────────
The following domain instructions are added. Each is prefixed with @ to
distinguish it from standard BF instructions. Extended instructions operate
on named registers rather than the tape, providing a higher-level interface
that the VM can dispatch to the MultiversX adapter.

  @LOAD_PEM{path}          load wallet from PEM file into WALLET register
  @SET_RECV{addr}          set RECEIVER register to bech32 address
  @SET_AMOUNT{n}           set AMOUNT register to n (attoEGLD, integer)
  @SET_GAS{n}              set GAS register to n
  @SET_MEMO{s}             set MEMO register to s
  @VALIDATE_TOKEN{sym}     assert token symbol is valid
  @VALIDATE_OP{op}         assert operation is valid
  @NOP                     no-operation (inserted by optimization pass)
  @EMIT                    finalize and dispatch transaction intent

TAPE USAGE CONVENTION
──────────────────────
Extended instructions do NOT use the tape. Standard BF instructions still
manipulate the tape as usual. The tape is maintained purely for ceremonial
reasons — to prove that BF++ is a proper extension of Brainfuck.
Cells 0–9 are reserved for "amount encoding theater" (see compiler below).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

from ir import IRInstruction, IROp, IRProgram


# ─────────────────────────────────────────────────────────────────────────────
# BF++ OPCODE DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

class BFOp(Enum):
    """
    Complete BF++ instruction set.

    Opcodes 0–7 are the canonical Brainfuck instructions.
    Opcodes 8+ are the domain extensions.
    """
    # ── Standard Brainfuck ────────────────────────────────────────────────────
    INC        = 0   # +
    DEC        = 1   # -
    RIGHT      = 2   # >
    LEFT       = 3   # <
    LOOP_START = 4   # [
    LOOP_END   = 5   # ]
    OUTPUT     = 6   # .
    INPUT      = 7   # ,

    # ── BF++ Extensions ───────────────────────────────────────────────────────
    LOAD_PEM         = 8    # @LOAD_PEM{path}
    SET_RECV         = 9    # @SET_RECV{addr}
    SET_AMOUNT       = 10   # @SET_AMOUNT{n}
    SET_GAS          = 11   # @SET_GAS{n}
    SET_MEMO         = 12   # @SET_MEMO{s}
    VALIDATE_TOKEN   = 13   # @VALIDATE_TOKEN{sym}
    VALIDATE_OP      = 14   # @VALIDATE_OP{name}
    NOP              = 15   # @NOP (inserted by optimization pass)
    MULTI_INC        = 16   # @MULTI_INC{n} (optimization artifact: n consecutive +)
    MULTI_DEC        = 17   # @MULTI_DEC{n} (optimization artifact: n consecutive -)
    EMIT             = 18   # @EMIT


# ─────────────────────────────────────────────────────────────────────────────
# BF++ INSTRUCTION AND PROGRAM
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BFInstruction:
    """
    A single Brainfuck++ instruction: an opcode plus an optional operand.

    The operand is None for standard BF instructions and non-None for
    extended instructions. For MULTI_INC/MULTI_DEC, the operand is an int.
    For all other extended ops, the operand is a string.
    """
    op:      BFOp
    operand: Optional[Any] = None
    comment: str = ""

    def to_source(self) -> str:
        """Render this instruction as BF++ source text."""
        # Standard BF symbols
        SYMBOLS = {
            BFOp.INC:        "+",
            BFOp.DEC:        "-",
            BFOp.RIGHT:      ">",
            BFOp.LEFT:       "<",
            BFOp.LOOP_START: "[",
            BFOp.LOOP_END:   "]",
            BFOp.OUTPUT:     ".",
            BFOp.INPUT:      ",",
        }
        if self.op in SYMBOLS:
            return SYMBOLS[self.op]
        if self.op == BFOp.NOP:
            return "@NOP"
        if self.op == BFOp.MULTI_INC:
            return f"@MULTI_INC{{{self.operand}}}"
        if self.op == BFOp.MULTI_DEC:
            return f"@MULTI_DEC{{{self.operand}}}"
        return f"@{self.op.name}{{{self.operand}}}"

    def __repr__(self) -> str:
        src = self.to_source()
        comment_part = f"  // {self.comment}" if self.comment else ""
        return f"{src}{comment_part}"


@dataclass
class BFProgram:
    """A linear sequence of BF++ instructions."""
    instructions: list[BFInstruction] = field(default_factory=list)

    def append(self, instr: BFInstruction) -> None:
        self.instructions.append(instr)

    def extend(self, instrs: list[BFInstruction]) -> None:
        self.instructions.extend(instrs)

    def __iter__(self):
        return iter(self.instructions)

    def __len__(self) -> int:
        return len(self.instructions)

    def to_source(self) -> str:
        """
        Render the full BF++ program as a source string.
        Standard BF instructions are run together; extended instructions
        appear on their own lines for readability.
        """
        lines: list[str] = []
        run: list[str] = []

        for instr in self.instructions:
            src = instr.to_source()
            if src.startswith("@"):
                if run:
                    lines.append("".join(run))
                    run = []
                suffix = f"  // {instr.comment}" if instr.comment else ""
                lines.append(src + suffix)
            else:
                run.append(src)

        if run:
            lines.append("".join(run))

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# IR → BF++ COMPILER
# ─────────────────────────────────────────────────────────────────────────────

def _inc_sequence(n: int) -> list[BFInstruction]:
    """
    Generate n consecutive INC instructions.
    Used to encode small values onto the tape ceremonially.
    """
    return [BFInstruction(BFOp.INC) for _ in range(n % 256)]


def _move_right(n: int) -> list[BFInstruction]:
    return [BFInstruction(BFOp.RIGHT) for _ in range(n)]


class IRToBFCompiler:
    """
    Compiles an IRProgram into a BFProgram.

    Each IR instruction maps to one or more BF++ instructions.
    Standard BF instructions are generated for the "tape ceremony" —
    a sequence of tape manipulations that encode no information but
    demonstrate that the tape is being used (for Turing completeness theater).

    Tape ceremony layout:
        Cell 0: operation code (1 = TRANSFER)
        Cell 1: token code     (1 = EGLD)
        Cell 2: amount LSB fragment (lowest 8 bits of attoEGLD)
        Cell 3: amount flag    (1 = amount loaded)
        Cell 4: gas LSB fragment
        Cell 5: memo length mod 256
        Cell 6: emit flag      (set to 1 just before EMIT)
    """

    def compile(self, ir: IRProgram) -> BFProgram:
        prog = BFProgram()

        # ── Tape ceremony preamble ────────────────────────────────────────────
        # Zero all ceremony cells (they start at 0 but we do it explicitly
        # because that's what a real compiler would do)
        prog.append(BFInstruction(BFOp.NOP, comment="tape ceremony preamble: reset cells 0-6"))
        for _ in range(7):
            # Set to 0: cell starts at 0, loop that decrements to 0 = [−]
            prog.extend([
                BFInstruction(BFOp.LOOP_START),
                BFInstruction(BFOp.DEC),
                BFInstruction(BFOp.LOOP_END),
                BFInstruction(BFOp.RIGHT),
            ])
        # Return to cell 0
        prog.extend(_move_right(0))  # stay at cell 7 (off-ceremony area)
        # Move back to cell 0
        for _ in range(7):
            prog.append(BFInstruction(BFOp.LEFT))

        # ── Compile each IR instruction ───────────────────────────────────────
        for ir_instr in ir:
            self._compile_instruction(ir_instr, prog)

        return prog

    def _compile_instruction(self, instr: IRInstruction, prog: BFProgram) -> None:
        op = instr.op

        if op == IROp.LOAD_SENDER:
            wallet_path = instr.operands[0]
            # Cell 0 ← 1 (operation code for TRANSFER)
            prog.extend(_inc_sequence(1))
            prog.append(BFInstruction(
                BFOp.LOAD_PEM, wallet_path,
                comment=instr.comment
            ))

        elif op == IROp.LOAD_RECEIVER:
            addr = instr.operands[0]
            prog.append(BFInstruction(BFOp.RIGHT))  # advance to cell 1
            prog.extend(_inc_sequence(1))            # cell 1 ← 1 (token code)
            prog.append(BFInstruction(BFOp.LEFT))    # return to cell 0
            prog.append(BFInstruction(
                BFOp.SET_RECV, addr,
                comment=instr.comment
            ))

        elif op == IROp.ENCODE_AMOUNT:
            attoegld = instr.operands[0]
            lsb = attoegld & 0xFF
            # Cell 2 ← lsb (ceremonial amount encoding)
            prog.extend(_move_right(2))
            prog.extend(_inc_sequence(lsb))
            prog.extend([BFInstruction(BFOp.LEFT), BFInstruction(BFOp.LEFT)])
            # Set amount flag cell 3 ← 1
            prog.extend(_move_right(3))
            prog.extend(_inc_sequence(1))
            prog.extend([BFInstruction(BFOp.LEFT)] * 3)
            prog.append(BFInstruction(
                BFOp.SET_AMOUNT, attoegld,
                comment=instr.comment
            ))

        elif op == IROp.SET_GAS:
            gas = instr.operands[0]
            lsb = gas & 0xFF
            prog.extend(_move_right(4))
            prog.extend(_inc_sequence(lsb))
            prog.extend([BFInstruction(BFOp.LEFT)] * 4)
            prog.append(BFInstruction(
                BFOp.SET_GAS, gas,
                comment=instr.comment
            ))

        elif op == IROp.SET_MEMO:
            memo = instr.operands[0]
            memo_len_mod = len(memo.encode()) % 256
            prog.extend(_move_right(5))
            prog.extend(_inc_sequence(memo_len_mod))
            prog.extend([BFInstruction(BFOp.LEFT)] * 5)
            prog.append(BFInstruction(
                BFOp.SET_MEMO, memo,
                comment=instr.comment
            ))

        elif op == IROp.VALIDATE_TOKEN:
            prog.append(BFInstruction(
                BFOp.VALIDATE_TOKEN, instr.operands[0],
                comment=instr.comment
            ))

        elif op == IROp.VALIDATE_OPERATION:
            prog.append(BFInstruction(
                BFOp.VALIDATE_OP, instr.operands[0],
                comment=instr.comment
            ))

        elif op in (
            IROp.VALIDATE_AMOUNT_EVEN,
            IROp.VALIDATE_MEMO_PRIME,
            IROp.VALIDATE_GAS_ODD,
            IROp.ENCODE_AMOUNT_HEX,
            IROp.ENCODE_AMOUNT_B64,
            IROp.DECODE_AMOUNT_FINAL,
        ):
            # These map to NOPs at the BF++ level.
            # The validation and redundant encoding were performed during IR
            # generation; the BF++ layer acknowledges their existence via NOP.
            prog.append(BFInstruction(
                BFOp.NOP,
                comment=f"(absorbed) {op.name}: {instr.comment}"
            ))

        elif op == IROp.EMIT:
            # Cell 6 ← 1 (emit flag)
            prog.extend(_move_right(6))
            prog.extend(_inc_sequence(1))
            prog.extend([BFInstruction(BFOp.LEFT)] * 6)
            prog.append(BFInstruction(
                BFOp.EMIT,
                comment="dispatch transaction intent to VM output register"
            ))

        else:
            prog.append(BFInstruction(BFOp.NOP, comment=f"unhandled op: {op.name}"))
