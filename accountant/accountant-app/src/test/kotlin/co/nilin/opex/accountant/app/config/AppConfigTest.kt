package co.nilin.opex.accountant.app.config

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler

class AppConfigTest {

    @Test
    fun givenScheduledJobs_whenAppConfigCreatesScheduler_thenJobsDoNotShareSingleThread() {
        val scheduler = AppConfig().taskScheduler()

        assertTrue(scheduler is ThreadPoolTaskScheduler)
        assertEquals(4, (scheduler as ThreadPoolTaskScheduler).poolSize)
    }
}
