package co.nilin.opex.bcgateway.core.model

import java.time.LocalDateTime

enum class ZkScreeningDecision {
    ALLOW,
    REVIEW,
    BLOCK
}

data class DepositScreeningRequest(
    val ownerUuid: String,
    val chain: String,
    val txHash: String,
    val amount: String,
    val receiverAddress: String,
    val receiverMemo: String?,
    val tokenAddress: String?
)

data class DepositScreeningResult(
    val decision: ZkScreeningDecision,
    val reason: String? = null,
    val externalRef: String? = null
)

data class ZkAmlDepositCaseRecord(
    val ownerUuid: String,
    val chain: String,
    val txHash: String,
    val amount: String,
    val receiverAddress: String,
    val receiverMemo: String?,
    val tokenAddress: String?,
    val decision: ZkScreeningDecision,
    val reason: String? = null,
    val externalRef: String? = null,
    val createdAt: LocalDateTime = LocalDateTime.now()
)
