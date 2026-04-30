package co.nilin.opex.wallet.core.model

import java.math.BigDecimal
import java.time.LocalDateTime

enum class ZkScreeningDecision {
    ALLOW,
    REVIEW,
    BLOCK
}

data class WithdrawScreeningRequest(
    val ownerUuid: String,
    val currency: String,
    val amount: BigDecimal,
    val destinationSymbol: String,
    val destinationNetwork: String,
    val destinationAddress: String,
    val destinationNote: String?
)

data class WithdrawScreeningResult(
    val decision: ZkScreeningDecision,
    val reason: String? = null,
    val externalRef: String? = null
)

data class ZkAmlWithdrawCaseRecord(
    val ownerUuid: String,
    val currency: String,
    val amount: BigDecimal,
    val destinationSymbol: String,
    val destinationNetwork: String,
    val destinationAddress: String,
    val destinationNote: String?,
    val decision: ZkScreeningDecision,
    val reason: String? = null,
    val externalRef: String? = null,
    val createdAt: LocalDateTime = LocalDateTime.now()
)

data class ZkPolLiabilityEvent(
    val tokenId: String,
    val accountId: String,
    val balance: BigDecimal,
    val delta: BigDecimal,
    val eventType: String,
    val occurredAt: LocalDateTime,
    val referenceId: String,
    val sourceSystem: String = "opex-wallet"
)
