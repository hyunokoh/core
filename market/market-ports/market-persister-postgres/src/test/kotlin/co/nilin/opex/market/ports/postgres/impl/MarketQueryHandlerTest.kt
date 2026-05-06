package co.nilin.opex.market.ports.postgres.impl

import co.nilin.opex.common.utils.Interval
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
import java.math.BigDecimal
import java.time.LocalDateTime

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
        val trade = VALID.TRADE_MODEL.copyPrices(
            matchedPrice = BigDecimal.valueOf(90),
            matchedQuantity = BigDecimal.valueOf(0.1),
            takerPrice = BigDecimal.valueOf(95),
            makerPrice = BigDecimal.valueOf(90)
        )
        seedTrade(trade)

        val trades = marketQueryHandler.recentTrades(VALID.ETH_USDT, 1)

        assertThat(trades).hasSize(1)
        assertThat(trades.first().id).isEqualTo(trade.tradeId)
        assertThat(trades.first().price).isEqualByComparingTo(trade.matchedPrice)
        assertThat(trades.first().quoteQuantity).isEqualByComparingTo(BigDecimal.valueOf(9.0))
    }

    @Test
    fun givenTrade_whenLastPrice_thenReturnTicker(): Unit = runBlocking {
        seedTrade()

        val prices = marketQueryHandler.lastPrice(VALID.ETH_USDT)

        assertThat(prices).hasSize(1)
        assertThat(prices.first().symbol).isEqualTo(VALID.ETH_USDT)
        assertThat(prices.first().price).isEqualTo(VALID.TRADE_MODEL.matchedPrice.toString())
    }

    @Test
    fun givenMultipleSymbolsAndNewTrade_whenLastPriceRequested_thenReturnFreshSymbolScopedPrices(): Unit = runBlocking {
        val now = LocalDateTime.now()
        seedTrade(
            tradeWith(
                tradeId = 3001,
                symbol = VALID.ETH_USDT,
                matchedPrice = BigDecimal.valueOf(100),
                matchedQuantity = BigDecimal.valueOf(1),
                createDate = now.minusMinutes(3)
            )
        )
        seedTrade(
            tradeWith(
                tradeId = 3002,
                symbol = "BTC_USDT",
                matchedPrice = BigDecimal.valueOf(200),
                matchedQuantity = BigDecimal.valueOf(1),
                createDate = now.minusMinutes(2)
            )
        )

        val ethBeforeNewTrade = marketQueryHandler.lastPrice(VALID.ETH_USDT)

        seedTrade(
            tradeWith(
                tradeId = 3003,
                symbol = VALID.ETH_USDT,
                matchedPrice = BigDecimal.valueOf(101),
                matchedQuantity = BigDecimal.valueOf(1),
                createDate = now.minusMinutes(1)
            )
        )

        val ethAfterNewTrade = marketQueryHandler.lastPrice(VALID.ETH_USDT)
        val allPrices = marketQueryHandler.lastPrice(null)

        assertThat(ethBeforeNewTrade).extracting<String> { it.symbol }.containsExactly(VALID.ETH_USDT)
        assertThat(ethBeforeNewTrade.first().price).isEqualTo("100")
        assertThat(ethAfterNewTrade).extracting<String> { it.symbol }.containsExactly(VALID.ETH_USDT)
        assertThat(ethAfterNewTrade.first().price).isEqualTo("101")
        assertThat(allPrices).extracting<String> { it.symbol }.containsExactlyInAnyOrder(VALID.ETH_USDT, "BTC_USDT")
        assertThat(allPrices.associateBy { it.symbol }[VALID.ETH_USDT]?.price).isEqualTo("101")
        assertThat(allPrices.associateBy { it.symbol }["BTC_USDT"]?.price).isEqualTo("200")
    }

    @Test
    fun givenNewTrade_whenTradeTickerRequestedAgain_thenReturnFreshTickerStats(): Unit = runBlocking {
        val now = LocalDateTime.now()
        seedTrade(
            tradeWith(
                tradeId = 4001,
                symbol = VALID.ETH_USDT,
                matchedPrice = BigDecimal.valueOf(100),
                matchedQuantity = BigDecimal.valueOf(1),
                createDate = now.minusMinutes(3)
            )
        )

        val tickerBeforeNewTrade = marketQueryHandler.getTradeTickerDateBySymbol(
            VALID.ETH_USDT,
            Interval.TwentyFourHours
        )

        seedTrade(
            tradeWith(
                tradeId = 4002,
                symbol = VALID.ETH_USDT,
                matchedPrice = BigDecimal.valueOf(101),
                matchedQuantity = BigDecimal.valueOf(2),
                createDate = now.minusMinutes(1)
            )
        )

        val tickerAfterNewTrade = marketQueryHandler.getTradeTickerDateBySymbol(
            VALID.ETH_USDT,
            Interval.TwentyFourHours
        )
        val allTickers = marketQueryHandler.getTradeTickerData(Interval.TwentyFourHours)

        assertThat(tickerBeforeNewTrade).isNotNull
        assertThat(tickerBeforeNewTrade!!.lastPrice).isEqualByComparingTo(BigDecimal.valueOf(100))
        assertThat(tickerBeforeNewTrade.count).isEqualTo(1)
        assertThat(tickerAfterNewTrade).isNotNull
        assertThat(tickerAfterNewTrade!!.lastPrice).isEqualByComparingTo(BigDecimal.valueOf(101))
        assertThat(tickerAfterNewTrade.lastQty).isEqualByComparingTo(BigDecimal.valueOf(2))
        assertThat(tickerAfterNewTrade.openPrice).isEqualByComparingTo(BigDecimal.valueOf(100))
        assertThat(tickerAfterNewTrade.highPrice).isEqualByComparingTo(BigDecimal.valueOf(101))
        assertThat(tickerAfterNewTrade.lowPrice).isEqualByComparingTo(BigDecimal.valueOf(100))
        assertThat(tickerAfterNewTrade.volume).isEqualByComparingTo(BigDecimal.valueOf(3))
        assertThat(tickerAfterNewTrade.count).isEqualTo(2)
        assertThat(allTickers).hasSize(1)
        assertThat(allTickers.first().lastPrice).isEqualByComparingTo(BigDecimal.valueOf(101))
        assertThat(allTickers.first().count).isEqualTo(2)
    }

    @Test
    fun givenOpenOrdersAndTrades_whenTickerRequested_thenReturnBestPricesAndWeightedAverage(): Unit = runBlocking {
        val symbol = "BEST_USDT"
        val now = LocalDateTime.now()
        seedOrder(
            VALID.MAKER_ORDER_MODEL.copy(
                id = null,
                ouid = "best-bid-old",
                symbol = symbol,
                direction = OrderDirection.BID,
                price = BigDecimal.valueOf(100),
                createDate = now.minusMinutes(5)
            ),
            status = OrderStatus.NEW,
            open = true
        )
        seedOrder(
            VALID.MAKER_ORDER_MODEL.copy(
                id = null,
                ouid = "best-bid-newer-but-worse",
                symbol = symbol,
                direction = OrderDirection.BID,
                price = BigDecimal.valueOf(90),
                createDate = now.minusMinutes(1)
            ),
            status = OrderStatus.NEW,
            open = true
        )
        seedOrder(
            VALID.MAKER_ORDER_MODEL.copy(
                id = null,
                ouid = "best-ask-old-but-worse",
                symbol = symbol,
                direction = OrderDirection.ASK,
                price = BigDecimal.valueOf(120),
                createDate = now.minusMinutes(5)
            ),
            status = OrderStatus.NEW,
            open = true
        )
        seedOrder(
            VALID.MAKER_ORDER_MODEL.copy(
                id = null,
                ouid = "best-ask-newer",
                symbol = symbol,
                direction = OrderDirection.ASK,
                price = BigDecimal.valueOf(110),
                createDate = now.minusMinutes(1)
            ),
            status = OrderStatus.NEW,
            open = true
        )
        seedTrade(
            tradeWith(
                tradeId = 1001,
                symbol = symbol,
                matchedPrice = BigDecimal.valueOf(100),
                matchedQuantity = BigDecimal.valueOf(1),
                createDate = now.minusMinutes(2)
            )
        )
        seedTrade(
            tradeWith(
                tradeId = 1002,
                symbol = symbol,
                matchedPrice = BigDecimal.valueOf(200),
                matchedQuantity = BigDecimal.valueOf(1),
                createDate = now.minusMinutes(1)
            )
        )

        val ticker = marketQueryHandler.getTradeTickerDateBySymbol(symbol, Interval.TwentyFourHours)
        val bestPrices = marketQueryHandler.getBestPriceForSymbols(listOf(symbol))

        assertThat(ticker).isNotNull
        assertThat(ticker!!.bidPrice).isEqualByComparingTo(BigDecimal.valueOf(100))
        assertThat(ticker.askPrice).isEqualByComparingTo(BigDecimal.valueOf(110))
        assertThat(ticker.openPrice).isEqualByComparingTo(BigDecimal.valueOf(100))
        assertThat(ticker.weightedAvgPrice).isEqualByComparingTo(BigDecimal.valueOf(150))
        assertThat(bestPrices).hasSize(1)
        assertThat(bestPrices.first().bidPrice).isEqualByComparingTo(BigDecimal.valueOf(100))
        assertThat(bestPrices.first().askPrice).isEqualByComparingTo(BigDecimal.valueOf(110))
    }

    @Test
    fun givenMultipleSymbols_whenMostVolumeAndMostTradesRequested_thenReturnHighestStats(): Unit = runBlocking {
        val now = LocalDateTime.now()
        seedTrade(
            tradeWith(
                tradeId = 2001,
                symbol = "LOW_VOL_USDT",
                matchedPrice = BigDecimal.valueOf(10),
                matchedQuantity = BigDecimal.valueOf(1),
                createDate = now.minusMinutes(3)
            )
        )
        seedTrade(
            tradeWith(
                tradeId = 2002,
                symbol = "HIGH_VOL_USDT",
                matchedPrice = BigDecimal.valueOf(10),
                matchedQuantity = BigDecimal.valueOf(5),
                createDate = now.minusMinutes(2)
            )
        )
        seedTrade(
            tradeWith(
                tradeId = 2003,
                symbol = "HIGH_TRADES_USDT",
                matchedPrice = BigDecimal.valueOf(10),
                matchedQuantity = BigDecimal.valueOf(1),
                createDate = now.minusMinutes(3)
            )
        )
        seedTrade(
            tradeWith(
                tradeId = 2004,
                symbol = "HIGH_TRADES_USDT",
                matchedPrice = BigDecimal.valueOf(11),
                matchedQuantity = BigDecimal.valueOf(1),
                createDate = now.minusMinutes(2)
            )
        )
        seedTrade(
            tradeWith(
                tradeId = 2005,
                symbol = "HIGH_TRADES_USDT",
                matchedPrice = BigDecimal.valueOf(12),
                matchedQuantity = BigDecimal.valueOf(1),
                createDate = now.minusMinutes(1)
            )
        )

        val mostVolume = marketQueryHandler.mostVolume(Interval.TwentyFourHours)
        val mostTrades = marketQueryHandler.mostTrades(Interval.TwentyFourHours)

        assertThat(mostVolume).isNotNull
        assertThat(mostVolume!!.symbol).isEqualTo("HIGH_VOL_USDT")
        assertThat(mostVolume.volume).isEqualByComparingTo(BigDecimal.valueOf(5))
        assertThat(mostTrades).isNotNull
        assertThat(mostTrades!!.symbol).isEqualTo("HIGH_TRADES_USDT")
        assertThat(mostTrades.tradeCount).isEqualByComparingTo(BigDecimal.valueOf(3))
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

    private fun tradeWith(
        tradeId: Long,
        symbol: String,
        matchedPrice: BigDecimal,
        matchedQuantity: BigDecimal,
        createDate: LocalDateTime
    ) = TradeModel(
        null,
        tradeId,
        symbol,
        VALID.TRADE_MODEL.baseAsset,
        VALID.TRADE_MODEL.quoteAsset,
        matchedPrice,
        matchedQuantity,
        matchedPrice,
        matchedPrice,
        VALID.TRADE_MODEL.takerCommission,
        VALID.TRADE_MODEL.makerCommission,
        VALID.TRADE_MODEL.takerCommissionAsset,
        VALID.TRADE_MODEL.makerCommissionAsset,
        createDate,
        VALID.TRADE_MODEL.makerOuid,
        VALID.TRADE_MODEL.takerOuid,
        VALID.TRADE_MODEL.makerUuid,
        VALID.TRADE_MODEL.takerUuid,
        createDate
    )
}
