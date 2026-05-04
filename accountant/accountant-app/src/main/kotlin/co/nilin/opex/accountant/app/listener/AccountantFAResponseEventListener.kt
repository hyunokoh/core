package co.nilin.opex.accountant.app.listener

import co.nilin.opex.accountant.core.model.FinancialActionStatus
import co.nilin.opex.accountant.core.spi.FinancialActionPersister
import co.nilin.opex.accountant.ports.kafka.listener.inout.FinancialActionResponseEvent
import co.nilin.opex.accountant.ports.kafka.listener.spi.FAResponseListener
import kotlinx.coroutines.runBlocking
import org.slf4j.LoggerFactory

class AccountantFAResponseEventListener(private val financialActionPersister: FinancialActionPersister) :
    FAResponseListener {

    private val logger = LoggerFactory.getLogger(AccountantFAResponseEventListener::class.java)

    override fun id(): String {
        return "FAResponseEventListener"
    }

    override fun onEvent(event: FinancialActionResponseEvent, partition: Int, offset: Long, timestamp: Long) {
        runBlocking {
            if (event.status == FinancialActionStatus.ERROR) {
                logger.warn("Financial action failed in wallet: uuid=${event.uuid}, errorCode=${event.errorCode}, reason=${event.reason}")
                financialActionPersister.updateWithError(
                    event.uuid,
                    event.errorCode?.toString() ?: event.status.name,
                    event.reason,
                    null
                )
            } else {
                financialActionPersister.updateStatus(event.uuid, event.status)
            }
        }
    }

}
