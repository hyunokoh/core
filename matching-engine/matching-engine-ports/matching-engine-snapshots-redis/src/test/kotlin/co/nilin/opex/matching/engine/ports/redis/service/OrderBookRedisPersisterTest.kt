package co.nilin.opex.matching.engine.ports.redis.service

import co.nilin.opex.matching.engine.core.model.Pair
import co.nilin.opex.matching.engine.core.model.PersistentOrderBook
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertSame
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test
import org.mockito.Mockito.mock
import org.mockito.Mockito.verify
import org.mockito.Mockito.`when`
import org.springframework.data.redis.core.ReactiveHashOperations
import org.springframework.data.redis.core.ReactiveRedisTemplate
import reactor.core.publisher.Mono

class OrderBookRedisPersisterTest {

    private val redisTemplate = mock(ReactiveRedisTemplate::class.java) as ReactiveRedisTemplate<String, PersistentOrderBook>
    private val hashOperations = mock(ReactiveHashOperations::class.java)
        as ReactiveHashOperations<String, String, PersistentOrderBook>
    private val persister = OrderBookRedisPersister(redisTemplate)

    @Test
    fun givenOrderBook_whenStoreLastState_thenAwaitRedisWrite(): Unit = runBlocking {
        val orderBook = PersistentOrderBook(Pair("BTC", "USDT"))
        `when`(redisTemplate.opsForHash<String, PersistentOrderBook>()).thenReturn(hashOperations)
        `when`(hashOperations.put("OrderbookSnapshots", "BTC_USDT", orderBook)).thenReturn(Mono.just(true))

        persister.storeLastState(orderBook)

        verify(hashOperations).put("OrderbookSnapshots", "BTC_USDT", orderBook)
    }

    @Test
    fun givenRedisWriteFailure_whenStoreLastState_thenPropagateFailure() {
        val orderBook = PersistentOrderBook(Pair("BTC", "USDT"))
        val failure = IllegalStateException("redis unavailable")
        `when`(redisTemplate.opsForHash<String, PersistentOrderBook>()).thenReturn(hashOperations)
        `when`(hashOperations.put("OrderbookSnapshots", "BTC_USDT", orderBook)).thenReturn(Mono.error(failure))

        assertThrows(IllegalStateException::class.java) {
            runBlocking { persister.storeLastState(orderBook) }
        }
    }

    @Test
    fun givenExistingSnapshot_whenLoadLastState_thenReturnSnapshot(): Unit = runBlocking {
        val orderBook = PersistentOrderBook(Pair("ETH", "USDT"))
        `when`(redisTemplate.opsForHash<String, PersistentOrderBook>()).thenReturn(hashOperations)
        `when`(hashOperations.get("OrderbookSnapshots", "ETH_USDT")).thenReturn(Mono.just(orderBook))

        val loaded = persister.loadLastState("ETH_USDT")

        assertSame(orderBook, loaded)
    }

    @Test
    fun givenMissingSnapshot_whenLoadLastState_thenReturnNull(): Unit = runBlocking {
        `when`(redisTemplate.opsForHash<String, PersistentOrderBook>()).thenReturn(hashOperations)
        `when`(hashOperations.get("OrderbookSnapshots", "ETH_USDT")).thenReturn(Mono.empty())

        val loaded = persister.loadLastState("ETH_USDT")

        assertNull(loaded)
    }
}
