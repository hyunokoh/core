package co.nilin.opex.wallet.ports.postgres.model

import co.nilin.opex.wallet.core.model.ZkScreeningDecision
import org.springframework.data.annotation.Id
import org.springframework.data.relational.core.mapping.Column
import org.springframework.data.relational.core.mapping.Table
import java.math.BigDecimal
import java.time.LocalDateTime

@Table("zkaml_withdraw_case")
data class ZkAmlWithdrawCaseModel(
    @Column("owner_uuid") val ownerUuid: String,
    val currency: String,
    val amount: BigDecimal,
    @Column("destination_symbol") val destinationSymbol: String,
    @Column("destination_network") val destinationNetwork: String,
    @Column("destination_address") val destinationAddress: String,
    @Column("destination_note") val destinationNote: String? = null,
    val decision: ZkScreeningDecision,
    val reason: String? = null,
    @Column("external_ref") val externalRef: String? = null,
    @Column("created_at") val createdAt: LocalDateTime = LocalDateTime.now(),
    @Id val id: Long? = null
)
