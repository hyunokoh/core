package co.nilin.opex.matching.engine.app.bl

import co.nilin.opex.matching.engine.core.model.PersistentOrderBook
import co.nilin.opex.matching.engine.core.spi.OrderBookPersister
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test

class OrderBookBootstrapperTest {

    @BeforeEach
    fun setUp() {
        OrderBooks.clearForTest()
    }

    @Test
    fun givenOneSymbolLoadFails_whenBootstrap_thenOtherSymbolsStillCreateOrderBooks() = runBlocking {
        val persister = FailingSymbolOrderBookPersister("BTC_USDT")
        val bootstrapper = OrderBookBootstrapper(
            listOf("BTC_USDT", "ETH_USDT"),
            persister
        )

        bootstrapper.bootstrap().joinAll()

        assertThrows(IllegalArgumentException::class.java) {
            OrderBooks.lookupOrderBook("BTC_USDT")
        }
        assertNotNull(OrderBooks.lookupOrderBook("ETH_USDT"))
    }

    private class FailingSymbolOrderBookPersister(
        private val failingSymbol: String
    ) : OrderBookPersister {
        override suspend fun storeLastState(orderBook: PersistentOrderBook) {
        }

        override suspend fun loadLastState(symbol: String): PersistentOrderBook? {
            if (symbol == failingSymbol) {
                throw IllegalStateException("Failed to load order book")
            }
            return null
        }
    }
}
