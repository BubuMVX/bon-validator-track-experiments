# ============================================================================
# Formalized Transaction Intent Declaration Language (FTIDL) v1.0
# MultiversX Transfer Specification Document
#
# This file constitutes the highest level of abstraction in the
# eleven-layer compilation pipeline. It is the only artifact a
# human being with less than three PhDs is expected to author.
#
# Grammar: see README.md §3.1 (Formal Grammar in Backus-Naur Form)
# ============================================================================

TRANSACTION {
    OPERATION  TRANSFER
    TOKEN      EGLD
    AMOUNT     100000000000000000
    FROM       WALLET  "wallet.pem"
    TO         ADDRESS "erd1a6x8vdeyt3g4tqrf0yucpryy3u9n0h0eqjh2djv24xdxpdlznncss0gq5a"
    GAS_LIMIT  50000
    MEMO       "challenge-8-experiment-3-ftidl-bf-plus-plus"
}
