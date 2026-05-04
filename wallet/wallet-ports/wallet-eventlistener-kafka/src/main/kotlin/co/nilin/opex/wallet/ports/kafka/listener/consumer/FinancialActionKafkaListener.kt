package co.nilin.opex.wallet.ports.kafka.listener.consumer

import co.nilin.opex.wallet.ports.kafka.listener.model.FinancialActionEvent
import co.nilin.opex.wallet.ports.kafka.listener.spi.FinancialActionEventListener
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.springframework.kafka.listener.MessageListener
import org.springframework.stereotype.Component
import java.util.concurrent.CopyOnWriteArrayList

@Component
class FinancialActionKafkaListener : MessageListener<String, FinancialActionEvent> {

    val eventListeners = CopyOnWriteArrayList<FinancialActionEventListener>()

    override fun onMessage(data: ConsumerRecord<String, FinancialActionEvent>) {
        eventListeners.forEach {
            it.onEvent(data.value(), data.partition(), data.offset(), data.timestamp())
        }
    }

    fun addEventListener(tl: FinancialActionEventListener) {
        removeEventListener(tl)
        eventListeners.add(tl)
    }

    fun removeEventListener(tl: FinancialActionEventListener) {
        eventListeners.removeIf { it.id() == tl.id() }
    }
}
