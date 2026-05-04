package co.nilin.opex.wallet.ports.kafka.listener.consumer

import co.nilin.opex.wallet.ports.kafka.listener.model.FinancialActionEvent
import co.nilin.opex.wallet.ports.kafka.listener.spi.FinancialActionEventListener
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.util.concurrent.atomic.AtomicInteger

class FinancialActionKafkaListenerTest {

    @Test
    fun givenSameListenerId_whenAddEventListener_thenReplaceExistingListener() {
        val consumer = FinancialActionKafkaListener()
        val calls = AtomicInteger()

        consumer.addEventListener(countingListener("listener", calls))
        consumer.addEventListener(countingListener("listener", calls))
        consumer.onMessage(actionRecord())

        assertEquals(1, consumer.eventListeners.size)
        assertEquals(1, calls.get())
    }

    @Test
    fun givenListenerRemovedDuringDispatch_whenOnMessage_thenDispatchCompletes() {
        val consumer = FinancialActionKafkaListener()
        val calls = AtomicInteger()
        lateinit var removingListener: FinancialActionEventListener
        removingListener = object : FinancialActionEventListener {
            override fun id() = "removing"

            override fun onEvent(event: FinancialActionEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
                consumer.removeEventListener(removingListener)
            }
        }

        consumer.addEventListener(removingListener)
        consumer.addEventListener(countingListener("stable", calls))
        consumer.onMessage(actionRecord())

        assertEquals(2, calls.get())
        assertTrue(consumer.eventListeners.none { it.id() == "removing" })
    }

    private fun countingListener(id: String, calls: AtomicInteger): FinancialActionEventListener {
        return object : FinancialActionEventListener {
            override fun id() = id

            override fun onEvent(event: FinancialActionEvent, partition: Int, offset: Long, timestamp: Long) {
                calls.incrementAndGet()
            }
        }
    }

    private fun actionRecord(): ConsumerRecord<String, FinancialActionEvent> {
        return ConsumerRecord("financial_action", 0, 1, "action", FinancialActionEvent())
    }
}
