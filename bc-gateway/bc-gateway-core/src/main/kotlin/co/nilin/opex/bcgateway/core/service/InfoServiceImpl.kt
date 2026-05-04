package co.nilin.opex.bcgateway.core.service

import co.nilin.opex.bcgateway.core.api.InfoService
import co.nilin.opex.bcgateway.core.model.CurrencyInfo
import co.nilin.opex.bcgateway.core.spi.CurrencyHandler
import co.nilin.opex.bcgateway.core.spi.ReservedAddressHandler

class InfoServiceImpl(
    private val currencyHandler: CurrencyHandler,
    private val reservedAddressHandler: ReservedAddressHandler
) : InfoService {
    override suspend fun countReservedAddresses(): Long {
        return reservedAddressHandler.count()
    }

    override suspend fun getCurrencyInfo(symbol: String): CurrencyInfo {
        return currencyHandler.fetchCurrencyInfo(symbol)
    }
}
