package co.nilin.opex.accountant.app.listener

import co.nilin.opex.accountant.core.api.OrderManager
import co.nilin.opex.accountant.ports.kafka.listener.spi.EventListener
import co.nilin.opex.matching.engine.core.eventh.events.*
import kotlinx.coroutines.runBlocking
import org.slf4j.LoggerFactory

class AccountantEventListener(private val orderManager: OrderManager) : EventListener {
    private val logger = LoggerFactory.getLogger(AccountantEventListener::class.java)

    override fun id(): String {
        return "EventListener"
    }

    override fun onEvent(event: CoreEvent, partition: Int, offset: Long, timestamp: Long) {
        runBlocking {
            when (event) {
                is CreateOrderEvent -> orderManager.handleNewOrder(event)
                is RejectOrderEvent -> orderManager.handleRejectOrder(event)
                is UpdatedOrderEvent -> orderManager.handleUpdateOrder(event)
                is CancelOrderEvent -> orderManager.handleCancelOrder(event)
                else -> {
                    logger.warn("Event is not accepted: eventType={}", event::class.java.name)
                }
            }
        }
        logger.debug("Accountant event handled: eventType={}, partition={}, offset={}", event::class.java.name, partition, offset)
    }
}
