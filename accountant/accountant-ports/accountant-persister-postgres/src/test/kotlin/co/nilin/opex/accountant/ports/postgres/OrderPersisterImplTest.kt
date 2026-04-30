package co.nilin.opex.accountant.ports.postgres

import co.nilin.opex.accountant.ports.postgres.dao.OrderRepository
import co.nilin.opex.accountant.ports.postgres.impl.OrderPersisterImpl
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.reactor.awaitSingleOrNull
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired

class OrderPersisterImplTest : AccountantPostgresIntegrationTest() {

    @Autowired
    private lateinit var repository: OrderRepository

    private val persister by lazy { OrderPersisterImpl(repository) }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        repository.deleteAll().awaitSingleOrNull()
    }

    @Test
    fun givenOUID_whenLoading_resultNotNull(): Unit = runBlocking {
        repository.save(Valid.orderModel.copy(id = null)).awaitSingle()

        val order = persister.load(Valid.orderModel.ouid)

        assertThat(order).isNotNull
    }

    @Test
    fun givenOUID_whenLoading_resultIsValidOrder(): Unit = runBlocking {
        repository.save(Valid.orderModel.copy(id = null)).awaitSingle()

        val order = persister.load(Valid.orderModel.ouid)!!

        assertThat(order.status).isEqualTo(Valid.orderModel.status)
        assertThat(order.matchingEngineId).isEqualTo(Valid.orderModel.matchingEngineId)
        assertThat(order.direction).isEqualTo(Valid.orderModel.direction)
        assertThat(order.filledQuantity).isEqualTo(Valid.orderModel.filledQuantity)
        assertThat(order.ouid).isEqualTo(Valid.orderModel.ouid)
    }

    @Test
    fun givenNewOrder_whenSaving_persistsAndReturnsValidOrder(): Unit = runBlocking {
        val savedOrder = persister.save(Valid.order.copy(id = null))
        val loadedModel = repository.findByOuid(Valid.order.ouid).awaitSingle()

        assertThat(savedOrder).isNotNull
        assertThat(savedOrder.status).isEqualTo(Valid.order.status)
        assertThat(savedOrder.matchingEngineId).isEqualTo(Valid.order.matchingEngineId)
        assertThat(savedOrder.direction).isEqualTo(Valid.order.direction)
        assertThat(savedOrder.filledQuantity).isEqualTo(Valid.order.filledQuantity)
        assertThat(loadedModel.ouid).isEqualTo(Valid.order.ouid)
        assertThat(loadedModel.status).isEqualTo(Valid.order.status)
    }
}
