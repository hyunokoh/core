package co.nilin.opex.api.ports.binance.controller

import co.nilin.opex.api.core.inout.*
import co.nilin.opex.api.core.spi.MarketUserDataProxy
import co.nilin.opex.api.core.spi.MatchingGatewayProxy
import co.nilin.opex.api.core.spi.SymbolMapper
import co.nilin.opex.api.core.spi.WalletProxy
import co.nilin.opex.common.OpexError
import co.nilin.opex.utility.error.data.OpexException
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.Test
import org.springframework.security.core.context.SecurityContext
import org.springframework.security.core.context.SecurityContextImpl
import org.springframework.security.oauth2.jwt.Jwt
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken
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

    @Test
    fun givenNoLimit_whenAllOrdersRequested_thenUseBinanceDefaultLimit(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        controller.fetchAllOrders(Principal { "user-1" }, "ETHUSDT", null, null, null, null, 1L)

        assertThat(queryHandler.allOrdersSymbol).isEqualTo("ETH_USDT")
        assertThat(queryHandler.allOrdersLimit).isEqualTo(500)
    }

    @Test
    fun givenTooLargeLimit_whenAllOrdersRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking { controller.fetchAllOrders(Principal { "user-1" }, "ETHUSDT", null, null, 1001, null, 1L) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.allOrdersSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenInvalidLimit_whenMyTradesRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking { controller.fetchAllTrades(Principal { "user-1" }, "ETHUSDT", null, null, null, 0, null, 1L) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.allTradesSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenNoLimit_whenMyTradesRequested_thenUseBinanceDefaultLimit(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        controller.fetchAllTrades(Principal { "user-1" }, "ETHUSDT", null, null, null, null, null, 1L)

        assertThat(queryHandler.allTradesSymbol).isEqualTo("ETH_USDT")
        assertThat(queryHandler.allTradesLimit).isEqualTo(500)
    }

    @Test
    fun givenUnsupportedOrderType_whenCreateOrderRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.createNewOrder(
                    symbol = "ETHUSDT",
                    side = OrderSide.BUY,
                    type = OrderType.STOP_LOSS,
                    timeInForce = null,
                    quantity = BigDecimal("0.5"),
                    quoteOrderQty = null,
                    price = null,
                    newClientOrderId = null,
                    stopPrice = BigDecimal("90"),
                    icebergQty = null,
                    newOrderRespType = null,
                    recvWindow = null,
                    timestamp = 1L,
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenQuoteOrderQuantityMarketOrder_whenCreateOrderRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.createNewOrder(
                    symbol = "ETHUSDT",
                    side = OrderSide.BUY,
                    type = OrderType.MARKET,
                    timeInForce = null,
                    quantity = null,
                    quoteOrderQty = BigDecimal("100"),
                    price = null,
                    newClientOrderId = null,
                    stopPrice = null,
                    icebergQty = null,
                    newOrderRespType = null,
                    recvWindow = null,
                    timestamp = 1L,
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenLimitOrder_whenCreateOrderRequested_thenSubmitExpectedMatchingOrder(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        controller.createNewOrder(
            symbol = "ETHUSDT",
            side = OrderSide.SELL,
            type = OrderType.LIMIT,
            timeInForce = TimeInForce.GTC,
            quantity = BigDecimal("0.5"),
            quoteOrderQty = null,
            price = BigDecimal("100"),
            newClientOrderId = null,
            stopPrice = null,
            icebergQty = null,
            newOrderRespType = null,
            recvWindow = null,
            timestamp = 1L,
            securityContext = securityContext()
        )

        assertThat(matchingGatewayProxy.createOrderCallCount).isEqualTo(1)
        assertThat(matchingGatewayProxy.createOrderUuid).isEqualTo("user-1")
        assertThat(matchingGatewayProxy.createOrderPair).isEqualTo("ETH_USDT")
        assertThat(matchingGatewayProxy.createOrderPrice).isEqualByComparingTo("100")
        assertThat(matchingGatewayProxy.createOrderQuantity).isEqualByComparingTo("0.5")
        assertThat(matchingGatewayProxy.createOrderDirection).isEqualTo(OrderDirection.ASK)
        assertThat(matchingGatewayProxy.createOrderConstraint).isEqualTo(MatchConstraint.GTC)
        assertThat(matchingGatewayProxy.createOrderType).isEqualTo(MatchingOrderType.LIMIT_ORDER)
        assertThat(matchingGatewayProxy.createOrderToken).isEqualTo("token-1")
    }

    private fun controller(
        queryHandler: RecordingMarketUserDataProxy = RecordingMarketUserDataProxy(),
        matchingGatewayProxy: RecordingMatchingGatewayProxy = RecordingMatchingGatewayProxy()
    ) = AccountController(
        queryHandler,
        matchingGatewayProxy,
        RecordingWalletProxy(),
        RecordingSymbolMapper()
    )

    private fun securityContext(): SecurityContext {
        val jwt = Jwt.withTokenValue("token-1")
            .header("alg", "none")
            .subject("user-1")
            .build()
        return SecurityContextImpl(JwtAuthenticationToken(jwt))
    }

    private fun org.assertj.core.api.AbstractThrowableAssert<*, out Throwable>.isOpexError(error: OpexError) {
        isInstanceOf(OpexException::class.java)
            .extracting("error")
            .isEqualTo(error)
    }

    private class RecordingMarketUserDataProxy : MarketUserDataProxy {
        var openOrdersSymbol: String? = "not-called"
        var openOrdersLimit: Int? = null
        var allOrdersSymbol: String? = "not-called"
        var allOrdersLimit: Int? = null
        var allTradesSymbol: String? = "not-called"
        var allTradesLimit: Int? = null

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
        ): List<Trade> {
            allTradesSymbol = symbol
            allTradesLimit = limit
            return emptyList()
        }

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
        var createOrderCallCount = 0
        var createOrderUuid: String? = null
        var createOrderPair: String? = null
        var createOrderPrice: BigDecimal? = null
        var createOrderQuantity: BigDecimal? = null
        var createOrderDirection: OrderDirection? = null
        var createOrderConstraint: MatchConstraint? = null
        var createOrderType: MatchingOrderType? = null
        var createOrderToken: String? = null

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
        ): OrderSubmitResult? {
            createOrderCallCount += 1
            createOrderUuid = uuid
            createOrderPair = pair
            createOrderPrice = price
            createOrderQuantity = quantity
            createOrderDirection = direction
            createOrderConstraint = matchConstraint
            createOrderType = orderType
            createOrderToken = token
            return OrderSubmitResult(1)
        }

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
