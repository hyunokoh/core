package co.nilin.opex.wallet.app.listener

import co.nilin.opex.common.OpexError
import co.nilin.opex.wallet.core.inout.FinancialActionResponseEvent
import co.nilin.opex.wallet.core.inout.Status
import co.nilin.opex.wallet.core.inout.TransferResult
import co.nilin.opex.wallet.core.model.TransferCategory
import co.nilin.opex.wallet.core.model.WalletType
import co.nilin.opex.wallet.core.spi.FiActionResponseEventSubmitter
import co.nilin.opex.wallet.ports.kafka.listener.model.FinancialActionEvent
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test
import java.math.BigDecimal
import java.time.LocalDateTime

internal class FinancialActionEventListenerImplTest {

    @Test
    fun givenDuplicateTransferRef_whenFinancialActionEventHandled_thenSubmitProcessedResponse() {
        val submitter = RecordingResponseSubmitter()
        val listener = FinancialActionEventListenerImpl(
            ThrowingTransferExecutor(OpexError.BadRequest.exception("transferRef already exists")),
            submitter
        )

        listener.onEvent(financialActionEvent("duplicate-action-uuid"), 0, 1, System.currentTimeMillis())

        assertEquals(1, submitter.events.size)
        with(submitter.events.single()) {
            assertEquals("duplicate-action-uuid", uuid)
            assertEquals(Status.PROCESSED, status)
            assertNull(errorCode)
            assertNull(reason)
        }
    }

    @Test
    fun givenTransferError_whenFinancialActionEventHandled_thenSubmitErrorResponse() {
        val submitter = RecordingResponseSubmitter()
        val listener = FinancialActionEventListenerImpl(
            ThrowingTransferExecutor(OpexError.NotEnoughBalance.exception()),
            submitter
        )

        listener.onEvent(financialActionEvent("failed-action-uuid"), 0, 1, System.currentTimeMillis())

        assertEquals(1, submitter.events.size)
        with(submitter.events.single()) {
            assertEquals("failed-action-uuid", uuid)
            assertEquals(Status.ERROR, status)
            assertEquals(OpexError.NotEnoughBalance.code(), errorCode)
            assertEquals(OpexError.NotEnoughBalance.errorName(), reason)
        }
    }

    private fun financialActionEvent(uuid: String): FinancialActionEvent {
        return FinancialActionEvent(
            uuid,
            "USDT",
            BigDecimal.TEN,
            "sender",
            WalletType.MAIN,
            "receiver",
            WalletType.EXCHANGE,
            LocalDateTime.now(),
            "accountant:fiActions:$uuid",
            "financial action test",
            TransferCategory.ORDER_CREATE,
            emptyMap()
        )
    }

    private class RecordingResponseSubmitter : FiActionResponseEventSubmitter {
        val events = mutableListOf<FinancialActionResponseEvent>()

        override suspend fun submit(event: FinancialActionResponseEvent) {
            events.add(event)
        }
    }

    private class ThrowingTransferExecutor(private val error: RuntimeException) : FinancialActionTransferExecutor {
        override suspend fun transfer(event: FinancialActionEvent): TransferResult {
            throw error
        }
    }

}
