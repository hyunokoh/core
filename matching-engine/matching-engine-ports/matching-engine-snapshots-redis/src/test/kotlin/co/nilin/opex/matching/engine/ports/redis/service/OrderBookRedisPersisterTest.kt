package co.nilin.opex.matching.engine.ports.redis.service

import co.nilin.opex.matching.engine.core.model.MatchConstraint
import co.nilin.opex.matching.engine.core.model.OrderDirection
import co.nilin.opex.matching.engine.core.model.OrderType
import co.nilin.opex.matching.engine.core.model.Pair
import co.nilin.opex.matching.engine.core.model.PersistentOrder
import co.nilin.opex.matching.engine.core.model.PersistentOrderBook
import co.nilin.opex.matching.engine.ports.redis.config.RedisConfig
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.AfterAll
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.BeforeAll
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory
import org.testcontainers.containers.GenericContainer

class OrderBookRedisPersisterTest {

    private lateinit var connectionFactory: LettuceConnectionFactory
    private lateinit var persister: OrderBookRedisPersister

    @BeforeEach
    fun setUp() {
        connectionFactory = LettuceConnectionFactory(redis.host, redis.getMappedPort(6379))
        connectionFactory.afterPropertiesSet()
        persister = OrderBookRedisPersister(RedisConfig().snapshotRedisTemplate(connectionFactory))
    }

    @AfterEach
    fun tearDown() {
        connectionFactory.destroy()
    }

    @Test
    fun givenOrderBook_whenStoreAndLoadLastState_thenRoundTripThroughRedis(): Unit = runBlocking {
        val firstOrder = PersistentOrder(
            id = 101,
            ouid = "redis-snapshot-order-1",
            uuid = "snapshot-owner-1",
            price = 20_000,
            quantity = 3,
            matchConstraint = MatchConstraint.GTC,
            orderType = OrderType.LIMIT_ORDER,
            direction = OrderDirection.BID,
            filledQuantity = 1,
            clientOrderId = "snapshot-client-1"
        )
        val secondOrder = PersistentOrder(
            id = 102,
            ouid = "redis-snapshot-order-2",
            uuid = "snapshot-owner-2",
            price = 20_100,
            quantity = 5,
            matchConstraint = MatchConstraint.IOC_BUDGET,
            orderType = OrderType.MARKET_ORDER,
            direction = OrderDirection.ASK,
            filledQuantity = 2
        )
        val orderBook = PersistentOrderBook(Pair("BTC", "USDT")).apply {
            tradeCounter = 77
            lastOrder = secondOrder
            orders = listOf(firstOrder, secondOrder)
            processedOrderOuids = setOf("redis-snapshot-order-1", "redis-snapshot-order-2")
            processedCancelOuids = setOf("redis-snapshot-cancel-1")
        }

        persister.storeLastState(orderBook)

        val loaded = persister.loadLastState("BTC_USDT")

        assertNotNull(loaded)
        assertEquals("BTC_USDT", loaded!!.pair.toString())
        assertEquals(77, loaded.tradeCounter)
        assertEquals(setOf("redis-snapshot-order-1", "redis-snapshot-order-2"), loaded.processedOrderOuids)
        assertEquals(setOf("redis-snapshot-cancel-1"), loaded.processedCancelOuids)
        assertEquals(2, loaded.orders!!.size)
        assertEquals(firstOrder.ouid, loaded.orders!![0].ouid)
        assertEquals(firstOrder.clientOrderId, loaded.orders!![0].clientOrderId)
        assertEquals(firstOrder.filledQuantity, loaded.orders!![0].filledQuantity)
        assertEquals(secondOrder.ouid, loaded.lastOrder!!.ouid)
        assertEquals(secondOrder.matchConstraint, loaded.lastOrder!!.matchConstraint)
        assertEquals(secondOrder.orderType, loaded.lastOrder!!.orderType)
        assertEquals(secondOrder.direction, loaded.lastOrder!!.direction)
    }

    @Test
    fun givenMissingSnapshot_whenLoadLastState_thenReturnNull(): Unit = runBlocking {
        val loaded = persister.loadLastState("MISSING_USDT")

        assertNull(loaded)
    }

    companion object {
        private class RedisContainer(image: String) : GenericContainer<RedisContainer>(image)

        private val redis = RedisContainer("redis:7-alpine")
            .withExposedPorts(6379)

        @JvmStatic
        @BeforeAll
        fun startRedis() {
            redis.start()
        }

        @JvmStatic
        @AfterAll
        fun stopRedis() {
            redis.stop()
        }
    }
}
