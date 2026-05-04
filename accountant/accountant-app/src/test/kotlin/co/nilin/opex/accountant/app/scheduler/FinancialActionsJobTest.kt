package co.nilin.opex.accountant.app.scheduler

import co.nilin.opex.accountant.core.api.FinancialActionJobManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

class FinancialActionsJobTest {

    @Test
    fun givenProcessJobAlreadyRunning_whenSchedulerTicksAgain_thenDoNotStartDuplicateProcessJob() {
        val manager = BlockingFinancialActionJobManager()
        val job = financialActionsJob(manager)

        job.processFinancialActions()
        assertTrue(manager.awaitProcessStarted(5, TimeUnit.SECONDS))

        job.processFinancialActions()

        assertEquals(1, manager.processCalls.get())
        manager.releaseProcess()
        assertTrue(manager.awaitProcessFinished(5, TimeUnit.SECONDS))
        job.destroy()
    }

    @Test
    fun givenProcessJobFails_whenSchedulerTicksAgain_thenNextProcessJobStillRuns() {
        val manager = FailingOnceFinancialActionJobManager()
        val job = financialActionsJob(manager)

        job.processFinancialActions()
        assertTrue(manager.awaitProcessCalls(5, TimeUnit.SECONDS))

        assertTrue(eventually(5, TimeUnit.SECONDS) {
            job.processFinancialActions()
            manager.awaitSecondProcessCall(50, TimeUnit.MILLISECONDS)
        })

        assertEquals(2, manager.processCalls.get())
        job.destroy()
    }

    @Test
    fun givenRetryJobAlreadyRunning_whenSchedulerTicksAgain_thenDoNotStartDuplicateRetryJob() {
        val manager = BlockingFinancialActionJobManager()
        val job = financialActionsJob(manager)

        job.retryFinancialActions()
        assertTrue(manager.awaitRetryStarted(5, TimeUnit.SECONDS))

        job.retryFinancialActions()

        assertEquals(1, manager.retryCalls.get())
        manager.releaseRetry()
        assertTrue(manager.awaitRetryFinished(5, TimeUnit.SECONDS))
        job.destroy()
    }

    private fun financialActionsJob(manager: FinancialActionJobManager): FinancialActionsJob {
        return FinancialActionsJob(
            manager,
            CoroutineScope(SupervisorJob() + Dispatchers.Default),
            CoroutineScope(SupervisorJob() + Dispatchers.Default)
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

    private class BlockingFinancialActionJobManager : FinancialActionJobManager {
        val processCalls = AtomicInteger()
        val retryCalls = AtomicInteger()
        private val processStarted = CountDownLatch(1)
        private val processRelease = CountDownLatch(1)
        private val processFinished = CountDownLatch(1)
        private val retryStarted = CountDownLatch(1)
        private val retryRelease = CountDownLatch(1)
        private val retryFinished = CountDownLatch(1)

        override suspend fun processFinancialActions(offset: Long, size: Long) {
            processCalls.incrementAndGet()
            processStarted.countDown()
            processRelease.await(5, TimeUnit.SECONDS)
            processFinished.countDown()
        }

        override suspend fun retryFinancialActions(limit: Int) {
            retryCalls.incrementAndGet()
            retryStarted.countDown()
            retryRelease.await(5, TimeUnit.SECONDS)
            retryFinished.countDown()
        }

        fun awaitProcessStarted(timeout: Long, unit: TimeUnit): Boolean = processStarted.await(timeout, unit)

        fun awaitProcessFinished(timeout: Long, unit: TimeUnit): Boolean = processFinished.await(timeout, unit)

        fun releaseProcess() = processRelease.countDown()

        fun awaitRetryStarted(timeout: Long, unit: TimeUnit): Boolean = retryStarted.await(timeout, unit)

        fun awaitRetryFinished(timeout: Long, unit: TimeUnit): Boolean = retryFinished.await(timeout, unit)

        fun releaseRetry() = retryRelease.countDown()
    }

    private class FailingOnceFinancialActionJobManager : FinancialActionJobManager {
        val processCalls = AtomicInteger()
        private val failed = AtomicBoolean()
        private val firstProcessCall = CountDownLatch(1)
        private val secondProcessCall = CountDownLatch(1)

        override suspend fun processFinancialActions(offset: Long, size: Long) {
            val calls = processCalls.incrementAndGet()
            if (calls == 1) {
                firstProcessCall.countDown()
                if (failed.compareAndSet(false, true)) {
                    throw IllegalStateException("process failed")
                }
            }
            if (calls == 2) {
                secondProcessCall.countDown()
            }
        }

        override suspend fun retryFinancialActions(limit: Int) {
        }

        fun awaitProcessCalls(timeout: Long, unit: TimeUnit): Boolean = firstProcessCall.await(timeout, unit)

        fun awaitSecondProcessCall(timeout: Long, unit: TimeUnit): Boolean = secondProcessCall.await(timeout, unit)
    }
}
