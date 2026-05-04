package co.nilin.opex.market.ports.kafka.listener.consumer

import co.nilin.opex.market.core.event.RichOrderEvent
import co.nilin.opex.market.ports.kafka.listener.spi.RichOrderListener
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.util.concurrent.atomic.AtomicInteger

class OrderKafkaListenerTest {

    @Test
    fun givenSameListenerId_whenAddOrderListener_thenReplaceExistingListener() {
        val consumer = OrderKafkaListener()
        val calls = AtomicInteger()

        consumer.addOrderListener(countingListener("listener", calls))
        consumer.addOrderListener(countingListener("listener", calls))
        consumer.onMessage(orderRecord())

        assertEquals(1, consumer.orderListeners.size)
        assertEquals(1, calls.get())
    }

    @Test
    fun givenListenerRemovedDuringDispatch_whenOnMessage_thenDispatchCompletes() {
        val consumer = OrderKafkaListener()
        val calls = AtomicInteger()
        lateinit var removingListener: RichOrderListener
        removingListener = object : RichOrderListener {
            override fun id() = "removing"

            override fun onOrder(order: RichOrderEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
                consumer.removeOrderListener(removingListener)
            }
        }

        consumer.addOrderListener(removingListener)
        consumer.addOrderListener(countingListener("stable", calls))
        consumer.onMessage(orderRecord())

        assertEquals(2, calls.get())
        assertTrue(consumer.orderListeners.none { it.id() == "removing" })
    }

    private fun countingListener(id: String, calls: AtomicInteger): RichOrderListener {
        return object : RichOrderListener {
            override fun id() = id

            override fun onOrder(order: RichOrderEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
            }
        }
    }

    private fun orderRecord(): ConsumerRecord<String, RichOrderEvent> {
        return ConsumerRecord("events_BTC_USDT", 0, 1, "key", object : RichOrderEvent {})
    }
}
