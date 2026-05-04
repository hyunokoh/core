package co.nilin.opex.accountant.ports.kafka.listener

import co.nilin.opex.accountant.core.inout.KycLevelUpdatedEvent
import co.nilin.opex.accountant.core.model.KycLevel
import co.nilin.opex.accountant.ports.kafka.listener.consumer.KycLevelUpdatedKafkaListener
import co.nilin.opex.accountant.ports.kafka.listener.spi.KycLevelUpdatedEventListener
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import java.time.LocalDateTime
import java.util.concurrent.atomic.AtomicInteger

class KycLevelUpdatedKafkaListenerTest {

    @Test
    fun givenSameListenerId_whenAddEventListener_thenReplaceExistingListener() {
        val consumer = KycLevelUpdatedKafkaListener()
        val calls = AtomicInteger()

        consumer.addEventListener(countingListener("listener", calls))
        consumer.addEventListener(countingListener("listener", calls))
        consumer.onMessage(eventRecord())

        assertThat(consumer.eventListeners).hasSize(1)
        assertThat(calls.get()).isEqualTo(1)
    }

    @Test
    fun givenListenerRemovedDuringDispatch_whenOnMessage_thenDispatchCompletes() {
        val consumer = KycLevelUpdatedKafkaListener()
        val calls = AtomicInteger()
        lateinit var removingListener: KycLevelUpdatedEventListener
        removingListener = object : KycLevelUpdatedEventListener {
            override fun id() = "removing"

            override fun onEvent(
                event: KycLevelUpdatedEvent,
                partition: Int,
                offset: Long,
                timestamp: Long,
                eventId: String
            ) {
                calls.incrementAndGet()
                consumer.removeEventListener(removingListener)
            }
        }

        consumer.addEventListener(removingListener)
        consumer.addEventListener(countingListener("stable", calls))
        consumer.onMessage(eventRecord())

        assertThat(calls.get()).isEqualTo(2)
        assertThat(consumer.eventListeners.none { it.id() == "removing" }).isTrue()
    }

    private fun countingListener(id: String, calls: AtomicInteger): KycLevelUpdatedEventListener {
        return object : KycLevelUpdatedEventListener {
            override fun id() = id

            override fun onEvent(
                event: KycLevelUpdatedEvent,
                partition: Int,
                offset: Long,
                timestamp: Long,
                eventId: String
            ) {
                calls.incrementAndGet()
            }
        }
    }

    private fun eventRecord(): ConsumerRecord<String, KycLevelUpdatedEvent> {
        return ConsumerRecord(
            "kyc_level_updated",
            0,
            1,
            "user",
            KycLevelUpdatedEvent("user", KycLevel.Level1, LocalDateTime.now())
        )
    }
}
