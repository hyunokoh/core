package co.nilin.opex.bcgateway.core.spi

import co.nilin.opex.bcgateway.core.model.ZkAmlDepositCaseRecord

interface ZkAmlDepositCaseRecorder {
    suspend fun record(record: ZkAmlDepositCaseRecord)
}
