package co.nilin.opex.accountant.app.listener

import co.nilin.opex.accountant.core.inout.KycLevelUpdatedEvent
import co.nilin.opex.accountant.core.spi.UserLevelLoader
import co.nilin.opex.accountant.ports.kafka.listener.spi.KycLevelUpdatedEventListener
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.DisposableBean
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.stereotype.Component

@Component
class KycLevelUpdatedListener : KycLevelUpdatedEventListener, DisposableBean {

    private val logger = LoggerFactory.getLogger(KycLevelUpdatedListener::class.java)
    private val userLevelLoader: UserLevelLoader
    private val scope: CoroutineScope

    @Autowired
    constructor(userLevelLoader: UserLevelLoader) : this(
        userLevelLoader,
        CoroutineScope(SupervisorJob() + Dispatchers.IO)
    )

    internal constructor(userLevelLoader: UserLevelLoader, scope: CoroutineScope) {
        this.userLevelLoader = userLevelLoader
        this.scope = scope
    }

    override fun id(): String {
        return "KycLevelUpdatedListener"
    }

    override fun onEvent(
        event: KycLevelUpdatedEvent,
        partition: Int,
        offset: Long,
        timestamp: Long,
        eventId: String
    ) {
        logger.info(
            "Incoming UserLevelUpdated event: eventId={}, userId={}, kycLevel={}, partition={}, offset={}",
            eventId,
            event.userId,
            event.kycLevel,
            partition,
            offset
        )
        scope.launch {
            try {
                userLevelLoader.update(event.userId, event.kycLevel)
            } catch (e: Exception) {
                logger.error(
                    "Failed to update user KYC level: eventId=$eventId, userId=${event.userId}, kycLevel=${event.kycLevel}",
                    e
                )
            }
        }
    }

    override fun destroy() {
        scope.cancel()
    }
}
