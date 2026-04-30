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
        seedTrade()

        val trades = userQueryHandler.allTrades(VALID.PRINCIPAL.name, TradeRequest(VALID.ETH_USDT, null, null, null, 100))

        assertThat(trades).hasSize(1)
        assertThat(trades.first().id).isEqualTo(VALID.TRADE_MODEL.tradeId)
        assertThat(trades.first().price).isEqualByComparingTo(VALID.TRADE_MODEL.takerPrice)
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

    private suspend fun seedTrade() {
        tradeRepository.save(
            TradeModel(
                null,
                VALID.TRADE_MODEL.tradeId,
                VALID.TRADE_MODEL.symbol,
                VALID.TRADE_MODEL.baseAsset,
                VALID.TRADE_MODEL.quoteAsset,
                VALID.TRADE_MODEL.matchedPrice,
                VALID.TRADE_MODEL.matchedQuantity,
                VALID.TRADE_MODEL.takerPrice,
                VALID.TRADE_MODEL.makerPrice,
                VALID.TRADE_MODEL.takerCommission,
                VALID.TRADE_MODEL.makerCommission,
                VALID.TRADE_MODEL.takerCommissionAsset,
                VALID.TRADE_MODEL.makerCommissionAsset,
                VALID.TRADE_MODEL.tradeDate,
                VALID.TRADE_MODEL.makerOuid,
                VALID.TRADE_MODEL.takerOuid,
                VALID.TRADE_MODEL.makerUuid,
                VALID.TRADE_MODEL.takerUuid,
                VALID.TRADE_MODEL.createDate
            )
        ).awaitSingle()
    }
}
