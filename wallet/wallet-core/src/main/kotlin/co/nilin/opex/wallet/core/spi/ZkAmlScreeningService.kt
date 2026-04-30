package co.nilin.opex.wallet.core.spi

import co.nilin.opex.wallet.core.model.WithdrawScreeningRequest
import co.nilin.opex.wallet.core.model.WithdrawScreeningResult

interface ZkAmlScreeningService {
    suspend fun screenWithdraw(request: WithdrawScreeningRequest): WithdrawScreeningResult
}
