package co.nilin.opex.accountant.app.listener

import co.nilin.opex.accountant.core.model.FinancialAction
import co.nilin.opex.accountant.core.model.FinancialActionStatus
import co.nilin.opex.accountant.core.spi.FinancialActionPersister
import co.nilin.opex.accountant.ports.kafka.listener.inout.FinancialActionResponseEvent
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

internal class AccountantFAResponseEventListenerTest {

    @Test
    fun givenProcessedResponse_whenEventHandled_thenUpdateStatusOnly() {
        val persister = RecordingFinancialActionPersister()
        val listener = AccountantFAResponseEventListener(persister)

        listener.onEvent(
            FinancialActionResponseEvent("processed-uuid", FinancialActionStatus.PROCESSED, null, null),
            0,
            1,
            System.currentTimeMillis()
        )

        assertEquals(listOf("processed-uuid" to FinancialActionStatus.PROCESSED), persister.statusUpdates)
        assertTrue(persister.errors.isEmpty())
    }

    @Test
    fun givenErrorResponse_whenEventHandled_thenPersistErrorDetails() {
        val persister = RecordingFinancialActionPersister()
        val listener = AccountantFAResponseEventListener(persister)

        listener.onEvent(
            FinancialActionResponseEvent("failed-uuid", FinancialActionStatus.ERROR, 6018, "NotEnoughBalance"),
            0,
            2,
            System.currentTimeMillis()
        )

        assertTrue(persister.statusUpdates.isEmpty())
        assertEquals(listOf(RecordedError("failed-uuid", "6018", "NotEnoughBalance", null)), persister.errors)
    }

    private data class RecordedError(
        val uuid: String,
        val error: String,
        val message: String?,
        val body: String?
    )

    private class RecordingFinancialActionPersister : FinancialActionPersister {
        val statusUpdates = mutableListOf<Pair<String, FinancialActionStatus>>()
        val errors = mutableListOf<RecordedError>()

        override suspend fun persist(financialActions: List<FinancialAction>): List<FinancialAction> = financialActions

        override suspend fun persistWithStatus(financialAction: FinancialAction, status: FinancialActionStatus) {}

        override suspend fun updateWithError(
            financialAction: FinancialAction,
            error: String,
            message: String?,
            body: String?
        ) {
            errors.add(RecordedError(financialAction.uuid, error, message, body))
        }

        override suspend fun updateWithError(faUuid: String, error: String, message: String?, body: String?) {
            errors.add(RecordedError(faUuid, error, message, body))
        }

        override suspend fun updateStatus(financialAction: FinancialAction, status: FinancialActionStatus) {
            statusUpdates.add(financialAction.uuid to status)
        }

        override suspend fun updateStatus(faUuid: String, status: FinancialActionStatus) {
            statusUpdates.add(faUuid to status)
        }

        override suspend fun updateBatchStatus(financialAction: List<FinancialAction>, status: FinancialActionStatus) {}

        override suspend fun updateStatusNewTx(financialAction: FinancialAction, status: FinancialActionStatus) {}

        override suspend fun retrySuccessful(financialAction: FinancialAction) {}
    }
}
