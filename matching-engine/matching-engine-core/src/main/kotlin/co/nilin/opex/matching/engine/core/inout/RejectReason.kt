package co.nilin.opex.matching.engine.core.inout

enum class RejectReason {
    ORDER_TYPE_NOT_MATCHED_MATCHC,
    ORDER_NOT_FOUND,
    OPERATION_NOT_MATCHED_MATCHC,
    SELF_TRADE_PREVENTION,
    DUPLICATE_CLIENT_ORDER_ID,
    INVALID_ORDER
}
