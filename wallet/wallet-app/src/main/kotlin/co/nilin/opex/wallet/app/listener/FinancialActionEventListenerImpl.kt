package co.nilin.opex.wallet.app.listener

import co.nilin.opex.utility.error.data.OpexException
import co.nilin.opex.wallet.app.service.TransferService
import co.nilin.opex.wallet.core.inout.FinancialActionResponseEvent
import co.nilin.opex.wallet.core.inout.Status
import co.nilin.opex.wallet.core.inout.TransferResult
import co.nilin.opex.wallet.core.spi.FiActionResponseEventSubmitter
import co.nilin.opex.wallet.ports.kafka.listener.model.FinancialActionEvent
import co.nilin.opex.wallet.ports.kafka.listener.spi.FinancialActionEventListener
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Component

interface FinancialActionTransferExecutor {
    suspend fun transfer(event: FinancialActionEvent): TransferResult
}

@Component
class TransferServiceFinancialActionTransferExecutor(
    private val transferService: TransferService,
) : FinancialActionTransferExecutor {

    override suspend fun transfer(event: FinancialActionEvent): TransferResult {
        return transferService.transfer(
            event.symbol,
            event.senderWalletType,
            event.sender,
            event.receiverWalletType,
            event.receiver,
            event.amount,
            event.description,
            event.transferRef,
            event.transferCategory
        )
    }
}

@Component
class FinancialActionEventListenerImpl(
    private val transferExecutor: FinancialActionTransferExecutor,
    private val responseSubmitter: FiActionResponseEventSubmitter
) : FinancialActionEventListener {

    private val logger = LoggerFactory.getLogger(FinancialActionEventListenerImpl::class.java)

    override fun id(): String {
        return "FinancialActionEventListener"
    }

    override fun onEvent(event: FinancialActionEvent, partition: Int, offset: Long, timestamp: Long) {
        logger.info("On FinancialActionEvent ${event.uuid}")
        runBlocking(Dispatchers.IO) {
            val responseEvent = FinancialActionResponseEvent(event.uuid, Status.PROCESSED)
            try {
                transferExecutor.transfer(event)
            } catch (e: OpexException) {
                if (e.isDuplicateTransferRefError()) {
                    logger.info("Financial action already processed by wallet: uuid=${event.uuid}")
                } else {
                    responseEvent.apply {
                        status = Status.ERROR
                        errorCode = e.error.code()
                        reason = e.error.errorName()
                    }
                }
            } catch (e: Exception) {
                if (e.isDuplicateTransferRefError()) {
                    logger.info("Financial action already processed by wallet: uuid=${event.uuid}")
                } else {
                    responseEvent.apply {
                        status = Status.ERROR
                        reason = e.message
                    }
                }
            }

            responseSubmitter.submit(responseEvent)
        }
    }

    private fun Throwable.isDuplicateTransferRefError(): Boolean {
        return generateSequence(this as Throwable?) { it.cause }
            .mapNotNull { it.message }
            .any { it.contains("transferRef already exists", ignoreCase = true) }
    }
}
