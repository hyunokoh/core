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
    fun givenEventConsumer_onMessageWith2Listeners_callListener() {
        consumer.addListener(listener)
        consumer.addListener(listener)
        consumer.onMessage(ConsumerRecord("topic", 1, 0, null, "value"))

        assertThat(listener.receivedEvents).hasSize(2)
        assertThat(listener.receivedEvents.map { it.event }).containsExactly("value", "value")
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

}
