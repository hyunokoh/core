package co.nilin.opex.wallet.core.spi

import co.nilin.opex.wallet.core.model.ZkAmlWithdrawCaseRecord

interface ZkAmlWithdrawCaseRecorder {
    suspend fun record(record: ZkAmlWithdrawCaseRecord)
}
