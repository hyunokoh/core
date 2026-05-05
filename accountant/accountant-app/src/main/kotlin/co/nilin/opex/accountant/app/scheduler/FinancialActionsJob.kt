package co.nilin.opex.accountant.app.scheduler

import co.nilin.opex.accountant.core.api.FinancialActionJobManager
import kotlinx.coroutines.*
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.DisposableBean
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.context.annotation.Profile
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Service

@Service
@Profile("scheduled")
class FinancialActionsJob : DisposableBean {

    private val financialActionJobManager: FinancialActionJobManager
    private val scope: CoroutineScope
    private val retryScope: CoroutineScope
    private val processTimeoutMs: Long
    private val retryTimeoutMs: Long
    private var processJob: Job? = null
    private var processJobStartedAtMs: Long = 0
    private var retryJob: Job? = null
    private var retryJobStartedAtMs: Long = 0
    private val log = LoggerFactory.getLogger(FinancialActionsJob::class.java)

    @Autowired
    constructor(financialActionJobManager: FinancialActionJobManager) : this(
        financialActionJobManager,
        CoroutineScope(SupervisorJob() + Dispatchers.IO),
        CoroutineScope(SupervisorJob() + Dispatchers.IO),
        30_000,
        30_000
    )

    internal constructor(
        financialActionJobManager: FinancialActionJobManager,
        scope: CoroutineScope,
        retryScope: CoroutineScope,
        processTimeoutMs: Long = 30_000,
        retryTimeoutMs: Long = 30_000
    ) {
        this.financialActionJobManager = financialActionJobManager
        this.scope = scope
        this.retryScope = retryScope
        this.processTimeoutMs = processTimeoutMs
        this.retryTimeoutMs = retryTimeoutMs
    }

    @Scheduled(fixedDelay = 10000, initialDelay = 10000)
    @Synchronized
    fun processFinancialActions() {
        scope.ensureActive()
        if (!canStartJob(processJob, processJobStartedAtMs, processTimeoutMs, "PROCESS"))
            return

        processJobStartedAtMs = System.currentTimeMillis()
        processJob = scope.launch {
            try {
                //read unprocessed fa records and call transfer
                withTimeout(processTimeoutMs) {
                    financialActionJobManager.processFinancialActions(0, 100)
                }
            } catch (e: Exception) {
                log.error("Financial action PROCESS error", e)
            }
        }
    }

    @Scheduled(fixedDelay = 2000, initialDelay = 15000)
    @Synchronized
    fun retryFinancialActions() {
        retryScope.ensureActive()
        if (!canStartJob(retryJob, retryJobStartedAtMs, retryTimeoutMs, "RETRY"))
            return

        retryJobStartedAtMs = System.currentTimeMillis()
        retryJob = retryScope.launch {
            try {
                withTimeout(retryTimeoutMs) {
                    financialActionJobManager.retryFinancialActions(10)
                }
            } catch (e: Exception) {
                log.error("Financial action RETRY error", e)
            }
        }
    }

    private fun canStartJob(job: Job?, startedAtMs: Long, timeoutMs: Long, label: String): Boolean {
        if (job == null || job.isCompleted) {
            return true
        }
        if (job.isActive) {
            val elapsedMs = System.currentTimeMillis() - startedAtMs
            if (elapsedMs < timeoutMs) {
                return false
            }
            log.warn("Financial action $label job exceeded ${timeoutMs}ms; cancelling stale run and starting a new one")
            job.cancel()
        }
        return true
    }

    override fun destroy() {
        processJob?.cancel()
        retryJob?.cancel()
        scope.cancel()
        retryScope.cancel()
    }

}
