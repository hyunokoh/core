package co.nilin.opex.accountant.app.scheduler

import co.nilin.opex.accountant.core.api.OrderManager
import co.nilin.opex.accountant.core.api.TradeManager
import co.nilin.opex.accountant.core.model.FinancialAction
import co.nilin.opex.accountant.core.model.TempEvent
import co.nilin.opex.accountant.core.spi.TempEventPersister
import co.nilin.opex.matching.engine.core.eventh.events.CancelOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import co.nilin.opex.matching.engine.core.eventh.events.CreateOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.RejectOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.SubmitOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.TradeEvent
import co.nilin.opex.matching.engine.core.eventh.events.UpdatedOrderEvent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class TempEventsJobTest {

    @Test
    fun givenTempEventJobAlreadyRunning_whenSchedulerTicksAgain_thenDoNotStartDuplicateJob() {
        val persister = BlockingTempEventPersister()
        val job = tempEventsJob(persister)

        job.processTempEventJobs()
        assertTrue(persister.awaitFetchStarted(5, TimeUnit.SECONDS))

        job.processTempEventJobs()

        assertEquals(1, persister.fetchCalls.get())
        persister.releaseFetch()
        assertTrue(persister.awaitFetchFinished(5, TimeUnit.SECONDS))
        job.destroy()
    }

    @Test
    fun givenTempEventJobBlocksCancellation_whenTimeoutExpires_thenNextJobCanRun() {
        val persister = CancellationBlockingTempEventPersister()
        val job = tempEventsJob(persister, processTimeoutMs = 100)

        job.processTempEventJobs()
        assertTrue(persister.awaitFirstFetchCall(5, TimeUnit.SECONDS))

        assertTrue(eventually(5, TimeUnit.SECONDS) {
            job.processTempEventJobs()
            persister.awaitSecondFetchCall(50, TimeUnit.MILLISECONDS)
        })

        assertEquals(2, persister.fetchCalls.get())
        persister.releaseFirstFetch()
        job.destroy()
    }

    private fun tempEventsJob(
        persister: TempEventPersister,
        processTimeoutMs: Long = 30_000
    ): TempEventsJob {
        return TempEventsJob(
            persister,
            NoopOrderManager(),
            NoopTradeManager(),
            CoroutineScope(SupervisorJob() + Dispatchers.Default),
            processTimeoutMs
        )
    }

    private fun eventually(timeout: Long, unit: TimeUnit, assertion: () -> Boolean): Boolean {
        val deadline = System.nanoTime() + unit.toNanos(timeout)
        while (System.nanoTime() < deadline) {
            if (assertion()) {
                return true
            }
            Thread.sleep(10)
        }
        return assertion()
    }

    private class BlockingTempEventPersister : NoopTempEventPersister() {
        val fetchCalls = AtomicInteger()
        private val fetchStarted = CountDownLatch(1)
        private val fetchRelease = CountDownLatch(1)
        private val fetchFinished = CountDownLatch(1)

        override suspend fun fetchTempEvents(offset: Long, size: Long): List<TempEvent> {
            fetchCalls.incrementAndGet()
            fetchStarted.countDown()
            fetchRelease.await(5, TimeUnit.SECONDS)
            fetchFinished.countDown()
            return emptyList()
        }

        fun awaitFetchStarted(timeout: Long, unit: TimeUnit): Boolean = fetchStarted.await(timeout, unit)

        fun awaitFetchFinished(timeout: Long, unit: TimeUnit): Boolean = fetchFinished.await(timeout, unit)

        fun releaseFetch() = fetchRelease.countDown()
    }

    private class CancellationBlockingTempEventPersister : NoopTempEventPersister() {
        val fetchCalls = AtomicInteger()
        private val firstFetchCall = CountDownLatch(1)
        private val secondFetchCall = CountDownLatch(1)
        private val firstFetchRelease = CountDownLatch(1)

        override suspend fun fetchTempEvents(offset: Long, size: Long): List<TempEvent> {
            val calls = fetchCalls.incrementAndGet()
            if (calls == 1) {
                firstFetchCall.countDown()
                firstFetchRelease.await(5, TimeUnit.SECONDS)
            }
            if (calls == 2) {
                secondFetchCall.countDown()
            }
            return emptyList()
        }

        fun awaitFirstFetchCall(timeout: Long, unit: TimeUnit): Boolean = firstFetchCall.await(timeout, unit)

        fun awaitSecondFetchCall(timeout: Long, unit: TimeUnit): Boolean = secondFetchCall.await(timeout, unit)

        fun releaseFirstFetch() = firstFetchRelease.countDown()
    }

    private open class NoopTempEventPersister : TempEventPersister {
        override suspend fun saveTempEvent(ouid: String, event: CoreEvent) {
        }

        override suspend fun loadTempEvents(ouid: String): List<CoreEvent> = emptyList()

        override suspend fun removeTempEvent(ouid: String, event: CoreEvent) {
        }

        override suspend fun removeTempEvents(ouid: String) {
        }

        override suspend fun removeTempEvents(tempEvents: List<TempEvent>) {
        }

        override suspend fun fetchTempEvents(offset: Long, size: Long): List<TempEvent> = emptyList()
    }

    private class NoopOrderManager : OrderManager {
        override suspend fun handleRequestOrder(submitOrderEvent: SubmitOrderEvent): List<FinancialAction> = emptyList()

        override suspend fun handleNewOrder(createOrderEvent: CreateOrderEvent): List<FinancialAction> = emptyList()

        override suspend fun handleUpdateOrder(updatedOrderEvent: UpdatedOrderEvent): List<FinancialAction> = emptyList()

        override suspend fun handleRejectOrder(rejectOrderEvent: RejectOrderEvent): List<FinancialAction> = emptyList()

        override suspend fun handleCancelOrder(cancelOrderEvent: CancelOrderEvent): List<FinancialAction> = emptyList()
    }

    private class NoopTradeManager : TradeManager {
        override suspend fun handleTrade(trade: TradeEvent): List<FinancialAction> = emptyList()
    }
}
