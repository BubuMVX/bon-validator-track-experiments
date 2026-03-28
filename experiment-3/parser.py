"""
parser.py — Formalized Transaction Intent Declaration Language (FTIDL) Parser
=============================================================================

Implements a full recursive-descent parser for the FTIDL v1.0 specification
language. The parser proceeds through three sub-phases:

  Phase 1 — Lexical Analysis (Tokenization)
    The input character stream is reduced to a token stream via a deterministic
    finite automaton encoded as a sequence of compiled regular expressions.
    Whitespace and comment nodes are consumed and discarded, preserving only
    semantically meaningful tokens. This is a standard scanner construction
    following Aho, Lam, Sethi, and Ullman (Dragon Book, 2nd ed., §3).

  Phase 2 — Syntactic Analysis (Recursive Descent)
    The token stream is consumed by a top-down, predictive parser whose
    grammar is LL(1). The grammar has been factored to eliminate left
    recursion and common prefixes, ensuring that each production can be
    selected deterministically by examining a single lookahead token.

  Phase 3 — AST Construction
    Each grammar production emits a typed AST node. The resulting tree
    is a shallow, normalized representation of the source transaction
    specification, suitable for IR generation.

The AST nodes defined herein correspond to the μ-calculus interpretation
of the FTIDL semantics: each node is a closed term whose meaning is
independent of the evaluation context (modulo address resolution, which
is deferred to the IR phase).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

class TokenKind(Enum):
    KEYWORD       = auto()  # TRANSACTION, OPERATION, TOKEN, AMOUNT, FROM, ...
    IDENT         = auto()  # TRANSFER, EGLD, WALLET, ADDRESS
    STRING        = auto()  # "..."
    NUMBER        = auto()  # 0.1, 50000
    LBRACE        = auto()  # {
    RBRACE        = auto()  # }
    COMMENT       = auto()  # # ...
    EOF           = auto()


KEYWORDS = frozenset({
    "TRANSACTION", "OPERATION", "TOKEN", "AMOUNT",
    "FROM", "TO", "GAS_LIMIT", "MEMO",
    "WALLET", "ADDRESS", "TRANSFER", "EGLD",
})

TOKEN_PATTERNS: list[tuple[TokenKind, str]] = [
    (TokenKind.COMMENT, r"#[^\n]*"),
    (TokenKind.STRING,  r'"[^"]*"'),
    (TokenKind.NUMBER,  r"\d+\.\d+|\d+"),
    (TokenKind.LBRACE,  r"\{"),
    (TokenKind.RBRACE,  r"\}"),
    (TokenKind.IDENT,   r"[A-Za-z_][A-Za-z0-9_]*"),
]

_MASTER_RE = re.compile(
    "|".join(f"(?P<{kind.name}_{i}>{pattern})"
             for i, (kind, pattern) in enumerate(TOKEN_PATTERNS)),
    re.MULTILINE,
)


@dataclass(frozen=True)
class Token:
    kind:  TokenKind
    value: str
    line:  int
    col:   int

    def __repr__(self) -> str:
        return f"Token({self.kind.name}, {self.value!r}, L{self.line}:C{self.col})"


# ─────────────────────────────────────────────────────────────────────────────
# LEXER
# ─────────────────────────────────────────────────────────────────────────────

class LexError(Exception):
    pass


def tokenize(source: str) -> list[Token]:
    """
    Convert a FTIDL source string into a flat list of tokens.

    The tokenizer is implemented as a single-pass scan over the input using
    a compiled master regex that encodes the union of all token patterns.
    Matches are tagged with their source position for error reporting.
    Comment tokens are silently discarded.
    """
    tokens: list[Token] = []
    line = 1
    line_start = 0

    for m in _MASTER_RE.finditer(source):
        col = m.start() - line_start + 1
        raw = m.group()

        # Determine which pattern matched by finding a non-None group
        kind: Optional[TokenKind] = None
        for i, (k, _) in enumerate(TOKEN_PATTERNS):
            if m.group(f"{k.name}_{i}") is not None:
                kind = k
                break

        assert kind is not None, f"Unmatched group at L{line}:C{col}"

        # Count newlines in the match to update line tracking
        newlines = raw.count("\n")
        if newlines:
            line += newlines
            line_start = m.start() + raw.rfind("\n") + 1

        if kind == TokenKind.COMMENT:
            continue  # Discard

        # Promote matching IDENTs to KEYWORD kind
        if kind == TokenKind.IDENT and raw.upper() in KEYWORDS:
            kind = TokenKind.KEYWORD

        tokens.append(Token(kind, raw, line, col))

    # Check for unmatched characters
    matched_spans = set()
    for m in _MASTER_RE.finditer(source):
        matched_spans.update(range(m.start(), m.end()))

    for i, ch in enumerate(source):
        if ch not in (" ", "\t", "\n", "\r") and i not in matched_spans:
            raise LexError(f"Unexpected character {ch!r} at position {i}")

    tokens.append(Token(TokenKind.EOF, "", line, col=0))
    return tokens


# ─────────────────────────────────────────────────────────────────────────────
# AST NODE DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ASTTransactionSpec:
    """
    Root AST node. Represents a complete FTIDL TRANSACTION block.

    In the type-theoretic sense, this is a product type:
        TransactionSpec ≅ Operation × Token × Amount × Sender × Receiver
                         × GasLimit × Memo

    where each component is a first-class citizen in the FTIDL type universe.
    """
    operation:   str              # e.g. "TRANSFER"
    token:       str              # e.g. "EGLD"
    amount:      Decimal          # decimal amount (e.g. Decimal("0.1"))
    wallet_path: str              # path to PEM file
    receiver:    str              # bech32 address
    gas_limit:   int              # gas limit (integer)
    memo:        str              # memo string (may be empty)
    source_line: int = field(default=0, repr=False)  # for error reporting


# ─────────────────────────────────────────────────────────────────────────────
# PARSER
# ─────────────────────────────────────────────────────────────────────────────

class ParseError(Exception):
    pass


class FTIDLParser:
    """
    Recursive-descent LL(1) parser for the FTIDL v1.0 grammar.

    The parser maintains a cursor into the token stream and implements
    one method per grammar production. Each method either returns a
    node (on success) or raises ParseError (on failure).

    The grammar in EBNF (simplified):

        program         ::= transaction EOF
        transaction     ::= "TRANSACTION" "{" field* "}"
        field           ::= operation_field
                          | token_field
                          | amount_field
                          | from_field
                          | to_field
                          | gas_field
                          | memo_field
        operation_field ::= "OPERATION" IDENT
        token_field     ::= "TOKEN" IDENT
        amount_field    ::= "AMOUNT" NUMBER
        from_field      ::= "FROM" "WALLET" STRING
        to_field        ::= "TO" "ADDRESS" STRING
        gas_field       ::= "GAS_LIMIT" NUMBER
        memo_field      ::= "MEMO" STRING
    """

    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._pos    = 0

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _peek(self) -> Token:
        return self._tokens[self._pos]

    def _advance(self) -> Token:
        tok = self._tokens[self._pos]
        if tok.kind != TokenKind.EOF:
            self._pos += 1
        return tok

    def _expect(self, kind: TokenKind, value: Optional[str] = None) -> Token:
        tok = self._advance()
        if tok.kind != kind:
            raise ParseError(
                f"Expected token {kind.name} but got {tok.kind.name} "
                f"({tok.value!r}) at L{tok.line}:C{tok.col}"
            )
        if value is not None and tok.value.upper() != value.upper():
            raise ParseError(
                f"Expected '{value}' but got '{tok.value}' "
                f"at L{tok.line}:C{tok.col}"
            )
        return tok

    def _match_keyword(self, kw: str) -> bool:
        tok = self._peek()
        return tok.kind == TokenKind.KEYWORD and tok.value.upper() == kw.upper()

    # ── Grammar productions ───────────────────────────────────────────────────

    def parse(self) -> ASTTransactionSpec:
        node = self._parse_transaction()
        self._expect(TokenKind.EOF)
        return node

    def _parse_transaction(self) -> ASTTransactionSpec:
        self._expect(TokenKind.KEYWORD, "TRANSACTION")
        start_line = self._peek().line
        self._expect(TokenKind.LBRACE)

        fields: dict[str, object] = {
            "operation":   None,
            "token":       None,
            "amount":      None,
            "wallet_path": None,
            "receiver":    None,
            "gas_limit":   None,
            "memo":        "",
        }

        while not self._match_keyword("") and self._peek().kind != TokenKind.RBRACE:
            if self._peek().kind == TokenKind.EOF:
                raise ParseError("Unexpected EOF inside TRANSACTION block")
            self._parse_field(fields)

        self._expect(TokenKind.RBRACE)

        # Validate that required fields are present
        required = ["operation", "token", "amount", "wallet_path", "receiver", "gas_limit"]
        for req in required:
            if fields[req] is None:
                raise ParseError(f"Missing required field '{req.upper()}' in TRANSACTION block")

        return ASTTransactionSpec(
            operation=str(fields["operation"]),
            token=str(fields["token"]),
            amount=Decimal(str(fields["amount"])),
            wallet_path=str(fields["wallet_path"]),
            receiver=str(fields["receiver"]),
            gas_limit=int(str(fields["gas_limit"])),
            memo=str(fields["memo"]),
            source_line=start_line,
        )

    def _parse_field(self, out: dict) -> None:
        tok = self._peek()

        if tok.kind not in (TokenKind.KEYWORD,):
            raise ParseError(
                f"Expected field keyword, got {tok.kind.name} ({tok.value!r}) "
                f"at L{tok.line}:C{tok.col}"
            )

        kw = tok.value.upper()

        if kw == "OPERATION":
            self._advance()
            val = self._advance()
            out["operation"] = val.value
        elif kw == "TOKEN":
            self._advance()
            val = self._advance()
            out["token"] = val.value
        elif kw == "AMOUNT":
            self._advance()
            val = self._expect(TokenKind.NUMBER)
            out["amount"] = Decimal(val.value)
        elif kw == "FROM":
            self._advance()
            self._expect(TokenKind.KEYWORD, "WALLET")
            path_tok = self._expect(TokenKind.STRING)
            out["wallet_path"] = path_tok.value.strip('"')
        elif kw == "TO":
            self._advance()
            self._expect(TokenKind.KEYWORD, "ADDRESS")
            addr_tok = self._expect(TokenKind.STRING)
            out["receiver"] = addr_tok.value.strip('"')
        elif kw == "GAS_LIMIT":
            self._advance()
            val = self._expect(TokenKind.NUMBER)
            out["gas_limit"] = int(val.value)
        elif kw == "MEMO":
            self._advance()
            val = self._expect(TokenKind.STRING)
            out["memo"] = val.value.strip('"')
        else:
            raise ParseError(f"Unknown field keyword '{kw}' at L{tok.line}:C{tok.col}")


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def parse_spec(source: str) -> ASTTransactionSpec:
    """
    Parse a FTIDL specification string into an AST.

    This is the sole public entry point for the parser module.
    All internal tokenization and parse-tree construction is encapsulated.
    """
    tokens = tokenize(source)
    parser = FTIDLParser(tokens)
    return parser.parse()


def parse_spec_file(path: str) -> ASTTransactionSpec:
    """Convenience wrapper: read file then parse."""
    with open(path, encoding="utf-8") as f:
        source = f.read()
    return parse_spec(source)
