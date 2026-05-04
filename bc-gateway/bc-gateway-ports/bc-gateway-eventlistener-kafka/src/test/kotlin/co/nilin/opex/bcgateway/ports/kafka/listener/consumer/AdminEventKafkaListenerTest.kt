package co.nilin.opex.bcgateway.ports.kafka.listener.consumer

import co.nilin.opex.bcgateway.ports.kafka.listener.model.AdminEvent
import co.nilin.opex.bcgateway.ports.kafka.listener.model.DeleteCurrencyEvent
import co.nilin.opex.bcgateway.ports.kafka.listener.spi.AdminEventListener
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import java.util.concurrent.atomic.AtomicInteger

class AdminEventKafkaListenerTest {

    @Test
    fun givenSameListenerId_whenAddEventListener_thenOnlyOneListenerIsInvoked() {
        val consumer = AdminEventKafkaListener()
        val calls = AtomicInteger()

        consumer.addEventListener(countingListener("listener", calls))
        consumer.addEventListener(countingListener("listener", calls))
        consumer.onMessage(adminRecord())

        assertEquals(1, calls.get())
    }

    @Test
    fun givenListenerRemovedDuringDispatch_whenOnMessage_thenDispatchCompletes() {
        val consumer = AdminEventKafkaListener()
        val calls = AtomicInteger()
        lateinit var removingListener: AdminEventListener
        removingListener = object : AdminEventListener {
            override fun id() = "removing"

            override fun onEvent(event: AdminEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
                consumer.removeEventListener(removingListener)
            }
        }

        consumer.addEventListener(removingListener)
        consumer.addEventListener(countingListener("stable", calls))
        consumer.onMessage(adminRecord())

        assertEquals(2, calls.get())
    }

    private fun countingListener(id: String, calls: AtomicInteger): AdminEventListener {
        return object : AdminEventListener {
            override fun id() = id

            override fun onEvent(event: AdminEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
            }
        }
    }

    private fun adminRecord(): ConsumerRecord<String?, AdminEvent> {
        return ConsumerRecord("admin", 0, 1, "admin", DeleteCurrencyEvent("BTC"))
    }
}
