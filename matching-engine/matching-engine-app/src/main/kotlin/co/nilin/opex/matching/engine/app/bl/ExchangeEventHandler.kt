package co.nilin.opex.matching.engine.app.bl

import co.nilin.opex.matching.engine.app.config.AppSchedulers
import co.nilin.opex.matching.engine.core.eventh.EventDispatcher
import co.nilin.opex.matching.engine.core.eventh.events.*
import co.nilin.opex.matching.engine.core.spi.OrderBookPersister
import co.nilin.opex.matching.engine.ports.kafka.submitter.service.EventsSubmitter
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import org.springframework.beans.factory.DisposableBean
import org.springframework.stereotype.Component

@Component
class ExchangeEventHandler(
    eventsSubmitter: EventsSubmitter, orderBookPersister: OrderBookPersister
) : DisposableBean {
    private val registrations = mutableListOf<EventDispatcher.Registration>()

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
        CoroutineScope(AppSchedulers.generalExecutor).launch {
            eventsSubmitter.submit(it)
        }
    }

    val localHandler: (OrderBookPublishedEvent) -> Unit = {
        CoroutineScope(AppSchedulers.generalExecutor).launch {
            orderBookPersister.storeLastState(it.persistentOrderBook)
        }
    }

    override fun destroy() {
        registrations.forEach { it.close() }
        registrations.clear()
    }
}
