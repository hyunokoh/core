package co.nilin.opex.accountant.app.listener

import co.nilin.opex.accountant.core.inout.KycLevelUpdatedEvent
import co.nilin.opex.accountant.core.model.KycLevel
import co.nilin.opex.accountant.core.spi.UserLevelLoader
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.time.LocalDateTime
import java.util.concurrent.CountDownLatch
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

class KycLevelUpdatedListenerTest {

    @Test
    fun givenUpdateFailure_whenNextEventHandled_thenScopeContinuesProcessingEvents() {
        val userLevelLoader = FailingOnceUserLevelLoader()
        val listener = listener(userLevelLoader)

        listener.onEvent(event("user-1"), 0, 10, 1000, "event-1")
        listener.onEvent(event("user-2"), 0, 11, 1001, "event-2")

        assertTrue(userLevelLoader.await(5, TimeUnit.SECONDS))
        assertEquals(2, userLevelLoader.updateCalls.get())
        assertTrue(userLevelLoader.updatedUsers.containsAll(listOf("user-1", "user-2")))
        listener.destroy()
    }

    private fun listener(userLevelLoader: UserLevelLoader): KycLevelUpdatedListener {
        return KycLevelUpdatedListener(
            userLevelLoader,
            CoroutineScope(SupervisorJob() + Dispatchers.Default)
        )
    }

    private fun event(userId: String): KycLevelUpdatedEvent {
        return KycLevelUpdatedEvent(userId, KycLevel.Level2, LocalDateTime.now())
    }

    private class FailingOnceUserLevelLoader : UserLevelLoader {
        private val failed = AtomicBoolean()
        private val latch = CountDownLatch(2)
        val updateCalls = AtomicInteger()
        val updatedUsers = CopyOnWriteArrayList<String>()

        override suspend fun load(uuid: String): String {
            return "*"
        }

        override suspend fun update(uuid: String, userLevel: KycLevel) {
            updateCalls.incrementAndGet()
            updatedUsers.add(uuid)
            latch.countDown()
            if (failed.compareAndSet(false, true)) {
                throw IllegalStateException("KYC update failed")
            }
        }

        fun await(timeout: Long, unit: TimeUnit): Boolean = latch.await(timeout, unit)
    }
}
