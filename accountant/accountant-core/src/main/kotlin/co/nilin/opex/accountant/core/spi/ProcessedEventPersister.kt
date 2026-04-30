package co.nilin.opex.accountant.core.spi

interface ProcessedEventPersister {
    suspend fun tryMarkProcessed(eventType: String, eventKey: String): Boolean
}
