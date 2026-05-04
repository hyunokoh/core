package co.nilin.opex.matching.engine.ports.kafka.listener.consumer

import co.nilin.opex.matching.engine.core.inout.OrderCancelRequestEvent
import co.nilin.opex.matching.engine.core.inout.OrderRequestEvent
import co.nilin.opex.matching.engine.core.model.Pair
import co.nilin.opex.matching.engine.ports.kafka.listener.spi.OrderRequestEventListener
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

        consumer.addOrderListener(countingOrderListener("listener", calls))
        consumer.addOrderListener(countingOrderListener("listener", calls))
        consumer.onMessage(orderRecord())

        assertEquals(1, consumer.orderListeners.size)
        assertEquals(1, calls.get())
    }

    @Test
    fun givenListenerRemovedDuringDispatch_whenOnMessage_thenDispatchCompletes() {
        val consumer = OrderKafkaListener()
        val calls = AtomicInteger()
        lateinit var removingListener: OrderRequestEventListener
        removingListener = object : OrderRequestEventListener {
            override fun id() = "removing"

            override suspend fun onOrder(order: OrderRequestEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
                consumer.removeOrderListener(removingListener)
            }
        }

        consumer.addOrderListener(removingListener)
        consumer.addOrderListener(countingOrderListener("stable", calls))

        consumer.onMessage(orderRecord())

        assertEquals(2, calls.get())
        assertTrue(consumer.orderListeners.none { it.id() == "removing" })
    }

    private fun countingOrderListener(id: String, calls: AtomicInteger): OrderRequestEventListener {
        return object : OrderRequestEventListener {
            override fun id() = id

            override suspend fun onOrder(order: OrderRequestEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
            }
        }
    }

    private fun orderRecord(): ConsumerRecord<String, OrderRequestEvent> {
        return ConsumerRecord(
            "orders_BTC_USDT",
            0,
            1,
            "key",
            OrderCancelRequestEvent("ouid", "uuid", Pair("BTC", "USDT"), 10)
        )
    }
}
