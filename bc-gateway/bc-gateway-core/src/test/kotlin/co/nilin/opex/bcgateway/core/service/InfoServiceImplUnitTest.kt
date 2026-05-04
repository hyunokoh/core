package co.nilin.opex.bcgateway.core.service

import co.nilin.opex.bcgateway.core.model.AddressType
import co.nilin.opex.bcgateway.core.model.Chain
import co.nilin.opex.bcgateway.core.model.Currency
import co.nilin.opex.bcgateway.core.model.CurrencyImplementation
import co.nilin.opex.bcgateway.core.model.CurrencyInfo
import co.nilin.opex.bcgateway.core.model.ReservedAddress
import co.nilin.opex.bcgateway.core.model.WithdrawData
import co.nilin.opex.bcgateway.core.spi.CurrencyHandler
import co.nilin.opex.bcgateway.core.spi.ReservedAddressHandler
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import java.math.BigDecimal

class InfoServiceImplUnitTest {

    @Test
    fun givenReservedAddresses_whenCountReservedAddresses_thenReturnHandlerCount(): Unit = runBlocking {
        val service = InfoServiceImpl(
            StaticCurrencyHandler(currencyInfo("BTC")),
            CountingReservedAddressHandler(3)
        )

        assertThat(service.countReservedAddresses()).isEqualTo(3)
    }

    @Test
    fun givenSymbol_whenGetCurrencyInfo_thenReturnCurrencyHandlerInfo(): Unit = runBlocking {
        val service = InfoServiceImpl(
            StaticCurrencyHandler(currencyInfo("ETH")),
            CountingReservedAddressHandler(0)
        )

        val info = service.getCurrencyInfo("eth")

        assertThat(info.currency.symbol).isEqualTo("ETH")
        assertThat(info.implementations).hasSize(1)
    }

    private fun currencyInfo(symbol: String): CurrencyInfo {
        val currency = Currency(symbol, symbol)
        return CurrencyInfo(
            currency,
            listOf(
                CurrencyImplementation(
                    currency,
                    currency,
                    Chain("${symbol}_MAINNET", listOf(AddressType(1, symbol, ".*", ".*"))),
                    false,
                    null,
                    null,
                    true,
                    BigDecimal.ONE,
                    BigDecimal.ONE,
                    18
                )
            )
        )
    }
}

private class CountingReservedAddressHandler(private val count: Long) : ReservedAddressHandler {
    override suspend fun addReservedAddress(list: List<ReservedAddress>) = unsupported()

    override suspend fun peekReservedAddress(addressType: AddressType): ReservedAddress? = unsupported()

    override suspend fun remove(reservedAddress: ReservedAddress) = unsupported()

    override suspend fun count(): Long = count

    private fun unsupported(): Nothing = throw UnsupportedOperationException("Not used by InfoServiceImplUnitTest")
}

private class StaticCurrencyHandler(private val currencyInfo: CurrencyInfo) : CurrencyHandler {
    override suspend fun addCurrency(name: String, symbol: String) = unsupported()

    override suspend fun addCurrencyImplementationV2(
        currencySymbol: String,
        implementationSymbol: String,
        currencyName: String,
        chain: String,
        tokenName: String?,
        tokenAddress: String?,
        isToken: Boolean,
        withdrawFee: BigDecimal,
        minimumWithdraw: BigDecimal,
        isWithdrawEnabled: Boolean,
        decimal: Int
    ): CurrencyImplementation? = unsupported()

    override suspend fun updateCurrencyImplementation(
        currencySymbol: String,
        implementationSymbol: String,
        currencyName: String,
        newChain: String?,
        tokenName: String?,
        tokenAddress: String?,
        isToken: Boolean,
        withdrawFee: BigDecimal,
        minimumWithdraw: BigDecimal,
        isWithdrawEnabled: Boolean,
        decimal: Int,
        chain: String
    ): CurrencyImplementation? = unsupported()

    override suspend fun editCurrency(name: String, symbol: String) = unsupported()

    override suspend fun deleteCurrency(name: String) = unsupported()

    override suspend fun addCurrencyImplementation(
        currencySymbol: String,
        implementationSymbol: String,
        chain: String,
        tokenName: String?,
        tokenAddress: String?,
        isToken: Boolean,
        withdrawFee: BigDecimal,
        minimumWithdraw: BigDecimal,
        isWithdrawEnabled: Boolean,
        decimal: Int
    ): CurrencyImplementation = unsupported()

    override suspend fun fetchAllImplementations(): List<CurrencyImplementation> = unsupported()

    override suspend fun fetchCurrencyInfo(symbol: String): CurrencyInfo = currencyInfo

    override suspend fun findByChainAndTokenAddress(chain: String, address: String?): CurrencyImplementation? =
        unsupported()

    override suspend fun findImplementationsWithTokenOnChain(chain: String): List<CurrencyImplementation> =
        unsupported()

    override suspend fun findImplementationsByCurrency(currency: String): List<CurrencyImplementation> =
        unsupported()

    override suspend fun changeWithdrawStatus(symbol: String, chain: String, status: Boolean) = unsupported()

    override suspend fun getWithdrawData(symbol: String, network: String): WithdrawData = unsupported()

    private fun unsupported(): Nothing = throw UnsupportedOperationException("Not used by InfoServiceImplUnitTest")
}
