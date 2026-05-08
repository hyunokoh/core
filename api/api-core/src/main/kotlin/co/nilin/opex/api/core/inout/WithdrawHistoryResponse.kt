package co.nilin.opex.api.core.inout

import java.math.BigDecimal
import java.time.LocalDateTime

data class WithdrawHistoryResponse(
    val withdrawId: Long?,
    val uuid: String,
    val amount: BigDecimal,
    val currency: String,
    val acceptedFee: BigDecimal? = null,
    val appliedFee: BigDecimal?,
    val destAmount: BigDecimal?,
    val destSymbol: String?,
    val destAddress: String?,
    val destNetwork: String?,
    var destNote: String?,
    var destTransactionRef: String?,
    val statusReason: String?,
    val status: String,
    val createDate: LocalDateTime,
    val acceptDate: LocalDateTime?
)
