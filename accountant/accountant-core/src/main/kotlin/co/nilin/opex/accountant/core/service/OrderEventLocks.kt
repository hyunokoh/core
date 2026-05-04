package co.nilin.opex.accountant.core.service

import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.util.concurrent.ConcurrentHashMap

internal object OrderEventLocks {
    private val orderLocks = ConcurrentHashMap<String, Mutex>()

    suspend fun <T> withOrderLock(ouid: String, block: suspend () -> T): T {
        return orderLocks.computeIfAbsent(ouid) { Mutex() }.withLock {
            block()
        }
    }

    suspend fun <T> withOrderLocks(ouids: Collection<String>, block: suspend () -> T): T {
        val locks = ouids.distinct().sorted().map { orderLocks.computeIfAbsent(it) { Mutex() } }
        return withLocks(locks, block)
    }

    private suspend fun <T> withLocks(locks: List<Mutex>, block: suspend () -> T): T {
        if (locks.isEmpty())
            return block()

        return locks.first().withLock {
            withLocks(locks.drop(1), block)
        }
    }
}
