package co.nilin.opex.accountant.app.scheduler

import co.nilin.opex.accountant.core.api.OrderManager
import co.nilin.opex.accountant.core.api.TradeManager
import co.nilin.opex.accountant.core.spi.TempEventPersister
import co.nilin.opex.matching.engine.core.eventh.events.CancelOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.CreateOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.RejectOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.TradeEvent
import co.nilin.opex.matching.engine.core.eventh.events.UpdatedOrderEvent
import kotlinx.coroutines.runBlocking
import org.slf4j.LoggerFactory
import org.springframework.context.annotation.Profile
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Service

@Service
@Profile("scheduled")
class TempEventsJob(
    private val tempEventPersister: TempEventPersister,
    private val orderManager: OrderManager,
    private val tradeManager: TradeManager,
) {

    private val log = LoggerFactory.getLogger(TempEventsJob::class.java)

    @Scheduled(fixedDelay = 1000)
    fun processTempEventJobs() {
        runBlocking {
            try {
                val tempEvents = tempEventPersister.fetchTempEvents(0, 100)
                tempEvents.forEach { tempEvent ->
                    try {
                        when (val event = tempEvent.eventBody) {
                            is CreateOrderEvent -> orderManager.handleNewOrder(event)
                            is UpdatedOrderEvent -> orderManager.handleUpdateOrder(event)
                            is RejectOrderEvent -> orderManager.handleRejectOrder(event)
                            is CancelOrderEvent -> orderManager.handleCancelOrder(event)
                            is TradeEvent -> tradeManager.handleTrade(event)
                            else -> log.debug("Skipping unsupported temp event {}", event::class.simpleName)
                        }
                    } catch (e: Exception) {
                        log.warn(
                            "Temp event {} for ouid {} is still not ready; keeping it for retry",
                            tempEvent.eventBody::class.simpleName,
                            tempEvent.ouid,
                            e
                        )
                    }
                }
            } catch (e: Exception) {
                log.error("Job error!", e)
            }
        }
    }

}
