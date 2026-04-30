package co.nilin.opex.accountant.ports.postgres.impl

import co.nilin.opex.accountant.core.spi.ProcessedEventPersister
import kotlinx.coroutines.reactor.awaitSingle
import org.springframework.r2dbc.core.DatabaseClient
import org.springframework.stereotype.Component

@Component
class ProcessedEventPersisterImpl(
    private val databaseClient: DatabaseClient
) : ProcessedEventPersister {

    override suspend fun tryMarkProcessed(eventType: String, eventKey: String): Boolean {
        val rowsUpdated = databaseClient.sql(
            """
            INSERT INTO processed_events (event_type, event_key, create_date)
            VALUES (:eventType, :eventKey, NOW())
            ON CONFLICT (event_type, event_key) DO NOTHING
            """.trimIndent()
        )
            .bind("eventType", eventType)
            .bind("eventKey", eventKey)
            .fetch()
            .rowsUpdated()
            .awaitSingle()

        return rowsUpdated > 0
    }
}
