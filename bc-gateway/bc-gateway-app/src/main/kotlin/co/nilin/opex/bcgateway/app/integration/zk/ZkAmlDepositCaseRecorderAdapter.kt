package co.nilin.opex.bcgateway.app.integration.zk

import co.nilin.opex.bcgateway.core.model.ZkAmlDepositCaseRecord
import co.nilin.opex.bcgateway.core.spi.ZkAmlDepositCaseRecorder
import co.nilin.opex.bcgateway.ports.postgres.dao.ZkAmlDepositCaseRepository
import co.nilin.opex.bcgateway.ports.postgres.model.ZkAmlDepositCaseModel
import kotlinx.coroutines.reactive.awaitFirstOrNull
import org.springframework.beans.factory.annotation.Value
import org.springframework.stereotype.Component

@Component
class ZkAmlDepositCaseRecorderAdapter(
    private val repository: ZkAmlDepositCaseRepository
) : ZkAmlDepositCaseRecorder {

    @Value("\${app.zkaml.case-recording.enabled:true}")
    private var enabled: Boolean = true

    override suspend fun record(record: ZkAmlDepositCaseRecord) {
        if (!enabled) {
            return
        }

        repository.save(
            ZkAmlDepositCaseModel(
                ownerUuid = record.ownerUuid,
                chain = record.chain,
                txHash = record.txHash,
                amount = record.amount,
                receiverAddress = record.receiverAddress,
                receiverMemo = record.receiverMemo,
                tokenAddress = record.tokenAddress,
                decision = record.decision,
                reason = record.reason,
                externalRef = record.externalRef,
                createdAt = record.createdAt
            )
        ).awaitFirstOrNull()
    }
}
