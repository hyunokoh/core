package co.nilin.opex.matching.engine.app.bl

import co.nilin.opex.matching.engine.app.config.AppSchedulers
import co.nilin.opex.matching.engine.core.eventh.EventDispatcher
import co.nilin.opex.matching.engine.core.eventh.events.*
import co.nilin.opex.matching.engine.core.spi.OrderBookPersister
import co.nilin.opex.matching.engine.ports.kafka.submitter.service.EventsSubmitter
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.beans.factory.DisposableBean
import org.springframework.stereotype.Component

@Component
class ExchangeEventHandler : DisposableBean {
    private lateinit var eventsSubmitter: EventsSubmitter
    private lateinit var orderBookPersister: OrderBookPersister
    private lateinit var eventScope: CoroutineScope
    private val logger = LoggerFactory.getLogger(ExchangeEventHandler::class.java)
    private val registrations = mutableListOf<EventDispatcher.Registration>()

    @Autowired
    constructor(
        eventsSubmitter: EventsSubmitter,
        orderBookPersister: OrderBookPersister
    ) : this(eventsSubmitter, orderBookPersister, CoroutineScope(SupervisorJob() + AppSchedulers.generalExecutor))

    internal constructor(
        eventsSubmitter: EventsSubmitter,
        orderBookPersister: OrderBookPersister,
        eventScope: CoroutineScope
    ) {
        this.eventsSubmitter = eventsSubmitter
        this.orderBookPersister = orderBookPersister
        this.eventScope = eventScope
    }

    fun register() {
        if (registrations.isNotEmpty()) {
            return
        }
        registrations.add(EventDispatcher.register(CreateOrderEvent::class.java, handler))
        registrations.add(EventDispatcher.register(CancelOrderEvent::class.java, handler))
        registrations.add(EventDispatcher.register(UpdatedOrderEvent::class.java, handler))
        registrations.add(EventDispatcher.register(RejectOrderEvent::class.java, handler))
        registrations.add(EventDispatcher.register(SubmitOrderEvent::class.java, handler))
        registrations.add(EventDispatcher.register(TradeEvent::class.java, handler))
        registrations.add(EventDispatcher.register(OrderBookPublishedEvent::class.java, localHandler))
    }

    val handler: (CoreEvent) -> Unit = {
        eventScope.launch {
            try {
                eventsSubmitter.submit(it)
            } catch (e: Exception) {
                logger.error("Failed to submit matching event: pair=${it.pair}, type=${it::class.java.simpleName}", e)
            }
        }
    }

    val localHandler: (OrderBookPublishedEvent) -> Unit = {
        eventScope.launch {
            try {
                orderBookPersister.storeLastState(it.persistentOrderBook)
            } catch (e: Exception) {
                logger.error("Failed to persist matching order book snapshot: pair=${it.persistentOrderBook.pair}", e)
            }
        }
    }

    override fun destroy() {
        registrations.forEach { it.close() }
        registrations.clear()
        eventScope.cancel()
    }
}
