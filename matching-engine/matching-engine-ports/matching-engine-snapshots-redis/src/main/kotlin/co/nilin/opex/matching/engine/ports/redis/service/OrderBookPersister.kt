package co.nilin.opex.matching.engine.ports.redis.service

import co.nilin.opex.matching.engine.core.model.PersistentOrderBook
import co.nilin.opex.matching.engine.core.spi.OrderBookPersister
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.reactor.awaitSingleOrNull
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.data.redis.core.ReactiveRedisTemplate
import org.springframework.stereotype.Component

@Component
class OrderBookRedisPersister(
    @Qualifier("snapshotRedisTemplate")
    val redisTemplate: ReactiveRedisTemplate<String, PersistentOrderBook>
) : OrderBookPersister {

    override suspend fun storeLastState(orderBook: PersistentOrderBook) {
        redisTemplate.opsForHash<String, PersistentOrderBook>()
            .put("OrderbookSnapshots", orderBook.pair.toString(), orderBook)
            .awaitSingle()
    }

    override suspend fun loadLastState(symbol: String): PersistentOrderBook? =
        redisTemplate.opsForHash<String, PersistentOrderBook>()
            .get("OrderbookSnapshots", symbol)
            .awaitSingleOrNull()

}
