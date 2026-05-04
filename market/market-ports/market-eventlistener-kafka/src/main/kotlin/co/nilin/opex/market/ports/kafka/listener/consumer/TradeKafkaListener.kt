package co.nilin.opex.market.ports.kafka.listener.consumer

import co.nilin.opex.market.core.event.RichTrade
import co.nilin.opex.market.ports.kafka.listener.spi.RichTradeListener
import org.apache.kafka.clients.consumer.ConsumerRecord
import org.springframework.kafka.listener.MessageListener
import org.springframework.stereotype.Component
import java.util.concurrent.CopyOnWriteArrayList

@Component
class TradeKafkaListener : MessageListener<String, RichTrade> {

    val tradeListeners = CopyOnWriteArrayList<RichTradeListener>()

    override fun onMessage(data: ConsumerRecord<String, RichTrade>) {
        tradeListeners.forEach { tl ->
            tl.onTrade(data.value(), data.partition(), data.offset(), data.timestamp())
        }
    }

    fun addTradeListener(tl: RichTradeListener) {
        removeTradeListener(tl)
        tradeListeners.add(tl)
    }

    fun removeTradeListener(tl: RichTradeListener) {
        tradeListeners.removeIf { item ->
            item.id() == tl.id()
        }
    }
}
