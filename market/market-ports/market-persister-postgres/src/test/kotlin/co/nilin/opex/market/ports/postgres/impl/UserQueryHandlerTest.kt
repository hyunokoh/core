package co.nilin.opex.market.ports.postgres.impl

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
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.reactor.awaitSingleOrNull
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
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
    fun givenTradeBeforeOrdersProjected_whenAllTrades_thenSkipIncompleteProjection(): Unit = runBlocking {
        seedTrade()

        val trades = userQueryHandler.allTrades(VALID.PRINCIPAL.name, TradeRequest(VALID.ETH_USDT, null, null, null, 100))

        assertThat(trades).isEmpty()
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
}
