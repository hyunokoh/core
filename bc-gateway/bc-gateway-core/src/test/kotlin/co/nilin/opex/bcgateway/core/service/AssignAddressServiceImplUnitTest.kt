package co.nilin.opex.bcgateway.core.service

import co.nilin.opex.bcgateway.core.model.*
import co.nilin.opex.bcgateway.core.model.Currency
import co.nilin.opex.bcgateway.core.spi.AssignedAddressHandler
import co.nilin.opex.bcgateway.core.spi.CurrencyHandler
import co.nilin.opex.bcgateway.core.spi.ReservedAddressHandler
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.Test
import java.math.BigDecimal
import java.util.*

class AssignAddressServiceImplUnitTest {

    private val currency = Currency("ETH", "Ethereum")
    private val chain = "ETH_MAINNET"
    private val ethAddressType = AddressType(1, "ETH", "+*", ".*")
    private val ethMemoAddressType = AddressType(2, "ETH", "+*", "+*")
    private val ethChain = Chain("ETH_MAINNET", arrayListOf(ethAddressType))
    private val bscChain = Chain("BSC_MAINNET", arrayListOf(ethAddressType, ethMemoAddressType))

    @Test
    fun givenReservedAddressAndUserWithNoAssignedAddress_whenAssignAddress_thenReservedAddressAssigned(): Unit =
        runBlocking {
            val user = UUID.randomUUID().toString()
            val assignedAddressHandler = InMemoryAssignedAddressHandler()
            val reservedAddressHandler = InMemoryReservedAddressHandler(
                ReservedAddress("0x1", null, ethAddressType),
                ReservedAddress("0x2", "Memo", ethMemoAddressType)
            )
            val assignAddressServiceImpl = assignAddressService(assignedAddressHandler, reservedAddressHandler)

            val assignedAddress = assignAddressServiceImpl.assignAddress(user, currency, chain)
            assertThat(assignedAddress).isEqualTo(
                listOf(
                    AssignedAddress(
                        user,
                        "0x1",
                        null,
                        ethAddressType,
                        mutableListOf(ethChain)

                    )
                )
            )
            assertThat(assignedAddressHandler.persisted).containsExactlyElementsOf(assignedAddress)
            assertThat(reservedAddressHandler.remaining()).doesNotContain(ReservedAddress("0x1", null, ethAddressType))
        }

    @Test
    fun givenNoReservedAddressAndUserWithNoAssignedAddress_whenAssignAddress_thenExcpetion(): Unit = runBlocking {
        val user = UUID.randomUUID().toString()
        val assignedAddressHandler = InMemoryAssignedAddressHandler()
        val reservedAddressHandler = InMemoryReservedAddressHandler()
        val assignAddressServiceImpl = assignAddressService(assignedAddressHandler, reservedAddressHandler)

        assertThatThrownBy {
            runBlocking { assignAddressServiceImpl.assignAddress(user, currency, chain) }
        }.isInstanceOf(RuntimeException::class.java)
    }

    @Test
    fun givenReservedAddressAndUserOneAssignedAddress_whenAssignAddress_thenReservedAddressAssigned(): Unit =
        runBlocking {
            val user = UUID.randomUUID().toString()
            val existingAddress = AssignedAddress(
                user,
                "0x1",
                null,
                ethAddressType,
                mutableListOf(ethChain)
            )
            val assignedAddressHandler = InMemoryAssignedAddressHandler(existingAddress)
            val reservedAddressHandler = InMemoryReservedAddressHandler(
                ReservedAddress("0x1", null, ethAddressType),
                ReservedAddress("0x2", "Memo", ethMemoAddressType)
            )
            val assignAddressServiceImpl = assignAddressService(assignedAddressHandler, reservedAddressHandler)

            val assignedAddress = assignAddressServiceImpl.assignAddress(user, currency, chain)
            assertThat(assignedAddress).isEqualTo(
                listOf(
                    AssignedAddress(
                        user,
                        "0x1",
                        null,
                        ethAddressType,
                        mutableListOf(ethChain)
                    )
                )
            )
            assertThat(assignedAddressHandler.persisted).containsExactlyElementsOf(assignedAddress)
            assertThat(reservedAddressHandler.remaining()).contains(
                ReservedAddress("0x1", null, ethAddressType),
                ReservedAddress("0x2", "Memo", ethMemoAddressType)
            )
        }

    private fun assignAddressService(
        assignedAddressHandler: AssignedAddressHandler,
        reservedAddressHandler: ReservedAddressHandler
    ) = AssignAddressServiceImpl(
        FixedCurrencyHandler(currencyInfo()),
        assignedAddressHandler,
        reservedAddressHandler
    )

    private fun currencyInfo(): CurrencyInfo {
        val eth = CurrencyImplementation(
            currency,
            currency,
            ethChain,
            false,
            null,
            null,
            true,
            BigDecimal.ONE,
            BigDecimal.TEN,
            18
        )
        val wrappedEth = CurrencyImplementation(
            currency,
            currency,
            bscChain,
            false,
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "WETH",
            true,
            BigDecimal.ONE,
            BigDecimal.ONE,
            18
        )
        return CurrencyInfo(currency, listOf(eth, wrappedEth))
    }

}

private class FixedCurrencyHandler(private val currencyInfo: CurrencyInfo) : CurrencyHandler {

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

    private fun unsupported(): Nothing = throw UnsupportedOperationException("Not used by AssignAddressServiceImpl")
}

private class InMemoryAssignedAddressHandler(
    vararg initialAddresses: AssignedAddress
) : AssignedAddressHandler {
    private val addresses = initialAddresses.toMutableList()
    val persisted = mutableListOf<AssignedAddress>()

    override suspend fun fetchAssignedAddresses(user: String, addressTypes: List<AddressType>): List<AssignedAddress> =
        addresses.filter { it.uuid == user && it.type in addressTypes }

    override suspend fun persist(assignedAddress: AssignedAddress) {
        addresses.removeIf { it.uuid == assignedAddress.uuid && it.type == assignedAddress.type }
        addresses.add(assignedAddress)
        persisted.add(assignedAddress)
    }

    override suspend fun revoke(assignedAddress: AssignedAddress) {
        addresses.remove(assignedAddress)
    }

    override suspend fun findUuid(address: String, memo: String?): String? =
        addresses.firstOrNull { it.address == address && it.memo == memo }?.uuid

    override suspend fun fetchExpiredAssignedAddresses(): List<AssignedAddress>? = emptyList()
}

private class InMemoryReservedAddressHandler(
    vararg initialAddresses: ReservedAddress
) : ReservedAddressHandler {
    private val addresses = initialAddresses.toMutableList()

    override suspend fun addReservedAddress(list: List<ReservedAddress>) {
        addresses.addAll(list)
    }

    override suspend fun peekReservedAddress(addressType: AddressType): ReservedAddress? =
        addresses.firstOrNull { it.type == addressType }

    override suspend fun remove(reservedAddress: ReservedAddress) {
        addresses.remove(reservedAddress)
    }

    fun remaining(): List<ReservedAddress> = addresses.toList()
}
