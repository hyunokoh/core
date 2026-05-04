package co.nilin.opex.api.ports.binance.controller

import co.nilin.opex.api.core.inout.*
import co.nilin.opex.api.core.spi.MarketUserDataProxy
import co.nilin.opex.api.core.spi.MatchingGatewayProxy
import co.nilin.opex.api.core.spi.SymbolMapper
import co.nilin.opex.api.core.spi.WalletProxy
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import java.math.BigDecimal
import java.security.Principal
import java.time.LocalDateTime
import java.util.*

private class AccountControllerTest {

    @Test
    fun givenNoSymbol_whenOpenOrdersRequested_thenQueryAllOpenOrdersAndReturnAliasSymbols(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)
        val principal = Principal { "user-1" }

        val responses = controller.fetchOpenOrders(principal, null, null, 1L, 25)

        assertThat(queryHandler.openOrdersSymbol).isNull()
        assertThat(queryHandler.openOrdersLimit).isEqualTo(25)
        assertThat(responses).hasSize(1)
        assertThat(responses.first().symbol).isEqualTo("ETHUSDT")
    }

    @Test
    fun givenNoSymbol_whenAllOrdersRequested_thenQueryAllOrdersAndReturnAliasSymbols(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)
        val principal = Principal { "user-1" }

        val responses = controller.fetchAllOrders(principal, null, null, null, 50, null, 1L)

        assertThat(queryHandler.allOrdersSymbol).isNull()
        assertThat(queryHandler.allOrdersLimit).isEqualTo(50)
        assertThat(responses).hasSize(1)
        assertThat(responses.first().symbol).isEqualTo("ETHUSDT")
    }

    private fun controller(queryHandler: RecordingMarketUserDataProxy) = AccountController(
        queryHandler,
        RecordingMatchingGatewayProxy(),
        RecordingWalletProxy(),
        RecordingSymbolMapper()
    )

    private class RecordingMarketUserDataProxy : MarketUserDataProxy {
        var openOrdersSymbol: String? = "not-called"
        var openOrdersLimit: Int? = null
        var allOrdersSymbol: String? = "not-called"
        var allOrdersLimit: Int? = null

        override suspend fun queryOrder(
            principal: Principal,
            symbol: String,
            orderId: Long?,
            origClientOrderId: String?
        ): Order? = null

        override suspend fun openOrders(principal: Principal, symbol: String?, limit: Int?): List<Order> {
            openOrdersSymbol = symbol
            openOrdersLimit = limit
            return listOf(order())
        }

        override suspend fun allOrders(
            principal: Principal,
            symbol: String?,
            startTime: Date?,
            endTime: Date?,
            limit: Int?
        ): List<Order> {
            allOrdersSymbol = symbol
            allOrdersLimit = limit
            return listOf(order())
        }

        override suspend fun allTrades(
            principal: Principal,
            symbol: String?,
            fromTrade: Long?,
            startTime: Date?,
            endTime: Date?,
            limit: Int?
        ): List<Trade> = emptyList()

        private fun order() = Order(
            id = 1,
            ouid = "ouid-1",
            uuid = "user-1",
            clientOrderId = "client-1",
            symbol = "ETH_USDT",
            orderId = 100,
            makerFee = BigDecimal("0.001"),
            takerFee = BigDecimal("0.001"),
            leftSideFraction = BigDecimal.ONE,
            rightSideFraction = BigDecimal.ONE,
            userLevel = "*",
            direction = OrderDirection.ASK,
            constraint = MatchConstraint.GTC,
            type = MatchingOrderType.LIMIT_ORDER,
            price = BigDecimal("100"),
            quantity = BigDecimal("0.5"),
            quoteQuantity = BigDecimal.ZERO,
            executedQuantity = BigDecimal.ZERO,
            accumulativeQuoteQty = BigDecimal.ZERO,
            status = OrderStatus.NEW,
            createDate = LocalDateTime.now(),
            updateDate = LocalDateTime.now()
        )
    }

    private class RecordingSymbolMapper : SymbolMapper {
        override suspend fun fromInternalSymbol(symbol: String?): String? =
            when (symbol) {
                "ETH_USDT" -> "ETHUSDT"
                else -> symbol
            }

        override suspend fun toInternalSymbol(alias: String?): String? =
            when (alias) {
                "ETHUSDT" -> "ETH_USDT"
                else -> null
            }

        override suspend fun symbolToAliasMap(): Map<String, String> = mapOf("ETH_USDT" to "ETHUSDT")
    }

    private class RecordingMatchingGatewayProxy : MatchingGatewayProxy {
        override suspend fun createNewOrder(
            uuid: String?,
            pair: String,
            price: BigDecimal,
            quantity: BigDecimal,
            direction: OrderDirection,
            matchConstraint: MatchConstraint?,
            orderType: MatchingOrderType,
            userLevel: String,
            token: String?
        ): OrderSubmitResult? = null

        override suspend fun cancelOrder(
            ouid: String,
            uuid: String,
            orderId: Long,
            symbol: String,
            token: String?
        ): OrderSubmitResult? = null
    }

    private class RecordingWalletProxy : WalletProxy {
        override suspend fun getWallets(uuid: String?, token: String?): List<Wallet> = emptyList()

        override suspend fun getWallet(uuid: String?, token: String?, symbol: String): Wallet =
            Wallet(symbol, BigDecimal.ZERO, BigDecimal.ZERO, BigDecimal.ZERO)

        override suspend fun getOwnerLimits(uuid: String?, token: String?): OwnerLimitsResponse =
            OwnerLimitsResponse(canTrade = true, canWithdraw = true, canDeposit = true)

        override suspend fun getDepositTransactions(
            uuid: String,
            token: String?,
            coin: String?,
            startTime: Long?,
            endTime: Long?,
            limit: Int,
            offset: Int,
            ascendingByTime: Boolean?
        ): List<TransactionHistoryResponse> = emptyList()

        override suspend fun getWithdrawTransactions(
            uuid: String,
            token: String?,
            coin: String?,
            startTime: Long?,
            endTime: Long?,
            limit: Int,
            offset: Int,
            ascendingByTime: Boolean?
        ): List<WithdrawHistoryResponse> = emptyList()
    }
}
