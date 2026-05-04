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
    fun givenInvalidTickerDuration_whenPriceChangeRequested_thenThrowInvalidDurationBeforeProxyCall(): Unit = runBlocking {
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(marketDataProxy)

        assertThatThrownBy {
            runBlocking { controller.priceChange("2h", "ETHUSDT", null) }
        }.isOpexError(OpexError.InvalidPriceChangeDuration)

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

    private class RecordingMarketDataProxy : MarketDataProxy {
        private var callCount = 0

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
            return emptyList()
        }

        override suspend fun openAskOrders(symbol: String, limit: Int): List<OrderBook> {
            called()
            return emptyList()
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
            return emptyList()
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
}
