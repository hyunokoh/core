package co.nilin.opex.accountant.core.service

import co.nilin.opex.accountant.core.model.FinancialAction
import co.nilin.opex.accountant.core.model.FinancialActionCategory
import co.nilin.opex.accountant.core.model.FinancialActionStatus
import co.nilin.opex.accountant.core.model.WalletType
import co.nilin.opex.accountant.core.spi.FinancialActionLoader
import co.nilin.opex.accountant.core.spi.FinancialActionPersister
import co.nilin.opex.accountant.core.spi.WalletProxy
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import java.math.BigDecimal
import java.time.LocalDateTime

internal class FinancialActionJobManagerImplTest {

    @Test
    fun givenRetryFinancialAction_whenRetried_thenUseUuidTransferRefForIdempotency(): Unit = runBlocking {
        val financialAction = financialAction(id = 42, uuid = "retry-fa-uuid")
        val loader = FakeFinancialActionStore(retries = listOf(financialAction))
        val walletProxy = RecordingWalletProxy()
        val manager = FinancialActionJobManagerImpl(loader, loader, walletProxy)

        manager.retryFinancialActions(10)

        assertThat(walletProxy.transferRefs).containsExactly("accountant:fiActions:retry-fa-uuid")
        assertThat(loader.statusUpdates).containsExactly(financialAction.uuid to FinancialActionStatus.PROCESSED)
        assertThat(loader.retrySuccesses).containsExactly(financialAction.id)
    }

    @Test
    fun givenReadyFinancialAction_whenProcessed_thenUseSameUuidTransferRefAsRetryPath(): Unit = runBlocking {
        val financialAction = financialAction(id = 7, uuid = "ready-fa-uuid")
        val loader = FakeFinancialActionStore(ready = listOf(financialAction))
        val walletProxy = RecordingWalletProxy()
        val manager = FinancialActionJobManagerImpl(loader, loader, walletProxy)

        manager.processFinancialActions(0, 10)

        assertThat(walletProxy.transferRefs).containsExactly("accountant:fiActions:ready-fa-uuid")
        assertThat(loader.statusUpdates).containsExactly(financialAction.uuid to FinancialActionStatus.PROCESSED)
    }

    private fun financialAction(id: Long, uuid: String): FinancialAction {
        return FinancialAction(
            null,
            "TradeEvent",
            "order-1",
            "USDT",
            BigDecimal.ONE,
            "sender",
            WalletType.EXCHANGE,
            "receiver",
            WalletType.MAIN,
            LocalDateTime.now(),
            FinancialActionCategory.TRADE,
            uuid = uuid,
            id = id
        )
    }

    private class FakeFinancialActionStore(
        private val ready: List<FinancialAction> = emptyList(),
        private val retries: List<FinancialAction> = emptyList()
    ) : FinancialActionLoader, FinancialActionPersister {
        val statusUpdates = mutableListOf<Pair<String, FinancialActionStatus>>()
        val retrySuccesses = mutableListOf<Long?>()
        private val byId = (ready + retries).associateBy { it.id }

        override suspend fun findLast(userUuid: String, ouid: String): FinancialAction? = null

        override suspend fun loadUnprocessed(offset: Long, size: Long): List<FinancialAction> = emptyList()

        override suspend fun countUnprocessed(userUuid: String, symbol: String, eventType: String): Long = 0

        override suspend fun loadReadyToProcess(offset: Long, size: Long): List<FinancialAction> = ready

        override suspend fun loadFinancialAction(id: Long?): FinancialAction? = byId[id]

        override suspend fun loadRetries(limit: Int): List<FinancialAction> = retries.take(limit)

        override suspend fun persist(financialActions: List<FinancialAction>): List<FinancialAction> = financialActions

        override suspend fun persistWithStatus(financialAction: FinancialAction, status: FinancialActionStatus) {}

        override suspend fun updateWithError(
            financialAction: FinancialAction,
            error: String,
            message: String?,
            body: String?
        ) {}

        override suspend fun updateStatus(financialAction: FinancialAction, status: FinancialActionStatus) {
            statusUpdates.add(financialAction.uuid to status)
        }

        override suspend fun updateStatus(faUuid: String, status: FinancialActionStatus) {
            statusUpdates.add(faUuid to status)
        }

        override suspend fun updateBatchStatus(financialAction: List<FinancialAction>, status: FinancialActionStatus) {}

        override suspend fun updateStatusNewTx(financialAction: FinancialAction, status: FinancialActionStatus) {
            statusUpdates.add(financialAction.uuid to status)
        }

        override suspend fun retrySuccessful(financialAction: FinancialAction) {
            retrySuccesses.add(financialAction.id)
        }
    }

    private class RecordingWalletProxy : WalletProxy {
        val transferRefs = mutableListOf<String?>()

        override suspend fun transfer(
            symbol: String,
            senderWalletType: WalletType,
            senderUuid: String,
            receiverWalletType: WalletType,
            receiverUuid: String,
            amount: BigDecimal,
            description: String?,
            transferRef: String?,
            transferCategory: String
        ) {
            transferRefs.add(transferRef)
        }

        override suspend fun canFulfil(
            symbol: String,
            walletType: WalletType,
            uuid: String,
            amount: BigDecimal
        ): Boolean = true
    }
}
