package co.nilin.opex.matching.engine.ports.kafka.listener.consumer

import co.nilin.opex.matching.engine.core.inout.OrderRequestEvent
import co.nilin.opex.matching.engine.ports.kafka.listener.spi.OrderRequestEventListener
import kotlinx.coroutines.runBlocking
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.springframework.kafka.listener.MessageListener
import org.springframework.stereotype.Component
import java.util.concurrent.CopyOnWriteArrayList

@Component
class OrderKafkaListener : MessageListener<String, OrderRequestEvent> {

    val orderListeners = CopyOnWriteArrayList<OrderRequestEventListener>()

    override fun onMessage(data: ConsumerRecord<String, OrderRequestEvent>) {
        orderListeners.forEach { tl ->
            runBlocking {
                tl.onOrder(data.value(), data.partition(), data.offset(), data.timestamp())
            }
        }
    }

    fun addOrderListener(tl: OrderRequestEventListener) {
        removeOrderListener(tl)
        orderListeners.add(tl)
    }

    fun removeOrderListener(tl: OrderRequestEventListener) {
        orderListeners.removeIf { item ->
            item.id() == tl.id()
        }
    }
}
