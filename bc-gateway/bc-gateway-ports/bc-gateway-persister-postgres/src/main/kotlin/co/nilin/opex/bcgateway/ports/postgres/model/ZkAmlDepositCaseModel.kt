package co.nilin.opex.bcgateway.ports.postgres.model

import co.nilin.opex.bcgateway.core.model.ZkScreeningDecision
import org.springframework.data.annotation.Id
import org.springframework.data.relational.core.mapping.Column
import org.springframework.data.relational.core.mapping.Table
import java.time.LocalDateTime

@Table("zkaml_deposit_case")
data class ZkAmlDepositCaseModel(
    @Column("owner_uuid") val ownerUuid: String,
    val chain: String,
    @Column("tx_hash") val txHash: String,
    val amount: String,
    @Column("receiver_address") val receiverAddress: String,
    @Column("receiver_memo") val receiverMemo: String? = null,
    @Column("token_address") val tokenAddress: String? = null,
    val decision: ZkScreeningDecision,
    val reason: String? = null,
    @Column("external_ref") val externalRef: String? = null,
    @Column("created_at") val createdAt: LocalDateTime = LocalDateTime.now(),
    @Id val id: Long? = null
)
