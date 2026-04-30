package co.nilin.opex.market.ports.postgres.impl

import co.nilin.opex.market.core.inout.OrderStatus
import co.nilin.opex.market.ports.postgres.MarketPostgresIntegrationTest
import co.nilin.opex.market.ports.postgres.dao.OpenOrderRepository
import co.nilin.opex.market.ports.postgres.dao.OrderRepository
import co.nilin.opex.market.ports.postgres.dao.OrderStatusRepository
import co.nilin.opex.market.ports.postgres.impl.sample.VALID
import co.nilin.opex.market.ports.postgres.util.RedisCacheHelper
import kotlinx.coroutines.reactive.awaitFirstOrNull
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.r2dbc.core.DatabaseClient

private class OrderPersisterTest : MarketPostgresIntegrationTest() {
    @Autowired
    private lateinit var databaseClient: DatabaseClient

    @Autowired
    private lateinit var orderRepository: OrderRepository

    @Autowired
    private lateinit var orderStatusRepository: OrderStatusRepository

    @Autowired
    private lateinit var openOrderRepository: OpenOrderRepository

    @Autowired
    private lateinit var redisCacheHelper: RedisCacheHelper

    private val orderPersister by lazy {
        OrderPersisterImpl(orderRepository, orderStatusRepository, openOrderRepository, redisCacheHelper)
    }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        databaseClient.executeSql("truncate table open_orders, order_status, orders restart identity cascade")
    }

    @Test
    fun givenRichOrder_whenSave_thenPersistOrderStatusAndOpenOrder(): Unit = runBlocking {
        orderPersister.save(VALID.RICH_ORDER)

        val order = orderRepository.findByOuid(VALID.RICH_ORDER.ouid).awaitSingle()
        val status = orderStatusRepository.findMostRecentByOUID(VALID.RICH_ORDER.ouid).awaitSingle()
        val openOrder = openOrderRepository.findAll().awaitFirstOrNull()

        assertThat(order.symbol).isEqualTo(VALID.ETH_USDT)
        assertThat(status.status).isEqualTo(OrderStatus.NEW.code)
        assertThat(openOrder?.ouid).isEqualTo(VALID.RICH_ORDER.ouid)
    }

    @Test
    fun givenDuplicateRichOrder_whenSaveAgain_thenPersistOnceAndKeepOpenOrder(): Unit = runBlocking {
        orderPersister.save(VALID.RICH_ORDER)
        orderPersister.save(VALID.RICH_ORDER)

        val orders = orderRepository.findAll().collectList().awaitSingle()
        val openOrders = openOrderRepository.findAll().collectList().awaitSingle()

        assertThat(orders).hasSize(1)
        assertThat(openOrders).hasSize(1)
        assertThat(openOrders.single().ouid).isEqualTo(VALID.RICH_ORDER.ouid)
    }

    @Test
    fun givenOpenOrder_whenUpdateToFilled_thenRemoveFromOpenOrders(): Unit = runBlocking {
        orderPersister.save(VALID.RICH_ORDER)
        orderPersister.update(VALID.RICH_ORDER_UPDATE.copy(status = OrderStatus.FILLED))
        orderPersister.save(VALID.RICH_ORDER)

        val status = orderStatusRepository.findMostRecentByOUID(VALID.RICH_ORDER.ouid).awaitSingle()
        val openOrder = openOrderRepository.findAll().awaitFirstOrNull()

        assertThat(status.status).isEqualTo(OrderStatus.FILLED.code)
        assertThat(openOrder).isNull()
    }
}
