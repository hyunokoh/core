package co.nilin.opex.matching.engine.ports.kafka.submitter.service

import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import co.nilin.opex.matching.engine.core.eventh.events.TradeEvent
import org.slf4j.LoggerFactory
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.stereotype.Component
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

@Component
class EventsSubmitter(val kafkaTemplate: KafkaTemplate<String, CoreEvent>) {

    private val logger = LoggerFactory.getLogger(EventsSubmitter::class.java)

    suspend fun submit(event: CoreEvent) {
        logger.info("Submitting matching event: pair=${event.pair}, type=${event::class.java.simpleName}")
        if (event is TradeEvent) {
            send("trades_${event.pair.leftSideName}_${event.pair.rightSideName}", event)
        }
        send("events_${event.pair.leftSideName}_${event.pair.rightSideName}", event)
    }

    private suspend fun send(topic: String, event: CoreEvent): Unit = suspendCoroutine { cont ->
        kafkaTemplate.send(topic, event).addCallback({
            cont.resume(Unit)
        }, {
            logger.error("Error submitting matching event to topic=$topic", it)
            cont.resumeWithException(it)
        })
    }
}
