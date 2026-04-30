package co.nilin.opex.accountant.ports.kafka.submitter.service

import co.nilin.opex.accountant.core.spi.TempEventRepublisher
import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.stereotype.Component
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

@Component
class TempEventSubmitter(
    @Qualifier("accountantEventKafkaTemplate")
    private val kafkaTemplate: KafkaTemplate<String, CoreEvent>
) : TempEventRepublisher, EventPublisher {

    private val logger = LoggerFactory.getLogger(TempEventSubmitter::class.java)

    override val topic = "tempevents"

    override suspend fun republish(events: List<CoreEvent>): Unit = suspendCoroutine { cont ->
        logger.info("Submitting TempEvents")

        if (events.isEmpty()) {
            cont.resume(Unit)
            return@suspendCoroutine
        }

        val remaining = AtomicInteger(events.size)
        val firstError = AtomicReference<Throwable?>()

        events.forEach { event ->
            val sendFuture = kafkaTemplate.send(topic, event)
            sendFuture.addCallback({
                if (remaining.decrementAndGet() == 0) {
                    firstError.get()?.let(cont::resumeWithException) ?: cont.resume(Unit)
                }
            }, {
                logger.error("Error submitting TempEvents", it)
                firstError.compareAndSet(null, it)
                if (remaining.decrementAndGet() == 0) {
                    cont.resumeWithException(firstError.get() ?: it)
                }
            })
        }
    }
}
