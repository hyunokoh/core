package co.nilin.opex.bcgateway.core.spi

import co.nilin.opex.bcgateway.core.model.DepositScreeningRequest
import co.nilin.opex.bcgateway.core.model.DepositScreeningResult

interface ZkAmlDepositScreeningService {
    suspend fun screenDeposit(request: DepositScreeningRequest): DepositScreeningResult
}
