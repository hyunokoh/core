package co.nilin.opex.accountant.app.scheduler

import co.nilin.opex.accountant.core.api.OrderManager
import co.nilin.opex.accountant.core.api.TradeManager
import co.nilin.opex.accountant.core.spi.TempEventPersister
import co.nilin.opex.matching.engine.core.eventh.events.CancelOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.CreateOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.RejectOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.TradeEvent
import co.nilin.opex.matching.engine.core.eventh.events.UpdatedOrderEvent
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.cancel
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeout
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.DisposableBean
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.context.annotation.Profile
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Service

@Service
@Profile("scheduled")
class TempEventsJob : DisposableBean {

    private val tempEventPersister: TempEventPersister
    private val orderManager: OrderManager
    private val tradeManager: TradeManager
    private val scope: CoroutineScope
    private val processTimeoutMs: Long
    private var processJob: Job? = null
    private var processJobStartedAtMs: Long = 0

    private val log = LoggerFactory.getLogger(TempEventsJob::class.java)

    @Autowired
    constructor(
        tempEventPersister: TempEventPersister,
        orderManager: OrderManager,
        tradeManager: TradeManager
    ) : this(
        tempEventPersister,
        orderManager,
        tradeManager,
        CoroutineScope(SupervisorJob() + Dispatchers.IO),
        30_000
    )

    internal constructor(
        tempEventPersister: TempEventPersister,
        orderManager: OrderManager,
        tradeManager: TradeManager,
        scope: CoroutineScope,
        processTimeoutMs: Long = 30_000
    ) {
        this.tempEventPersister = tempEventPersister
        this.orderManager = orderManager
        this.tradeManager = tradeManager
        this.scope = scope
        this.processTimeoutMs = processTimeoutMs
    }

    @Scheduled(fixedDelay = 1000)
    @Synchronized
    fun processTempEventJobs() {
        scope.ensureActive()
        if (!canStartJob())
            return

        processJobStartedAtMs = System.currentTimeMillis()
        processJob = scope.launch {
            try {
                withTimeout(processTimeoutMs) {
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
                        } catch (e: CancellationException) {
                            throw e
                        } catch (e: Exception) {
                            log.warn(
                                "Temp event {} for ouid {} is still not ready; keeping it for retry",
                                tempEvent.eventBody::class.simpleName,
                                tempEvent.ouid,
                                e
                            )
                        }
                    }
                }
            } catch (e: TimeoutCancellationException) {
                log.error("Temp event job error", e)
            } catch (e: CancellationException) {
                log.debug("Temp event job cancelled")
            } catch (e: Exception) {
                log.error("Temp event job error", e)
            }
        }
    }

    private fun canStartJob(): Boolean {
        val job = processJob
        if (job == null || job.isCompleted) {
            return true
        }
        if (job.isActive) {
            val elapsedMs = System.currentTimeMillis() - processJobStartedAtMs
            if (elapsedMs < processTimeoutMs) {
                return false
            }
            log.warn("Temp event job exceeded ${processTimeoutMs}ms; cancelling stale run and starting a new one")
            job.cancel()
        }
        return true
    }

    override fun destroy() {
        processJob?.cancel()
        scope.cancel()
    }

}
