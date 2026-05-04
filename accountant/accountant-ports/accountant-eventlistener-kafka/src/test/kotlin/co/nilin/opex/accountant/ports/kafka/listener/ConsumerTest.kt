package co.nilin.opex.accountant.ports.kafka.listener

import co.nilin.opex.accountant.ports.kafka.listener.consumer.ConsumerObject
import co.nilin.opex.accountant.ports.kafka.listener.consumer.ListenerObject
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test

class ConsumerTest {

    private val consumer = ConsumerObject()
    private val listener = ListenerObject()

    @Test
    fun givenEventConsumer_onMessage_callListener() {
        consumer.addListener(listener)
        consumer.onMessage(ConsumerRecord("topic", 1, 0, null, "value"))

        assertThat(listener.receivedEvents).hasSize(consumer.countListeners())
        assertThat(listener.receivedEvents[0].event).isEqualTo("value")
        assertThat(listener.receivedEvents[0].partition).isEqualTo(1)
        assertThat(listener.receivedEvents[0].offset).isEqualTo(0)
    }

    @Test
    fun givenEventConsumer_whenAddingSameListenerTwice_replacesListener() {
        consumer.addListener(listener)
        consumer.addListener(listener)
        consumer.onMessage(ConsumerRecord("topic", 1, 0, null, "value"))

        assertThat(consumer.countListeners()).isEqualTo(1)
        assertThat(listener.receivedEvents).hasSize(1)
        assertThat(listener.receivedEvents.map { it.event }).containsExactly("value")
    }

    @Test
    fun givenEventConsumer_whenAdding1Listener_listenerCountIs1() {
        consumer.addListener(listener)
        assertThat(consumer.countListeners()).isEqualTo(1)
    }

    @Test
    fun givenEventConsumer_whenAdding1ListenerAndRemoving1_listenerCountIs0() {
        consumer.addListener(listener)
        listener.listenerId = "L1"
        consumer.removeListener(listener)
        assertThat(consumer.countListeners()).isEqualTo(0)
    }

    @Test
    fun givenEventConsumer_whenListenerRemovedDuringDispatch_dispatchCompletes() {
        val removingListener = object : ListenerObject() {
            override fun onEvent(event: Any, partition: Int, offset: Long, timestamp: Long) {
                super.onEvent(event, partition, offset, timestamp)
                consumer.removeListener(this)
            }
        }
        val stableListener = ListenerObject().apply { listenerId = "StableListener" }

        consumer.addListener(removingListener)
        consumer.addListener(stableListener)
        consumer.onMessage(ConsumerRecord("topic", 1, 0, null, "value"))

        assertThat(removingListener.receivedEvents).hasSize(1)
        assertThat(stableListener.receivedEvents).hasSize(1)
        assertThat(consumer.getListener(removingListener.id())).isNull()
    }

}
