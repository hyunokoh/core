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
    private val log = LoggerFactory.getLogger(FinancialActionsJob::class.java)

    @Autowired
    constructor(financialActionJobManager: FinancialActionJobManager) : this(
        financialActionJobManager,
        CoroutineScope(SupervisorJob() + Dispatchers.IO),
        CoroutineScope(SupervisorJob() + Dispatchers.IO)
    )

    internal constructor(
        financialActionJobManager: FinancialActionJobManager,
        scope: CoroutineScope,
        retryScope: CoroutineScope
    ) {
        this.financialActionJobManager = financialActionJobManager
        this.scope = scope
        this.retryScope = retryScope
    }

    @Scheduled(fixedDelay = 10000, initialDelay = 10000)
    fun processFinancialActions() {
        scope.ensureActive()
        if (!scope.isCompleted())
            return

        scope.launch {
            try {
                //read unprocessed fa records and call transfer
                financialActionJobManager.processFinancialActions(0, 100)
            } catch (e: Exception) {
                log.error("Financial action PROCESS error", e)
            }
        }
    }

    @Scheduled(fixedDelay = 2000, initialDelay = 15000)
    fun retryFinancialActions() {
        retryScope.ensureActive()
        if (!retryScope.isCompleted())
            return

        retryScope.launch {
            try {
                financialActionJobManager.retryFinancialActions(10)
            } catch (e: Exception) {
                log.error("Financial action RETRY error", e)
            }
        }
    }

    private fun CoroutineScope.isCompleted() = coroutineContext.job.children.all { it.isCompleted }

    override fun destroy() {
        scope.cancel()
        retryScope.cancel()
    }

}
