package co.nilin.opex.market.ports.postgres.impl

import co.nilin.opex.common.OpexError
import co.nilin.opex.market.core.inout.OrderStatus
import co.nilin.opex.market.core.inout.QueryOrderRequest
import co.nilin.opex.market.core.inout.AllOrderRequest
import co.nilin.opex.market.core.inout.TradeRequest
import co.nilin.opex.market.ports.postgres.MarketPostgresIntegrationTest
import co.nilin.opex.market.ports.postgres.dao.OpenOrderRepository
import co.nilin.opex.market.ports.postgres.dao.OrderRepository
import co.nilin.opex.market.ports.postgres.dao.OrderStatusRepository
import co.nilin.opex.market.ports.postgres.dao.TradeRepository
import co.nilin.opex.market.ports.postgres.impl.sample.VALID
import co.nilin.opex.market.ports.postgres.model.OrderModel
import co.nilin.opex.market.ports.postgres.model.TradeModel
import co.nilin.opex.utility.error.data.OpexException
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.reactor.awaitSingleOrNull
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.r2dbc.core.DatabaseClient
import java.math.BigDecimal

private class UserQueryHandlerTest : MarketPostgresIntegrationTest() {
    @Autowired
    private lateinit var databaseClient: DatabaseClient

    @Autowired
    private lateinit var orderRepository: OrderRepository

    @Autowired
    private lateinit var tradeRepository: TradeRepository

    @Autowired
    private lateinit var orderStatusRepository: OrderStatusRepository

    @Autowired
    private lateinit var openOrderRepository: OpenOrderRepository

    private val userQueryHandler by lazy {
        UserQueryHandlerImpl(orderRepository, tradeRepository, orderStatusRepository)
    }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        databaseClient.executeSql("truncate table trades, open_orders, order_status, orders restart identity cascade")
    }

    @Test
    fun givenOrder_whenAllOrders_thenReturnOrdersForUser(): Unit = runBlocking {
        seedOrder(status = OrderStatus.FILLED)

        val orders = userQueryHandler.allOrders(VALID.PRINCIPAL.name, AllOrderRequest(VALID.ETH_USDT, null, null, 100))

        assertThat(orders).hasSize(1)
        assertThat(orders.first().ouid).isEqualTo(VALID.MAKER_ORDER_MODEL.ouid)
        assertThat(orders.first().status).isEqualTo(OrderStatus.FILLED)
    }

    @Test
    fun givenOpenOrder_whenOpenOrders_thenReturnOpenOrdersForUser(): Unit = runBlocking {
        seedOrder(status = OrderStatus.NEW, open = true)

        val orders = userQueryHandler.openOrders(VALID.PRINCIPAL.name, VALID.ETH_USDT, 100)

        assertThat(orders).hasSize(1)
        assertThat(orders.first().status).isEqualTo(OrderStatus.NEW)
    }

    @Test
    fun givenClientOrderId_whenQueryOrder_thenReturnOrder(): Unit = runBlocking {
        seedOrder(status = OrderStatus.FILLED)

        val order = userQueryHandler.queryOrder(
            VALID.PRINCIPAL.name,
            QueryOrderRequest(VALID.ETH_USDT, null, "2")
        )

        assertThat(order).isNotNull
        assertThat(order!!.ouid).isEqualTo(VALID.MAKER_ORDER_MODEL.ouid)
    }

    @Test
    fun givenSameClientOrderIdForDifferentUsers_whenQueryOrder_thenReturnRequesterOrder(): Unit = runBlocking {
        seedOrder(
            VALID.MAKER_ORDER_MODEL.copy(
                id = null,
                ouid = "intruder-order",
                uuid = "intruder-user",
                clientOrderId = "shared-client-id",
                orderId = 10
            ),
            status = OrderStatus.FILLED
        )
        seedOrder(
            VALID.MAKER_ORDER_MODEL.copy(
                id = null,
                ouid = "owner-order",
                uuid = VALID.PRINCIPAL.name,
                clientOrderId = "shared-client-id",
                orderId = 11
            ),
            status = OrderStatus.NEW
        )

        val order = userQueryHandler.queryOrder(
            VALID.PRINCIPAL.name,
            QueryOrderRequest(VALID.ETH_USDT, null, "shared-client-id")
        )

        assertThat(order).isNotNull
        assertThat(order!!.ouid).isEqualTo("owner-order")
        assertThat(order.status).isEqualTo(OrderStatus.NEW)
    }

    @Test
    fun givenDuplicateClientOrderIdHistoryForUser_whenQueryOrder_thenReturnOpenOrder(): Unit = runBlocking {
        seedOrder(
            VALID.MAKER_ORDER_MODEL.copy(
                id = null,
                ouid = "rejected-duplicate-order",
                clientOrderId = "reused-client-id",
                orderId = 12,
                createDate = VALID.MAKER_ORDER_MODEL.createDate!!.plusSeconds(1),
                updateDate = VALID.MAKER_ORDER_MODEL.updateDate.plusSeconds(1)
            ),
            status = OrderStatus.REJECTED
        )
        seedOrder(
            VALID.MAKER_ORDER_MODEL.copy(
                id = null,
                ouid = "open-order",
                clientOrderId = "reused-client-id",
                orderId = 13
            ),
            status = OrderStatus.NEW
        )

        val order = userQueryHandler.queryOrder(
            VALID.PRINCIPAL.name,
            QueryOrderRequest(VALID.ETH_USDT, null, "reused-client-id")
        )

        assertThat(order).isNotNull
        assertThat(order!!.ouid).isEqualTo("open-order")
        assertThat(order.status).isEqualTo(OrderStatus.NEW)
    }

    @Test
    fun givenMissingLookupIdentifier_whenQueryOrder_thenThrowBadRequest(): Unit = runBlocking {
        assertThatThrownBy {
            runBlocking {
                userQueryHandler.queryOrder(
                    VALID.PRINCIPAL.name,
                    QueryOrderRequest(VALID.ETH_USDT, null, null)
                )
            }
        }.isOpexError(OpexError.BadRequest)
    }

    @Test
    fun givenInvalidLookupIdentifier_whenQueryOrder_thenThrowInvalidRequestParam(): Unit = runBlocking {
        assertThatThrownBy {
            runBlocking {
                userQueryHandler.queryOrder(
                    VALID.PRINCIPAL.name,
                    QueryOrderRequest(VALID.ETH_USDT, 0, null)
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThatThrownBy {
            runBlocking {
                userQueryHandler.queryOrder(
                    VALID.PRINCIPAL.name,
                    QueryOrderRequest(VALID.ETH_USDT, null, " ")
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)
    }

    @Test
    fun givenTrade_whenAllTrades_thenReturnTradesForUser(): Unit = runBlocking {
        seedOrder(status = OrderStatus.FILLED)
        seedOrder(VALID.TAKER_ORDER_MODEL.copy(id = null, clientOrderId = "taker-client"), status = OrderStatus.FILLED)
        val trade = VALID.TRADE_MODEL.copyPrices(
            matchedPrice = BigDecimal.valueOf(90),
            matchedQuantity = BigDecimal.valueOf(0.1),
            takerPrice = BigDecimal.valueOf(95),
            makerPrice = BigDecimal.valueOf(90)
        )
        seedTrade(trade)

        val trades = userQueryHandler.allTrades(VALID.PRINCIPAL.name, TradeRequest(VALID.ETH_USDT, null, null, null, 100))

        assertThat(trades).hasSize(1)
        assertThat(trades.first().id).isEqualTo(trade.tradeId)
        assertThat(trades.first().price).isEqualByComparingTo(trade.matchedPrice)
        assertThat(trades.first().quoteQuantity).isEqualByComparingTo(BigDecimal.valueOf(9.0))
    }

    @Test
    fun givenTradeIdFilter_whenAllTrades_thenReturnTradesFromThatExchangeTradeId(): Unit = runBlocking {
        seedOrder(status = OrderStatus.FILLED)
        seedOrder(VALID.TAKER_ORDER_MODEL.copy(id = null, clientOrderId = "taker-client"), status = OrderStatus.FILLED)
        seedTrade(VALID.TRADE_MODEL.copyTradeId(100))
        seedTrade(VALID.TRADE_MODEL.copyTradeId(101))

        val trades = userQueryHandler.allTrades(VALID.PRINCIPAL.name, TradeRequest(VALID.ETH_USDT, 100, null, null, 100))

        assertThat(trades.map { it.id }).containsExactlyInAnyOrder(100L, 101L)
    }

    @Test
    fun givenTradeBeforeOrdersProjected_whenAllTrades_thenSkipIncompleteProjection(): Unit = runBlocking {
        seedTrade()

        val trades = userQueryHandler.allTrades(VALID.PRINCIPAL.name, TradeRequest(VALID.ETH_USDT, null, null, null, 100))

        assertThat(trades).hasSize(1)
        assertThat(trades.first().orderId).isEqualTo(VALID.TRADE_MODEL.tradeId)
    }

    @Test
    fun givenTakerOrderNotProjected_whenMakerAllTrades_thenReturnMakerTrade(): Unit = runBlocking {
        seedOrder(status = OrderStatus.FILLED)
        seedTrade()

        val trades = userQueryHandler.allTrades(VALID.PRINCIPAL.name, TradeRequest(VALID.ETH_USDT, null, null, null, 100))

        assertThat(trades).hasSize(1)
        assertThat(trades.first().orderId).isEqualTo(VALID.MAKER_ORDER_MODEL.orderId)
        assertThat(trades.first().isMaker).isTrue()
    }

    @Test
    fun givenTakerOrderNotProjected_whenTakerAllTrades_thenReturnTakerTradeWithTradeIdFallback(): Unit = runBlocking {
        val takerUuid = "taker-user"
        seedOrder(status = OrderStatus.FILLED)
        seedTrade(VALID.TRADE_MODEL.copyTaker(takerUuid = takerUuid))

        val trades = userQueryHandler.allTrades(takerUuid, TradeRequest(VALID.ETH_USDT, null, null, null, 100))

        assertThat(trades).hasSize(1)
        assertThat(trades.first().orderId).isEqualTo(VALID.TRADE_MODEL.tradeId)
        assertThat(trades.first().isMaker).isFalse()
        assertThat(trades.first().isBuyer).isTrue()
    }

    private suspend fun seedOrder(
        order: OrderModel = VALID.MAKER_ORDER_MODEL.copy(id = null, clientOrderId = "2"),
        status: OrderStatus,
        open: Boolean = false
    ) {
        val savedOrder = orderRepository.save(order).awaitSingle()
        orderStatusRepository.insert(
            savedOrder.ouid,
            savedOrder.quantity ?: BigDecimal.ZERO,
            savedOrder.quoteQuantity ?: BigDecimal.ZERO,
            status.code,
            status.orderOfAppearance
        ).awaitSingleOrNull()
        if (open) {
            openOrderRepository.insertOrUpdate(savedOrder.ouid, BigDecimal.ZERO, status.code).awaitSingleOrNull()
        }
    }

    private suspend fun seedTrade(trade: TradeModel = VALID.TRADE_MODEL) {
        tradeRepository.save(
            TradeModel(
                null,
                trade.tradeId,
                trade.symbol,
                trade.baseAsset,
                trade.quoteAsset,
                trade.matchedPrice,
                trade.matchedQuantity,
                trade.takerPrice,
                trade.makerPrice,
                trade.takerCommission,
                trade.makerCommission,
                trade.takerCommissionAsset,
                trade.makerCommissionAsset,
                trade.tradeDate,
                trade.makerOuid,
                trade.takerOuid,
                trade.makerUuid,
                trade.takerUuid,
                trade.createDate
            )
        ).awaitSingle()
    }

    private fun TradeModel.copyPrices(
        matchedPrice: BigDecimal,
        matchedQuantity: BigDecimal,
        takerPrice: BigDecimal,
        makerPrice: BigDecimal
    ) = TradeModel(
        id,
        tradeId,
        symbol,
        baseAsset,
        quoteAsset,
        matchedPrice,
        matchedQuantity,
        takerPrice,
        makerPrice,
        takerCommission,
        makerCommission,
        takerCommissionAsset,
        makerCommissionAsset,
        tradeDate,
        makerOuid,
        takerOuid,
        makerUuid,
        takerUuid,
        createDate
    )

    private fun TradeModel.copyTradeId(tradeId: Long) = TradeModel(
        id,
        tradeId,
        symbol,
        baseAsset,
        quoteAsset,
        matchedPrice,
        matchedQuantity,
        takerPrice,
        makerPrice,
        takerCommission,
        makerCommission,
        takerCommissionAsset,
        makerCommissionAsset,
        tradeDate.plusNanos(tradeId),
        makerOuid,
        takerOuid,
        makerUuid,
        takerUuid,
        createDate.plusNanos(tradeId)
    )

    private fun TradeModel.copyTaker(takerUuid: String) = TradeModel(
        id,
        tradeId,
        symbol,
        baseAsset,
        quoteAsset,
        matchedPrice,
        matchedQuantity,
        takerPrice,
        makerPrice,
        takerCommission,
        makerCommission,
        takerCommissionAsset,
        makerCommissionAsset,
        tradeDate,
        makerOuid,
        takerOuid,
        makerUuid,
        takerUuid,
        createDate
    )

    private fun org.assertj.core.api.AbstractThrowableAssert<*, out Throwable>.isOpexError(error: OpexError) {
        isInstanceOf(OpexException::class.java)
            .extracting("error")
            .isEqualTo(error)
    }
}
