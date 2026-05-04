package co.nilin.opex.wallet.ports.kafka.listener.consumer

import co.nilin.opex.wallet.ports.kafka.listener.model.UserCreatedEvent
import co.nilin.opex.wallet.ports.kafka.listener.spi.UserCreatedEventListener
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.util.concurrent.atomic.AtomicInteger

class UserCreatedKafkaListenerTest {

    @Test
    fun givenSameListenerId_whenAddEventListener_thenReplaceExistingListener() {
        val consumer = UserCreatedKafkaListener()
        val calls = AtomicInteger()

        consumer.addEventListener(countingListener("listener", calls))
        consumer.addEventListener(countingListener("listener", calls))
        consumer.onMessage(userRecord())

        assertEquals(1, consumer.eventListeners.size)
        assertEquals(1, calls.get())
    }

    @Test
    fun givenListenerRemovedDuringDispatch_whenOnMessage_thenDispatchCompletes() {
        val consumer = UserCreatedKafkaListener()
        val calls = AtomicInteger()
        lateinit var removingListener: UserCreatedEventListener
        removingListener = object : UserCreatedEventListener {
            override fun id() = "removing"

            override fun onEvent(event: UserCreatedEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
                consumer.removeEventListener(removingListener)
            }
        }

        consumer.addEventListener(removingListener)
        consumer.addEventListener(countingListener("stable", calls))
        consumer.onMessage(userRecord())

        assertEquals(2, calls.get())
        assertTrue(consumer.eventListeners.none { it.id() == "removing" })
    }

    private fun countingListener(id: String, calls: AtomicInteger): UserCreatedEventListener {
        return object : UserCreatedEventListener {
            override fun id() = id

            override fun onEvent(event: UserCreatedEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
            }
        }
    }

    private fun userRecord(): ConsumerRecord<String, UserCreatedEvent> {
        return ConsumerRecord("user_created", 0, 1, "user", UserCreatedEvent("user", null, null, "user@example.com"))
    }
}
