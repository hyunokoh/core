package co.nilin.opex.matching.engine.ports.kafka.listener.consumer

import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import co.nilin.opex.matching.engine.core.model.Pair
import co.nilin.opex.matching.engine.ports.kafka.listener.spi.EventListener
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.util.concurrent.atomic.AtomicInteger

class EventKafkaListenerTest {

    @Test
    fun givenSameListenerId_whenAddEventListener_thenReplaceExistingListener() {
        val consumer = EventKafkaListener()
        val calls = AtomicInteger()

        consumer.addEventListener(countingEventListener("listener", calls))
        consumer.addEventListener(countingEventListener("listener", calls))
        consumer.onMessage(eventRecord())

        assertEquals(1, consumer.eventListeners.size)
        assertEquals(1, calls.get())
    }

    @Test
    fun givenListenerRemovedDuringDispatch_whenOnMessage_thenDispatchCompletes() {
        val consumer = EventKafkaListener()
        val calls = AtomicInteger()
        lateinit var removingListener: EventListener
        removingListener = object : EventListener {
            override fun id() = "removing"

            override fun onEvent(event: CoreEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
                consumer.removeEventListener(removingListener)
            }
        }

        consumer.addEventListener(removingListener)
        consumer.addEventListener(countingEventListener("stable", calls))

        consumer.onMessage(eventRecord())

        assertEquals(2, calls.get())
        assertTrue(consumer.eventListeners.none { it.id() == "removing" })
    }

    private fun countingEventListener(id: String, calls: AtomicInteger): EventListener {
        return object : EventListener {
            override fun id() = id

            override fun onEvent(event: CoreEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
            }
        }
    }

    private fun eventRecord(): ConsumerRecord<String, CoreEvent> {
        return ConsumerRecord("events_BTC_USDT", 0, 1, "key", CoreEvent(Pair("BTC", "USDT")))
    }
}
