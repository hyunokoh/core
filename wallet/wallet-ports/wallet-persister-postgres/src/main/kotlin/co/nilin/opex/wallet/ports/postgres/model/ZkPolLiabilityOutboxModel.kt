package co.nilin.opex.wallet.ports.postgres.model

import org.springframework.data.annotation.Id
import org.springframework.data.relational.core.mapping.Column
import org.springframework.data.relational.core.mapping.Table
import java.math.BigDecimal
import java.time.LocalDateTime

@Table("zkpol_liability_outbox")
data class ZkPolLiabilityOutboxModel(
    @Column("token_id") val tokenId: String,
    @Column("account_id") val accountId: String,
    val balance: BigDecimal,
    val delta: BigDecimal,
    @Column("event_type") val eventType: String,
    @Column("occurred_at") val occurredAt: LocalDateTime,
    @Column("reference_id") val referenceId: String,
    @Column("source_system") val sourceSystem: String,
    @Column("created_at") val createdAt: LocalDateTime = LocalDateTime.now(),
    @Id val id: Long? = null
)
