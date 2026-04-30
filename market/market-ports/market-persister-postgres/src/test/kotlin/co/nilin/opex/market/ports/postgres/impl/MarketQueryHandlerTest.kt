package co.nilin.opex.market.ports.postgres.impl

import co.nilin.opex.market.core.inout.OrderDirection
import co.nilin.opex.market.core.inout.OrderStatus
import co.nilin.opex.market.ports.postgres.MarketPostgresIntegrationTest
import co.nilin.opex.market.ports.postgres.dao.OpenOrderRepository
import co.nilin.opex.market.ports.postgres.dao.OrderRepository
import co.nilin.opex.market.ports.postgres.dao.OrderStatusRepository
import co.nilin.opex.market.ports.postgres.dao.TradeRepository
import co.nilin.opex.market.ports.postgres.impl.sample.VALID
import co.nilin.opex.market.ports.postgres.model.TradeModel
import co.nilin.opex.market.ports.postgres.util.RedisCacheHelper
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.reactor.awaitSingleOrNull
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.r2dbc.core.DatabaseClient

private class MarketQueryHandlerTest : MarketPostgresIntegrationTest() {
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

    @Autowired
    private lateinit var redisCacheHelper: RedisCacheHelper

    private val marketQueryHandler by lazy {
        MarketQueryHandlerImpl(orderRepository, tradeRepository, orderStatusRepository, redisCacheHelper)
    }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        databaseClient.executeSql("truncate table trades, open_orders, order_status, orders, currency_rate restart identity cascade")
    }

    @Test
    fun givenOpenAskOrders_whenOpenAskOrders_thenReturnAggregatedOrderBook(): Unit = runBlocking {
        seedOrder(status = OrderStatus.NEW, open = true)

        val orderBook = marketQueryHandler.openAskOrders(VALID.ETH_USDT, 1)

        assertThat(orderBook).hasSize(1)
        assertThat(orderBook.first().price).isEqualByComparingTo(VALID.MAKER_ORDER_MODEL.price)
        assertThat(orderBook.first().quantity).isEqualByComparingTo(VALID.MAKER_ORDER_MODEL.quantity)
    }

    @Test
    fun givenOpenBidOrders_whenOpenBidOrders_thenReturnAggregatedOrderBook(): Unit = runBlocking {
        seedOrder(
            VALID.MAKER_ORDER_MODEL.copy(
                id = null,
                ouid = "bid-${VALID.MAKER_ORDER_MODEL.ouid}",
                direction = OrderDirection.BID
            ),
            status = OrderStatus.NEW,
            open = true
        )

        val orderBook = marketQueryHandler.openBidOrders(VALID.ETH_USDT, 1)

        assertThat(orderBook).hasSize(1)
        assertThat(orderBook.first().price).isEqualByComparingTo(VALID.MAKER_ORDER_MODEL.price)
        assertThat(orderBook.first().quantity).isEqualByComparingTo(VALID.MAKER_ORDER_MODEL.quantity)
    }

    @Test
    fun givenOrder_whenLastOrder_thenReturnOrderWithLatestStatus(): Unit = runBlocking {
        seedOrder(status = OrderStatus.FILLED)

        val order = marketQueryHandler.lastOrder(VALID.ETH_USDT)

        assertThat(order).isNotNull
        assertThat(order!!.ouid).isEqualTo(VALID.MAKER_ORDER_MODEL.ouid)
        assertThat(order.status).isEqualTo(OrderStatus.FILLED)
    }

    @Test
    fun givenTradeAndOrders_whenRecentTrades_thenReturnRecentMarketTrade(): Unit = runBlocking {
        seedOrder(status = OrderStatus.FILLED)
        seedOrder(VALID.TAKER_ORDER_MODEL.copy(id = null, clientOrderId = "taker-client"), status = OrderStatus.FILLED)
        seedTrade()

        val trades = marketQueryHandler.recentTrades(VALID.ETH_USDT, 1)

        assertThat(trades).hasSize(1)
        assertThat(trades.first().id).isEqualTo(VALID.TRADE_MODEL.tradeId)
        assertThat(trades.first().price).isEqualByComparingTo(VALID.TRADE_MODEL.matchedPrice)
    }

    @Test
    fun givenTrade_whenLastPrice_thenReturnTicker(): Unit = runBlocking {
        seedTrade()

        val prices = marketQueryHandler.lastPrice(VALID.ETH_USDT)

        assertThat(prices).hasSize(1)
        assertThat(prices.first().symbol).isEqualTo(VALID.ETH_USDT)
        assertThat(prices.first().price).isEqualTo(VALID.TRADE_MODEL.matchedPrice.toString())
    }

    private suspend fun seedOrder(
        order: co.nilin.opex.market.ports.postgres.model.OrderModel = VALID.MAKER_ORDER_MODEL.copy(
            id = null,
            clientOrderId = "2"
        ),
        status: OrderStatus,
        open: Boolean = false
    ) {
        val savedOrder = orderRepository.save(order).awaitSingle()
        orderStatusRepository.insert(
            savedOrder.ouid,
            savedOrder.quantity ?: java.math.BigDecimal.ZERO,
            savedOrder.quoteQuantity ?: java.math.BigDecimal.ZERO,
            status.code,
            status.orderOfAppearance
        ).awaitSingleOrNull()
        if (open) {
            openOrderRepository.insertOrUpdate(savedOrder.ouid, java.math.BigDecimal.ZERO, status.code).awaitSingleOrNull()
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
