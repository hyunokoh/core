package co.nilin.opex.accountant.ports.postgres.impl

import co.nilin.opex.accountant.core.model.Order
import co.nilin.opex.accountant.core.spi.OrderPersister
import co.nilin.opex.accountant.ports.postgres.dao.OrderRepository
import co.nilin.opex.accountant.ports.postgres.model.OrderModel
import kotlinx.coroutines.reactive.awaitFirstOrNull
import org.springframework.stereotype.Component
import java.time.LocalDateTime

@Component
class OrderPersisterImpl(private val orderRepository: OrderRepository) : OrderPersister {

    override suspend fun load(ouid: String): Order? {
        val model = orderRepository.findByOuid(ouid).awaitFirstOrNull() ?: return null
        return model.toOrder()
    }

    override suspend fun loadForUpdate(ouid: String): Order? {
        val model = orderRepository.findByOuidForUpdate(ouid).awaitFirstOrNull() ?: return null
        return model.toOrder()
    }

    private fun OrderModel.toOrder(): Order {
        return Order(
            pair,
            ouid,
            matchingEngineId,
            makerFee,
            takerFee,
            leftSideFraction,
            rightSideFraction,
            uuid,
            userLevel,
            direction,
            matchConstraint,
            orderType,
            price,
            quantity,
            filledQuantity,
            origPrice,
            origQuantity,
            filledOrigQuantity,
            firstTransferAmount,
            remainedTransferAmount,
            status,
            id,
            accumulativeQuoteQty,
            clientOrderId
        )
    }

    override suspend fun save(order: Order): Order {
        orderRepository.save(
            OrderModel(
                order.id,
                order.ouid,
                order.uuid,
                order.clientOrderId,
                order.pair,
                order.matchingEngineId,
                order.makerFee,
                order.takerFee,
                order.leftSideFraction,
                order.rightSideFraction,
                order.userLevel,
                order.direction,
                order.matchConstraint,
                order.orderType,
                order.price,
                order.quantity,
                order.filledQuantity,
                order.origPrice,
                order.origQuantity,
                order.filledOrigQuantity,
                order.firstTransferAmount,
                order.remainedTransferAmount,
                order.accumulativeQuoteQty,
                order.status,
                "",
                "",
                LocalDateTime.now()
            )
        ).awaitFirstOrNull()
        return order
    }

    override suspend fun updateMatchingEngineId(ouid: String, matchingEngineId: Long): Order? {
        orderRepository.updateMatchingEngineId(ouid, matchingEngineId).awaitFirstOrNull()
        return load(ouid)
    }
}
