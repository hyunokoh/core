package co.nilin.opex.matching.engine.app.bl

import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import co.nilin.opex.matching.engine.core.eventh.events.CreateOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.OrderBookPublishedEvent
import co.nilin.opex.matching.engine.core.model.Pair
import co.nilin.opex.matching.engine.core.model.PersistentOrderBook
import co.nilin.opex.matching.engine.core.spi.OrderBookPersister
import co.nilin.opex.matching.engine.ports.kafka.submitter.service.EventsSubmitter
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import org.apache.kafka.clients.producer.ProducerConfig
import org.apache.kafka.common.serialization.StringSerializer
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.springframework.kafka.core.DefaultKafkaProducerFactory
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.kafka.support.SendResult
import org.springframework.kafka.support.serializer.JsonSerializer
import org.springframework.util.concurrent.ListenableFuture
import org.springframework.util.concurrent.SettableListenableFuture
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

class ExchangeEventHandlerTest {

    private val pair = Pair("BTC", "USDT")

    @Test
    fun givenSubmitFailure_whenNextEventHandled_thenScopeContinuesProcessingEvents() {
        val kafkaTemplate = CapturingKafkaTemplate(failFirstSend = true)
        val handler = ExchangeEventHandler(
            EventsSubmitter(kafkaTemplate),
            NoopOrderBookPersister,
            CoroutineScope(SupervisorJob() + Dispatchers.Default)
        )

        handler.handler(createOrderEvent())
        handler.handler(createOrderEvent())

        assertTrue(kafkaTemplate.await(5, TimeUnit.SECONDS))
        assertEquals(listOf("events_BTC_USDT", "events_BTC_USDT"), kafkaTemplate.topics)
        handler.destroy()
    }

    @Test
    fun givenSnapshotPersistenceFailure_whenNextSnapshotHandled_thenScopeContinuesProcessingSnapshots() {
        val persister = FailingOnceOrderBookPersister()
        val handler = ExchangeEventHandler(
            EventsSubmitter(CapturingKafkaTemplate()),
            persister,
            CoroutineScope(SupervisorJob() + Dispatchers.Default)
        )

        handler.localHandler(OrderBookPublishedEvent(PersistentOrderBook(pair)))
        handler.localHandler(OrderBookPublishedEvent(PersistentOrderBook(pair)))

        assertTrue(persister.await(5, TimeUnit.SECONDS))
        assertEquals(2, persister.attempts)
        handler.destroy()
    }

    private fun createOrderEvent(): CreateOrderEvent {
        return CreateOrderEvent(
            ouid = "ouid",
            uuid = "uuid",
            orderId = 1,
            pair = pair,
            price = 1,
            quantity = 1,
            remainedQuantity = 1
        )
    }

    private class CapturingKafkaTemplate(
        private val failFirstSend: Boolean = false
    ) : KafkaTemplate<String, CoreEvent>(
        DefaultKafkaProducerFactory(
            mapOf(
                ProducerConfig.BOOTSTRAP_SERVERS_CONFIG to "localhost:9092",
                ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG to StringSerializer::class.java,
                ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG to JsonSerializer::class.java
            )
        )
    ) {
        private val failed = AtomicBoolean()
        private val latch = CountDownLatch(if (failFirstSend) 2 else 1)
        val topics = CopyOnWriteArrayList<String>()

        override fun send(topic: String, data: CoreEvent): ListenableFuture<SendResult<String, CoreEvent>> {
            topics.add(topic)
            latch.countDown()

            val future = SettableListenableFuture<SendResult<String, CoreEvent>>()
            if (failFirstSend && failed.compareAndSet(false, true)) {
                future.setException(IllegalStateException("send failed"))
            } else {
                future.set(null)
            }
            return future
        }

        fun await(timeout: Long, unit: TimeUnit): Boolean = latch.await(timeout, unit)
    }

    private object NoopOrderBookPersister : OrderBookPersister {
        override suspend fun storeLastState(orderBook: PersistentOrderBook) {
        }

        override suspend fun loadLastState(symbol: String): PersistentOrderBook? = null
    }

    private class FailingOnceOrderBookPersister : OrderBookPersister {
        private val failed = AtomicBoolean()
        private val latch = CountDownLatch(2)
        private val attemptCounter = AtomicInteger()
        val attempts: Int
            get() = attemptCounter.get()

        override suspend fun storeLastState(orderBook: PersistentOrderBook) {
            attemptCounter.incrementAndGet()
            latch.countDown()
            if (failed.compareAndSet(false, true)) {
                throw IllegalStateException("store failed")
            }
        }

        override suspend fun loadLastState(symbol: String): PersistentOrderBook? = null

        fun await(timeout: Long, unit: TimeUnit): Boolean = latch.await(timeout, unit)
    }
}
