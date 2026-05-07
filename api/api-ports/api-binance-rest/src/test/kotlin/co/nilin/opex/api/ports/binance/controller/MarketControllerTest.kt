package co.nilin.opex.api.ports.binance.controller

import co.nilin.opex.api.core.inout.*
import co.nilin.opex.api.core.spi.AccountantProxy
import co.nilin.opex.api.core.spi.BlockchainGatewayProxy
import co.nilin.opex.api.core.spi.MarketDataProxy
import co.nilin.opex.api.core.spi.SymbolMapper
import co.nilin.opex.common.OpexError
import co.nilin.opex.common.utils.Interval
import co.nilin.opex.utility.error.data.OpexException
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.Test
import java.math.BigDecimal
import java.security.Principal
import java.time.LocalDateTime
import java.time.ZoneId

private class MarketControllerTest {

    @Test
    fun givenInvalidDepthLimit_whenOrderBookRequested_thenThrowInvalidLimitBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.orderBook("ETHUSDT", 7) }
        }.isOpexError(OpexError.InvalidLimitForOrderBook)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenInvalidTradesLimit_whenRecentTradesRequested_thenThrowInvalidLimitBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.recentTrades(Principal { "public" }, "ETHUSDT", 1001) }
        }.isOpexError(OpexError.InvalidLimitForRecentTrades)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenBlankSymbol_whenOrderBookRequested_thenThrowInvalidParamBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.orderBook(" ", null) }
        }.isOpexError(OpexError.InvalidRequestParam)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenBlankSymbol_whenRecentTradesRequested_thenThrowInvalidParamBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.recentTrades(Principal { "public" }, " ", null) }
        }.isOpexError(OpexError.InvalidRequestParam)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenDuplicatePriceOrders_whenOrderBookRequested_thenAggregateDepthLevels(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy(
            bidOrders = listOf(
                OrderBook(BigDecimal("100"), BigDecimal("0.1")),
                OrderBook(BigDecimal("100"), BigDecimal("0.2")),
                OrderBook(BigDecimal("99"), BigDecimal("0.3"))
            ),
            askOrders = listOf(
                OrderBook(BigDecimal("101"), BigDecimal("0.4")),
                OrderBook(BigDecimal("101"), BigDecimal("0.5")),
                OrderBook(BigDecimal("102"), BigDecimal("0.6"))
            )
        )
        val controller = controller(marketDataProxy)

        val response = controller.orderBook("ETHUSDT", 5)

        assertThat(response.bids).containsExactly(
            listOf(BigDecimal("100"), BigDecimal("0.3")),
            listOf(BigDecimal("99"), BigDecimal("0.3"))
        )
        assertThat(response.asks).containsExactly(
            listOf(BigDecimal("101"), BigDecimal("0.9")),
            listOf(BigDecimal("102"), BigDecimal("0.6"))
        )
        assertThat(marketDataProxy.openBidOrdersLimit).isEqualTo(5000)
        assertThat(marketDataProxy.openAskOrdersLimit).isEqualTo(5000)
    }

    @Test
    fun givenMoreDepthLevelsThanLimit_whenOrderBookRequested_thenLimitAggregatedLevels(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy(
            bidOrders = listOf(
                OrderBook(BigDecimal("100"), BigDecimal("0.1")),
                OrderBook(BigDecimal("100"), BigDecimal("0.2")),
                OrderBook(BigDecimal("99"), BigDecimal("0.3")),
                OrderBook(BigDecimal("98"), BigDecimal("0.4")),
                OrderBook(BigDecimal("97"), BigDecimal("0.5")),
                OrderBook(BigDecimal("96"), BigDecimal("0.6")),
                OrderBook(BigDecimal("95"), BigDecimal("0.7"))
            ),
            askOrders = listOf(
                OrderBook(BigDecimal("101"), BigDecimal("0.4")),
                OrderBook(BigDecimal("101"), BigDecimal("0.5")),
                OrderBook(BigDecimal("102"), BigDecimal("0.6")),
                OrderBook(BigDecimal("103"), BigDecimal("0.7")),
                OrderBook(BigDecimal("104"), BigDecimal("0.8")),
                OrderBook(BigDecimal("105"), BigDecimal("0.9")),
                OrderBook(BigDecimal("106"), BigDecimal("1.0"))
            )
        )
        val controller = controller(marketDataProxy)

        val response = controller.orderBook("ETHUSDT", 5)

        assertThat(response.bids).hasSize(5)
        assertThat(response.asks).hasSize(5)
        assertThat(response.bids.last()).isEqualTo(listOf(BigDecimal("96"), BigDecimal("0.6")))
        assertThat(response.asks.last()).isEqualTo(listOf(BigDecimal("105"), BigDecimal("0.9")))
    }

    @Test
    fun givenInvalidTickerDuration_whenPriceChangeRequested_thenThrowInvalidDurationBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.priceChange("2h", "ETHUSDT", null) }
        }.isOpexError(OpexError.InvalidPriceChangeDuration)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenBlankSymbol_whenPriceChangeRequested_thenThrowInvalidParamBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.priceChange("24h", " ", null) }
        }.isOpexError(OpexError.InvalidRequestParam)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenBlankSymbol_whenPriceTickerRequested_thenThrowInvalidParamBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.priceTicker(" ") }
        }.isOpexError(OpexError.InvalidRequestParam)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenInvertedTimeRange_whenKlinesRequested_thenThrowInvalidParamBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.klines("ETHUSDT", "1m", 2000, 1000, null) }
        }.isOpexError(OpexError.InvalidRequestParam)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenNegativeStartTime_whenKlinesRequested_thenThrowInvalidParamBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.klines("ETHUSDT", "1m", -1, null, null) }
        }.isOpexError(OpexError.InvalidRequestParam)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenBlankSymbol_whenKlinesRequested_thenThrowInvalidParamBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.klines(" ", "1m", null, null, null) }
        }.isOpexError(OpexError.InvalidRequestParam)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenCandle_whenKlinesRequested_thenReturnBinanceInclusiveCloseTime(): Unit = runBlocking {
        val openTime = LocalDateTime.of(2026, 1, 1, 0, 0)
        val closeTime = openTime.plusMinutes(1)
        val marketDataProxy = RecordingMarketDataProxy(
            candles = listOf(
                CandleData(
                    openTime,
                    closeTime,
                    BigDecimal("100"),
                    BigDecimal("101"),
                    BigDecimal("102"),
                    BigDecimal("99"),
                    BigDecimal("0.5"),
                    BigDecimal("50.5"),
                    3,
                    BigDecimal("0.2"),
                    BigDecimal("20.2")
                )
            )
        )
        val controller = controller(marketDataProxy)

        val response = controller.klines("ETHUSDT", "1m", null, null, 1)

        assertThat(response).hasSize(1)
        assertThat(response.first()[0]).isEqualTo(openTime.toEpochMillis())
        assertThat(response.first()[6]).isEqualTo(closeTime.toEpochMillis() - 1)
    }

    @Test
    fun givenSymbol_whenExchangeInfoRequested_thenReturnOnlyRequestedSymbol(): Unit = runBlocking {
        val controller = controller(RecordingMarketDataProxy())

        val response = controller.pairInfo("ETHUSDT", null)

        assertThat(response.symbols.map { it.symbol }).containsExactly("ETHUSDT")
    }

    @Test
    fun givenSymbols_whenExchangeInfoRequested_thenReturnOnlyRequestedSymbols(): Unit = runBlocking {
        val controller = controller(RecordingMarketDataProxy())

        val response = controller.pairInfo(null, "[\"ETHUSDT\",\"BTCUSDT\"]")

        assertThat(response.symbols.map { it.symbol }).containsExactlyInAnyOrder("ETHUSDT", "BTCUSDT")
    }

    @Test
    fun givenSymbolAndSymbols_whenExchangeInfoRequested_thenRejectRequest(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.pairInfo("ETHUSDT", "[\"BTCUSDT\"]") }
        }.isOpexError(OpexError.BadRequest)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenBlankSymbol_whenExchangeInfoRequested_thenRejectRequest(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.pairInfo(" ", null) }
        }.isOpexError(OpexError.InvalidRequestParam)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenUnknownSymbolsEntry_whenExchangeInfoRequested_thenRejectRequest(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.pairInfo(null, "[\"ETHUSDT\",\"UNKNOWN\"]") }
        }.isOpexError(OpexError.SymbolNotFound)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenNonArraySymbols_whenExchangeInfoRequested_thenRejectRequest(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.pairInfo(null, "\"ETHUSDT\"") }
        }.isOpexError(OpexError.InvalidRequestParam)

        marketDataProxy.assertNotCalled()
    }

    @Test
    fun givenUnquotedSymbolsEntry_whenExchangeInfoRequested_thenRejectRequest(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.pairInfo(null, "[ETHUSDT]") }
        }.isOpexError(OpexError.InvalidRequestParam)

        marketDataProxy.assertNotCalled()
    }

    private fun controller(marketDataProxy: RecordingMarketDataProxy) = MarketController(
        RecordingAccountantProxy(),
        marketDataProxy,
        RecordingBlockchainGatewayProxy(),
        RecordingSymbolMapper()
    )

    private fun org.assertj.core.api.AbstractThrowableAssert<*, out Throwable>.isOpexError(error: OpexError) {
        isInstanceOf(OpexException::class.java)
            .extracting("error")
            .isEqualTo(error)
    }

    private class RecordingMarketDataProxy(
        private val bidOrders: List<OrderBook> = emptyList(),
        private val askOrders: List<OrderBook> = emptyList(),
        private val candles: List<CandleData> = emptyList()
    ) : MarketDataProxy {
        private var callCount = 0
        var openBidOrdersLimit: Int? = null
            private set
        var openAskOrdersLimit: Int? = null
            private set

        fun assertNotCalled() {
            assertThat(callCount).isZero()
        }

        private fun called() {
            callCount++
        }

        override suspend fun getTradeTickerData(interval: Interval): List<PriceChange> {
            called()
            return emptyList()
        }

        override suspend fun getTradeTickerDataBySymbol(symbol: String, interval: Interval): PriceChange {
            called()
            return PriceChange(symbol)
        }

        override suspend fun openBidOrders(symbol: String, limit: Int): List<OrderBook> {
            called()
            openBidOrdersLimit = limit
            return bidOrders
        }

        override suspend fun openAskOrders(symbol: String, limit: Int): List<OrderBook> {
            called()
            openAskOrdersLimit = limit
            return askOrders
        }

        override suspend fun lastOrder(symbol: String): Order? {
            called()
            return null
        }

        override suspend fun recentTrades(symbol: String, limit: Int): List<MarketTrade> {
            called()
            return emptyList()
        }

        override suspend fun lastPrice(symbol: String?): List<PriceTicker> {
            called()
            return emptyList()
        }

        override suspend fun getBestPriceForSymbols(symbols: List<String>): List<BestPrice> {
            called()
            return emptyList()
        }

        override suspend fun getCandleInfo(
            symbol: String,
            interval: String,
            startTime: Long?,
            endTime: Long?,
            limit: Int
        ): List<CandleData> {
            called()
            return candles
        }

        override suspend fun getMarketCurrencyRates(quote: String, base: String?): List<CurrencyRate> {
            called()
            return emptyList()
        }

        override suspend fun getExternalCurrencyRates(quote: String, base: String?): List<CurrencyRate> {
            called()
            return emptyList()
        }

        override suspend fun countActiveUsers(interval: Interval): Long {
            called()
            return 0
        }

        override suspend fun countTotalOrders(interval: Interval): Long {
            called()
            return 0
        }

        override suspend fun countTotalTrades(interval: Interval): Long {
            called()
            return 0
        }
    }

    private class RecordingSymbolMapper : SymbolMapper {
        override suspend fun fromInternalSymbol(symbol: String?): String? = symbol

        override suspend fun toInternalSymbol(alias: String?): String? =
            when (alias) {
                "ETHUSDT" -> "ETH_USDT"
                "BTCUSDT" -> "BTC_USDT"
                else -> null
            }

        override suspend fun symbolToAliasMap(): Map<String, String> = mapOf(
            "ETH_USDT" to "ETHUSDT",
            "BTC_USDT" to "BTCUSDT"
        )
    }

    private class RecordingAccountantProxy : AccountantProxy {
        override suspend fun getPairConfigs(): List<PairInfoResponse> = listOf(
            PairInfoResponse("ETH_USDT", "ETH", "USDT", BigDecimal("0.00000001"), BigDecimal("0.00000001")),
            PairInfoResponse("BTC_USDT", "BTC", "USDT", BigDecimal("0.00000001"), BigDecimal("0.00000001"))
        )

        override suspend fun getFeeConfigs(): List<PairFeeResponse> = emptyList()

        override suspend fun getFeeConfig(symbol: String): PairFeeResponse {
            throw UnsupportedOperationException("Not used by this test")
        }
    }

    private class RecordingBlockchainGatewayProxy : BlockchainGatewayProxy {
        override suspend fun assignAddress(uuid: String, currency: String, chain: String): AssignResponse? = null

        override suspend fun getDepositDetails(refs: List<String>): List<DepositDetails> = emptyList()

        override suspend fun getCurrencyImplementations(currency: String?): List<CurrencyImplementation> = emptyList()
    }

    private fun LocalDateTime.toEpochMillis(): Long =
        atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
}
