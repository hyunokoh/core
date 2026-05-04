package co.nilin.opex.matching.engine.app.bl

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertSame
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class OrderBooksTest {

    @BeforeEach
    fun setUp() {
        OrderBooks.clearForTest()
    }

    @Test
    fun givenPair_whenCreateOrderBook_thenLookupUsesNormalizedPair() {
        OrderBooks.createOrderBook(" eth_usdt ")

        assertSame(
            OrderBooks.lookupOrderBook("ETH_USDT"),
            OrderBooks.lookupOrderBook("eth_usdt")
        )
    }

    @Test
    fun givenDuplicatePair_whenCreateOrderBook_thenThrow() {
        OrderBooks.createOrderBook("BTC_USDT")

        assertThrows(IllegalArgumentException::class.java) {
            OrderBooks.createOrderBook("btc_usdt")
        }
    }

    @Test
    fun givenMalformedPair_whenCreateOrderBook_thenThrow() {
        listOf("", " ", "ETHUSDT", "_USDT", "ETH_", "ETH_USDT_SPOT").forEach { pair ->
            assertThrows(IllegalArgumentException::class.java) {
                OrderBooks.createOrderBook(pair)
            }
        }
    }

    @Test
    fun givenConcurrentCreateSamePair_whenCreateOrderBook_thenOnlyOneSucceeds() {
        val workers = 16
        val executor = Executors.newFixedThreadPool(8)
        val start = CountDownLatch(1)
        val done = CountDownLatch(workers)
        val successes = AtomicInteger()
        val failures = AtomicInteger()

        try {
            repeat(workers) {
                executor.execute {
                    start.await()
                    try {
                        OrderBooks.createOrderBook("BNB_USDT")
                        successes.incrementAndGet()
                    } catch (e: IllegalArgumentException) {
                        failures.incrementAndGet()
                    } finally {
                        done.countDown()
                    }
                }
            }

            start.countDown()

            assertTrue(done.await(5, TimeUnit.SECONDS))
            assertEquals(1, successes.get())
            assertEquals(workers - 1, failures.get())
            assertNotNull(OrderBooks.lookupOrderBook("BNB_USDT"))
        } finally {
            executor.shutdownNow()
        }
    }
}
