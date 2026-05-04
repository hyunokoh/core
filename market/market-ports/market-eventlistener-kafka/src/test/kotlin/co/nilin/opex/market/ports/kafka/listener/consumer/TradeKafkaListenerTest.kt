package co.nilin.opex.market.ports.kafka.listener.consumer

import co.nilin.opex.market.core.event.RichTrade
import co.nilin.opex.market.core.inout.OrderDirection
import co.nilin.opex.market.ports.kafka.listener.spi.RichTradeListener
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.math.BigDecimal
import java.time.LocalDateTime
import java.util.concurrent.atomic.AtomicInteger

class TradeKafkaListenerTest {

    @Test
    fun givenSameListenerId_whenAddTradeListener_thenReplaceExistingListener() {
        val consumer = TradeKafkaListener()
        val calls = AtomicInteger()

        consumer.addTradeListener(countingListener("listener", calls))
        consumer.addTradeListener(countingListener("listener", calls))
        consumer.onMessage(tradeRecord())

        assertEquals(1, consumer.tradeListeners.size)
        assertEquals(1, calls.get())
    }

    @Test
    fun givenListenerRemovedDuringDispatch_whenOnMessage_thenDispatchCompletes() {
        val consumer = TradeKafkaListener()
        val calls = AtomicInteger()
        lateinit var removingListener: RichTradeListener
        removingListener = object : RichTradeListener {
            override fun id() = "removing"

            override fun onTrade(trade: RichTrade, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
                consumer.removeTradeListener(removingListener)
            }
        }

        consumer.addTradeListener(removingListener)
        consumer.addTradeListener(countingListener("stable", calls))
        consumer.onMessage(tradeRecord())

        assertEquals(2, calls.get())
        assertTrue(consumer.tradeListeners.none { it.id() == "removing" })
    }

    private fun countingListener(id: String, calls: AtomicInteger): RichTradeListener {
        return object : RichTradeListener {
            override fun id() = id

            override fun onTrade(trade: RichTrade, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
            }
        }
    }

    private fun tradeRecord(): ConsumerRecord<String, RichTrade> {
        return ConsumerRecord("trades_BTC_USDT", 0, 1, "key", trade())
    }

    private fun trade(): RichTrade {
        return RichTrade(
            1,
            "BTC_USDT",
            "taker-ouid",
            "taker-uuid",
            10,
            OrderDirection.BID,
            BigDecimal.ONE,
            BigDecimal.ONE,
            BigDecimal.ONE,
            BigDecimal.ZERO,
            BigDecimal.ZERO,
            "USDT",
            "maker-ouid",
            "maker-uuid",
            11,
            OrderDirection.ASK,
            BigDecimal.ONE,
            BigDecimal.ONE,
            BigDecimal.ONE,
            BigDecimal.ZERO,
            BigDecimal.ZERO,
            "USDT",
            BigDecimal.ONE,
            BigDecimal.ONE,
            LocalDateTime.now()
        )
    }
}
