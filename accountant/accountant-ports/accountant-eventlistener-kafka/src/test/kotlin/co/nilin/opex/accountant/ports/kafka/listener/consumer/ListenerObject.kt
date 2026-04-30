package co.nilin.opex.accountant.ports.kafka.listener.consumer

import co.nilin.opex.accountant.ports.kafka.listener.spi.Listener

class ListenerObject : Listener<Any> {

    val receivedEvents = mutableListOf<ReceivedEvent>()
    var listenerId = "AnyListener"

    override fun id(): String {
        return listenerId
    }

    override fun onEvent(event: Any, partition: Int, offset: Long, timestamp: Long) {
        receivedEvents.add(ReceivedEvent(event, partition, offset, timestamp))
    }
}

data class ReceivedEvent(val event: Any, val partition: Int, val offset: Long, val timestamp: Long)
