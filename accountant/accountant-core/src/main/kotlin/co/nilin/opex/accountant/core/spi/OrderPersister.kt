package co.nilin.opex.accountant.core.spi

import co.nilin.opex.accountant.core.model.Order

interface OrderPersister {
    suspend fun load(ouid: String): Order?
    suspend fun loadForUpdate(ouid: String): Order? = load(ouid)
    suspend fun save(order: Order): Order
    suspend fun updateMatchingEngineId(ouid: String, matchingEngineId: Long): Order? {
        val order = loadForUpdate(ouid) ?: return null
        order.matchingEngineId = matchingEngineId
        return save(order)
    }
}
