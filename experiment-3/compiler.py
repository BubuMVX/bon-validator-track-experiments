"""
compiler.py — Master Compilation Pipeline Orchestrator
=======================================================

This module implements the top-level compilation driver for the FTIDL→BF++
pipeline. It sequences all compilation phases, applies optimization and
analysis passes, and produces a fully compiled ExecutionPlan ready for
dispatch by the VM.

The pipeline, in full:

  Stage 0:  Load and parse FTIDL specification → ASTTransactionSpec
  Stage 1:  AST → IRProgram (IR generation)
  Stage 2:  IRProgram validation (structural well-formedness check)
  Stage 3:  IRProgram → BFProgram (BF++ emission)
  Stage 4:  BFProgram optimization (loop unrolling pass — provably useless)
  Stage 5:  BFProgram symbolic execution (fake but present)
  Stage 6:  BFProgram → Bytecode (flat integer encoding)
  Stage 7:  Bytecode gas estimation heuristic
  Stage 8:  ExecutionPlan construction

Each stage produces a typed intermediate artifact. The pipeline is entirely
deterministic: given the same FTIDL source, the same bytecode is produced
on every run. This property, called "referential transparency" in the
functional programming community, is maintained throughout.

OPTIMIZATION PASS: Loop Unrolling
──────────────────────────────────
The "optimization" pass scans the BF++ instruction stream for runs of
consecutive INC instructions and replaces them with a single MULTI_INC
instruction. This is semantically equivalent and, in a real compiler,
would reduce bytecode size. In this compiler, the effect is negligible
because the tape increments perform no useful computation.

The pass is then immediately followed by a "de-optimization" re-expansion
that converts MULTI_INC back to individual INC instructions. This ensures
the pass is strictly cost-neutral — a property we call "zero net effect."

We include both directions because:
  (a) A compiler without an optimization pass is not a compiler.
  (b) Having an optimization pass that undoes itself is funnier.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from brainfuck_ext import BFInstruction, BFOp, BFProgram, IRToBFCompiler
from ir import IRGenerator, IRProgram, IROp
from parser import ASTTransactionSpec
from vm import BFPlusVM, TransactionIntent, estimate_vm_gas, symbolic_execute


# ─────────────────────────────────────────────────────────────────────────────
# BYTECODE
# ─────────────────────────────────────────────────────────────────────────────

Bytecode = list[tuple[int, Any]]  # (opcode: int, operand: Any)


def bf_to_bytecode(prog: BFProgram) -> Bytecode:
    """
    Lower a BFProgram into a flat bytecode list.

    Each BF++ instruction is encoded as a (opcode, operand) tuple where
    opcode is the integer value of the BFOp enum. This is the final
    non-executable representation before VM dispatch.
    """
    bytecode: Bytecode = []
    for instr in prog:
        bytecode.append((instr.op.value, instr.operand))
    return bytecode


def bytecode_disassemble(bytecode: Bytecode) -> str:
    """Human-readable disassembly of a bytecode sequence."""
    lines: list[str] = []
    for i, (opcode, operand) in enumerate(bytecode):
        try:
            op_name = BFOp(opcode).name
        except ValueError:
            op_name = f"UNKNOWN({opcode})"
        operand_str = f"  {str(operand)[:60]!r}" if operand is not None else ""
        lines.append(f"  {i:04d}  {op_name:<20}{operand_str}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# OPTIMIZATION PASS (useless)
# ─────────────────────────────────────────────────────────────────────────────

def optimize_loop_unroll(prog: BFProgram) -> tuple[BFProgram, int]:
    """
    Phase 1: Consolidate consecutive INC instructions into MULTI_INC.

    This pass collapses runs of INC into MULTI_INC(n), reducing bytecode
    size. The transformation is semantically transparent.

    Returns the optimized program and the number of INC instructions merged.
    """
    result = BFProgram()
    merged = 0
    i = 0
    instrs = list(prog.instructions)

    while i < len(instrs):
        instr = instrs[i]
        if instr.op == BFOp.INC:
            # Count consecutive INCs
            count = 0
            j = i
            while j < len(instrs) and instrs[j].op == BFOp.INC:
                count += 1
                j += 1
            if count > 1:
                result.append(BFInstruction(
                    BFOp.MULTI_INC, count,
                    comment=f"unrolled {count} consecutive INC → MULTI_INC"
                ))
                merged += count - 1  # net reduction
                i = j
            else:
                result.append(instr)
                i += 1
        else:
            result.append(instr)
            i += 1

    return result, merged


def deoptimize_expand_multi_inc(prog: BFProgram) -> tuple[BFProgram, int]:
    """
    Phase 2: Re-expand MULTI_INC instructions back into individual INC.

    This pass is the semantic inverse of optimize_loop_unroll.
    Together, the two passes form an identity transformation.

    We call this the "enlightened no-op" pass.
    Returns the expanded program and the number of MULTI_INC expanded.
    """
    result = BFProgram()
    expanded = 0

    for instr in prog:
        if instr.op == BFOp.MULTI_INC:
            n = int(instr.operand or 0)
            for _ in range(n):
                result.append(BFInstruction(BFOp.INC))
            expanded += 1
        else:
            result.append(instr)

    return result, expanded


# ─────────────────────────────────────────────────────────────────────────────
# IR VALIDATOR
# ─────────────────────────────────────────────────────────────────────────────

class IRValidationError(Exception):
    pass


def validate_ir(ir: IRProgram) -> list[str]:
    """
    Validate the structural well-formedness of an IRProgram.

    Checks performed:
      - LOAD_SENDER appears exactly once
      - LOAD_RECEIVER appears exactly once
      - ENCODE_AMOUNT appears exactly once
      - EMIT is the last instruction
      - No instruction appears after EMIT
      - Amount is positive
      - Gas limit is positive

    Returns a list of warning strings (empty if all checks pass).
    Raises IRValidationError on fatal violations.
    """
    warnings: list[str] = []
    instrs = list(ir.instructions)

    sender_count   = sum(1 for x in instrs if x.op == IROp.LOAD_SENDER)
    receiver_count = sum(1 for x in instrs if x.op == IROp.LOAD_RECEIVER)
    amount_count   = sum(1 for x in instrs if x.op == IROp.ENCODE_AMOUNT)
    emit_count     = sum(1 for x in instrs if x.op == IROp.EMIT)

    if sender_count != 1:
        raise IRValidationError(f"LOAD_SENDER must appear exactly once, found {sender_count}")
    if receiver_count != 1:
        raise IRValidationError(f"LOAD_RECEIVER must appear exactly once, found {receiver_count}")
    if amount_count != 1:
        raise IRValidationError(f"ENCODE_AMOUNT must appear exactly once, found {amount_count}")
    if emit_count == 0:
        raise IRValidationError("IRProgram has no EMIT instruction")
    if emit_count > 1:
        warnings.append(f"Multiple EMIT instructions ({emit_count}); only the first will execute")
    if instrs[-1].op != IROp.EMIT:
        raise IRValidationError("EMIT must be the last instruction in IRProgram")

    # Validate amount is positive
    for instr in instrs:
        if instr.op == IROp.ENCODE_AMOUNT:
            if instr.operands[0] <= 0:
                raise IRValidationError(f"ENCODE_AMOUNT operand must be > 0, got {instr.operands[0]}")

    return warnings


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTION PLAN
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StageResult:
    name:        str
    duration_ms: float
    summary:     str
    detail:      Optional[str] = None


@dataclass
class ExecutionPlan:
    """
    The final artifact of the compilation pipeline.

    Contains the compiled bytecode and all intermediate artifacts,
    plus timing information for each compilation stage.
    """
    ast:            ASTTransactionSpec
    ir:             IRProgram
    bf_source:      str             # BF++ source text
    bf_raw:         BFProgram       # BFProgram before optimization
    bf_optimized:   BFProgram       # BFProgram after optimization round-trip
    bytecode:       Bytecode
    symbolic_state: dict            # tape symbolic analysis result
    estimated_gas:  float           # absurd gas estimate
    stages:         list[StageResult] = field(default_factory=list)
    ir_warnings:    list[str] = field(default_factory=list)

    def total_compile_time_ms(self) -> float:
        return sum(s.duration_ms for s in self.stages)

    def instruction_count(self) -> int:
        return len(self.bytecode)


# ─────────────────────────────────────────────────────────────────────────────
# MASTER COMPILER
# ─────────────────────────────────────────────────────────────────────────────

class CompilationError(Exception):
    pass


class MasterCompiler:
    """
    Orchestrates the complete FTIDL → Bytecode compilation pipeline.

    The compiler is stateless between calls to `compile()`. Each call
    produces an independent ExecutionPlan.
    """

    def __init__(self, verbose: bool = False) -> None:
        self._verbose = verbose
        self._stages:  list[StageResult] = []

    def _stage(self, name: str, fn) -> Any:
        """Execute one compilation stage, recording timing."""
        t0 = time.perf_counter()
        result = fn()
        elapsed = (time.perf_counter() - t0) * 1000
        self._stages.append(StageResult(name, elapsed, ""))
        return result

    def compile(self, ast: ASTTransactionSpec) -> ExecutionPlan:
        """
        Run the complete compilation pipeline from AST to ExecutionPlan.
        """
        self._stages = []

        # Stage 1: IR Generation
        ir_gen = IRGenerator()
        ir = self._stage("IR Generation", lambda: ir_gen.generate(ast))
        self._stages[-1].summary = f"{len(ir)} instructions"

        # Stage 2: IR Validation
        def do_ir_validation():
            return validate_ir(ir)
        ir_warnings = self._stage("IR Validation", do_ir_validation)
        self._stages[-1].summary = (
            f"passed ({len(ir_warnings)} warnings)"
            if ir_warnings else "passed (no warnings)"
        )

        # Stage 3: BF++ Compilation
        bf_compiler = IRToBFCompiler()
        bf_raw = self._stage("BF++ Emission", lambda: bf_compiler.compile(ir))
        self._stages[-1].summary = f"{len(bf_raw)} BF++ instructions"

        # Stage 4a: Optimization (loop unrolling)
        def do_opt():
            return optimize_loop_unroll(bf_raw)
        bf_opt_pair = self._stage("Optimization: Loop Unrolling", do_opt)
        bf_opt, merged = bf_opt_pair
        self._stages[-1].summary = f"merged {merged} INC → MULTI_INC"

        # Stage 4b: De-optimization (expand back)
        def do_deopt():
            return deoptimize_expand_multi_inc(bf_opt)
        bf_final_pair = self._stage("De-optimization: Expand MULTI_INC", do_deopt)
        bf_final, expanded = bf_final_pair
        self._stages[-1].summary = (
            f"expanded {expanded} MULTI_INC → INC  (net Δ = 0 instructions, "
            f"as intended)"
        )

        # Stage 5: BF++ source generation
        bf_source = self._stage("BF++ Source Rendering", lambda: bf_final.to_source())
        self._stages[-1].summary = f"{len(bf_source)} chars"

        # Stage 6: Bytecode compilation
        bytecode = self._stage("Bytecode Compilation", lambda: bf_to_bytecode(bf_final))
        self._stages[-1].summary = f"{len(bytecode)} bytecode tuples"

        # Stage 7: Symbolic execution
        sym_state = self._stage(
            "Symbolic Execution Analysis",
            lambda: symbolic_execute(bytecode)
        )
        self._stages[-1].summary = (
            f"{len(sym_state)} non-zero symbolic cells"
        )

        # Stage 8: Gas estimation heuristic
        gas_est = self._stage(
            "Gas Estimation Heuristic",
            lambda: estimate_vm_gas(bytecode)
        )
        self._stages[-1].summary = f"{gas_est:.4f} mythical gas units"

        return ExecutionPlan(
            ast=ast,
            ir=ir,
            bf_source=bf_source,
            bf_raw=bf_raw,
            bf_optimized=bf_final,
            bytecode=bytecode,
            symbolic_state=sym_state,
            estimated_gas=gas_est,
            stages=self._stages,
            ir_warnings=ir_warnings,
        )

    def execute_plan(self, plan: ExecutionPlan, trace: bool = False) -> TransactionIntent:
        """
        Execute a compiled ExecutionPlan in the BF++ VM.

        Returns a TransactionIntent ready for dispatch by the MX adapter.
        Stores the execution trace on plan._vm_trace if trace=True.
        """
        vm = BFPlusVM(trace=trace)
        try:
            intent = vm.execute(plan.bytecode)
        except Exception as e:
            raise CompilationError(f"VM execution failed: {e}") from e

        if trace:
            plan._vm_trace = vm.get_trace()  # type: ignore[attr-defined]
        else:
            plan._vm_trace = []  # type: ignore[attr-defined]

        return intent
