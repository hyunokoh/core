package co.nilin.opex.wallet.app.integration.zk

import co.nilin.opex.wallet.core.model.ZkAmlWithdrawCaseRecord
import co.nilin.opex.wallet.core.spi.ZkAmlWithdrawCaseRecorder
import co.nilin.opex.wallet.ports.postgres.dao.ZkAmlWithdrawCaseRepository
import co.nilin.opex.wallet.ports.postgres.model.ZkAmlWithdrawCaseModel
import kotlinx.coroutines.reactive.awaitFirstOrNull
import org.springframework.beans.factory.annotation.Value
import org.springframework.stereotype.Component

@Component
class ZkAmlWithdrawCaseRecorderAdapter(
    private val repository: ZkAmlWithdrawCaseRepository
) : ZkAmlWithdrawCaseRecorder {

    @Value("\${app.zkaml.case-recording.enabled:true}")
    private var enabled: Boolean = true

    override suspend fun record(record: ZkAmlWithdrawCaseRecord) {
        if (!enabled) {
            return
        }

        repository.save(
            ZkAmlWithdrawCaseModel(
                ownerUuid = record.ownerUuid,
                currency = record.currency,
                amount = record.amount,
                destinationSymbol = record.destinationSymbol,
                destinationNetwork = record.destinationNetwork,
                destinationAddress = record.destinationAddress,
                destinationNote = record.destinationNote,
                decision = record.decision,
                reason = record.reason,
                externalRef = record.externalRef,
                createdAt = record.createdAt
            )
        ).awaitFirstOrNull()
    }
}
