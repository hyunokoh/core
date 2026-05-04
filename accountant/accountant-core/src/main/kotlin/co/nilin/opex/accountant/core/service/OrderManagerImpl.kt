package co.nilin.opex.accountant.core.service

import co.nilin.opex.accountant.core.api.OrderManager
import co.nilin.opex.accountant.core.inout.OrderStatus
import co.nilin.opex.accountant.core.inout.RichOrder
import co.nilin.opex.accountant.core.inout.RichOrderUpdate
import co.nilin.opex.accountant.core.model.*
import co.nilin.opex.accountant.core.spi.*
import co.nilin.opex.matching.engine.core.eventh.events.*
import co.nilin.opex.matching.engine.core.inout.RequestedOperation
import co.nilin.opex.matching.engine.core.model.OrderDirection
import org.slf4j.LoggerFactory
import org.springframework.transaction.annotation.Transactional
import java.math.BigDecimal
import java.time.LocalDateTime

open class OrderManagerImpl(
    private val pairConfigLoader: PairConfigLoader,
    private val userLevelLoader: UserLevelLoader,
    private val financialActionPersister: FinancialActionPersister,
    private val financeActionLoader: FinancialActionLoader,
    private val orderPersister: OrderPersister,
    private val tempEventPersister: TempEventPersister,
    private val richOrderPublisher: RichOrderPublisher,
    private val financialActionPublisher: FinancialActionPublisher,
    private val jsonMapper: JsonMapper,
    private val processedEventPersister: ProcessedEventPersister
) : OrderManager {

    private val logger = LoggerFactory.getLogger(OrderManagerImpl::class.java)

    @Transactional
    override suspend fun handleRequestOrder(submitOrderEvent: SubmitOrderEvent): List<FinancialAction> {
        return OrderEventLocks.withOrderLock(submitOrderEvent.ouid) {
            handleRequestOrderLocked(submitOrderEvent)
        }
    }

    private suspend fun handleRequestOrderLocked(submitOrderEvent: SubmitOrderEvent): List<FinancialAction> {
        if (orderPersister.load(submitOrderEvent.ouid) != null)
            return emptyList()

        //pair + dir -> symbol
        //user level?
        //pair config.makerFee and takerFee
        val symbol = if (submitOrderEvent.direction == OrderDirection.ASK) {
            submitOrderEvent.pair.leftSideName
        } else {
            submitOrderEvent.pair.rightSideName
        }

        val level = userLevelLoader.load(submitOrderEvent.uuid)
        val pairFeeConfig = pairConfigLoader.load(
            submitOrderEvent.pair.toString(),
            submitOrderEvent.direction,
            level
        )
        val makerFee = pairFeeConfig.makerFee * BigDecimal.ONE //user level formula
        val takerFee = pairFeeConfig.takerFee * BigDecimal.ONE //user level formula

        //create fa for transfer uuid symbol main wallet to uuid symbol exchange wallet
        /*
        amount for sell (ask): quantity
        amount for buy (bid): quantity * price
         */

        val amount = if (submitOrderEvent.direction == OrderDirection.ASK) {
            BigDecimal(submitOrderEvent.quantity).multiply(pairFeeConfig.pairConfig.leftSideFraction)
        } else {
            BigDecimal(submitOrderEvent.quantity).multiply(pairFeeConfig.pairConfig.leftSideFraction)
                .multiply(submitOrderEvent.price.toBigDecimal())
                .multiply(pairFeeConfig.pairConfig.rightSideFraction)
        }

        //store order (ouid, uuid, fees, userlevel, pair, direction, price, quantity, filledQ, status, transfered)
        orderPersister.save(
            Order(
                submitOrderEvent.pair.toString(),
                submitOrderEvent.ouid,
                null,
                makerFee,
                takerFee,
                pairFeeConfig.pairConfig.leftSideFraction,
                pairFeeConfig.pairConfig.rightSideFraction,
                submitOrderEvent.uuid,
                submitOrderEvent.userLevel,
                submitOrderEvent.direction,
                submitOrderEvent.matchConstraint,
                submitOrderEvent.orderType,
                submitOrderEvent.price,
                submitOrderEvent.quantity,
                submitOrderEvent.quantity - submitOrderEvent.remainedQuantity,
                submitOrderEvent.price.toBigDecimal()
                    .multiply(pairFeeConfig.pairConfig.rightSideFraction),
                submitOrderEvent.quantity.toBigDecimal()
                    .multiply(pairFeeConfig.pairConfig.leftSideFraction),
                BigDecimal(submitOrderEvent.quantity - submitOrderEvent.remainedQuantity).multiply(pairFeeConfig.pairConfig.leftSideFraction),
                amount,
                amount,
                OrderStatus.REQUESTED.code
            )
        )
        val financialAction = FinancialAction(
            null,
            SubmitOrderEvent::class.simpleName!!,
            submitOrderEvent.ouid,
            symbol,
            amount,
            submitOrderEvent.uuid,
            WalletType.MAIN,
            submitOrderEvent.uuid,
            WalletType.EXCHANGE,
            LocalDateTime.now(),
            FinancialActionCategory.ORDER_CREATE
        )

        return financialActionPersister.persist(listOf(financialAction)).also {
            replayDeferredOrderEvents(submitOrderEvent.ouid)
        }
        /*publishFinancialAction(financialAction)
        return fa*/
    }

    @Transactional
    override suspend fun handleNewOrder(createOrderEvent: CreateOrderEvent): List<FinancialAction> {
        return OrderEventLocks.withOrderLock(createOrderEvent.ouid) {
            handleNewOrderLocked(createOrderEvent)
        }
    }

    private suspend fun handleNewOrderLocked(createOrderEvent: CreateOrderEvent): List<FinancialAction> {
        //update order add id to other fields
        val order = orderPersister.load(createOrderEvent.ouid)
        if (order != null) {
            if (order.matchingEngineId == createOrderEvent.orderId) {
                tempEventPersister.removeTempEvent(createOrderEvent.ouid, createOrderEvent)
                return emptyList()
            }
            val updatedOrder = orderPersister.updateMatchingEngineId(createOrderEvent.ouid, createOrderEvent.orderId)
                ?: order.apply { matchingEngineId = createOrderEvent.orderId }
            //new order accepted by engine
            val publishStatus = OrderStatus.fromCode(updatedOrder.status)
            val publishRemainedQuantity = if (publishStatus == OrderStatus.REQUESTED) {
                createOrderEvent.remainedQuantity.toBigDecimal()
            } else {
                updatedOrder.quantity.toBigDecimal().subtract(updatedOrder.filledQuantity.toBigDecimal())
            }
            publishRichOrder(
                updatedOrder,
                publishRemainedQuantity,
                if (publishStatus == OrderStatus.REQUESTED) null else publishStatus
            )
            tempEventPersister.removeTempEvent(createOrderEvent.ouid, createOrderEvent)
        } else {
            tempEventPersister.saveTempEvent(createOrderEvent.ouid, createOrderEvent)
        }
        return emptyList()
    }

    override suspend fun handleUpdateOrder(updatedOrderEvent: UpdatedOrderEvent): List<FinancialAction> {
        return OrderEventLocks.withOrderLock(updatedOrderEvent.ouid) {
            handleUpdateOrderLocked(updatedOrderEvent)
        }
    }

    private suspend fun handleUpdateOrderLocked(updatedOrderEvent: UpdatedOrderEvent): List<FinancialAction> {
        val order = orderPersister.load(updatedOrderEvent.ouid)
        if (order == null) {
            tempEventPersister.saveTempEvent(updatedOrderEvent.ouid, updatedOrderEvent)
            return emptyList()
        }
        if (!isUpdateEventForOrder(updatedOrderEvent, order)) {
            logger.warn("Inconsistent update order event ignored: ouid={}", updatedOrderEvent.ouid)
            tempEventPersister.removeTempEvent(updatedOrderEvent.ouid, updatedOrderEvent)
            return emptyList()
        }

        if (order.matchingEngineId == updatedOrderEvent.orderId &&
            order.price == updatedOrderEvent.price &&
            order.quantity == updatedOrderEvent.quantity
        ) {
            tempEventPersister.removeTempEvent(updatedOrderEvent.ouid, updatedOrderEvent)
            return emptyList()
        }

        if (order.price != updatedOrderEvent.oldPrice || order.quantity != updatedOrderEvent.oldQuantity) {
            logger.warn(
                "Stale update order event ignored: ouid={}, currentPrice={}, currentQuantity={}, eventOldPrice={}, eventOldQuantity={}",
                updatedOrderEvent.ouid,
                order.price,
                order.quantity,
                updatedOrderEvent.oldPrice,
                updatedOrderEvent.oldQuantity
            )
            tempEventPersister.removeTempEvent(updatedOrderEvent.ouid, updatedOrderEvent)
            return emptyList()
        }

        val filledQuantity = updatedOrderEvent.oldQuantity - updatedOrderEvent.remainedQuantity
        val newRemainedQuantity = updatedOrderEvent.quantity - filledQuantity
        val newRemainedTransferAmount = reserveAmount(
            updatedOrderEvent.direction,
            updatedOrderEvent.price,
            newRemainedQuantity,
            order.leftSideFraction,
            order.rightSideFraction
        )
        val delta = newRemainedTransferAmount.subtract(order.remainedTransferAmount)

        val updatedStatus = if (newRemainedQuantity == 0L) {
            OrderStatus.FILLED
        } else if (filledQuantity == 0L) {
            OrderStatus.NEW
        } else {
            OrderStatus.PARTIALLY_FILLED
        }

        val updatedOrder = order.copy(
            matchingEngineId = updatedOrderEvent.orderId,
            price = updatedOrderEvent.price,
            quantity = updatedOrderEvent.quantity,
            filledQuantity = filledQuantity,
            origPrice = updatedOrderEvent.price.toBigDecimal().multiply(order.rightSideFraction),
            origQuantity = updatedOrderEvent.quantity.toBigDecimal().multiply(order.leftSideFraction),
            filledOrigQuantity = filledQuantity.toBigDecimal().multiply(order.leftSideFraction),
            firstTransferAmount = order.firstTransferAmount.add(delta),
            remainedTransferAmount = newRemainedTransferAmount,
            status = updatedStatus.code
        )
        orderPersister.save(updatedOrder)

        val financialActions = if (delta.compareTo(BigDecimal.ZERO) == 0) {
            emptyList()
        } else {
            val symbol = if (updatedOrderEvent.direction == OrderDirection.ASK) {
                updatedOrderEvent.pair.leftSideName
            } else {
                updatedOrderEvent.pair.rightSideName
            }
            val parentFinancialAction = financeActionLoader.findLast(updatedOrderEvent.uuid, updatedOrderEvent.ouid)
            val amount = delta.abs()
            val increaseReserve = delta.compareTo(BigDecimal.ZERO) > 0
            listOf(
                FinancialAction(
                    parentFinancialAction,
                    UpdatedOrderEvent::class.simpleName!!,
                    updatedOrderEvent.ouid,
                    symbol,
                    amount,
                    updatedOrderEvent.uuid,
                    if (increaseReserve) WalletType.MAIN else WalletType.EXCHANGE,
                    updatedOrderEvent.uuid,
                    if (increaseReserve) WalletType.EXCHANGE else WalletType.MAIN,
                    LocalDateTime.now(),
                    if (increaseReserve) FinancialActionCategory.ORDER_CREATE else FinancialActionCategory.ORDER_CANCEL
                )
            )
        }

        val richOrderUpdate = RichOrderUpdate(
            updatedOrder.ouid,
            updatedOrder.origPrice,
            updatedOrder.origQuantity,
            newRemainedQuantity.toBigDecimal().multiply(updatedOrder.leftSideFraction),
            updatedStatus
        )

        return financialActionPersister.persist(financialActions).also {
            richOrderPublisher.publish(richOrderUpdate)
            tempEventPersister.removeTempEvent(updatedOrderEvent.ouid, updatedOrderEvent)
        }
    }

    private suspend fun replayDeferredOrderEvents(ouid: String) {
        tempEventPersister.loadTempEvents(ouid).toList().forEach { event ->
            when (event) {
                is CreateOrderEvent -> handleNewOrderLocked(event)
                is UpdatedOrderEvent -> handleUpdateOrderLocked(event)
                is RejectOrderEvent -> handleRejectOrderLocked(event)
                is CancelOrderEvent -> handleCancelOrderLocked(event)
                else -> Unit
            }
        }
    }


    @Transactional
    override suspend fun handleRejectOrder(rejectOrderEvent: RejectOrderEvent): List<FinancialAction> {
        return OrderEventLocks.withOrderLock(rejectOrderEvent.ouid) {
            handleRejectOrderLocked(rejectOrderEvent)
        }
    }

    private suspend fun handleRejectOrderLocked(rejectOrderEvent: RejectOrderEvent): List<FinancialAction> {
        if (rejectOrderEvent.requestedOperation != RequestedOperation.PLACE_ORDER)
            return emptyList()

        //order by ouid
        val order = orderPersister.load(rejectOrderEvent.ouid)
        if (order == null) {
            tempEventPersister.saveTempEvent(rejectOrderEvent.ouid, rejectOrderEvent)
            return emptyList()
        }
        if (isTerminalOrderStatus(order.status)) {
            tempEventPersister.removeTempEvent(rejectOrderEvent.ouid, rejectOrderEvent)
            return emptyList()
        }
        if (!isRejectEventForOrder(rejectOrderEvent, order)) {
            logger.warn("Inconsistent reject order event ignored: ouid={}", rejectOrderEvent.ouid)
            tempEventPersister.removeTempEvent(rejectOrderEvent.ouid, rejectOrderEvent)
            return emptyList()
        }
        val eventType = RejectOrderEvent::class.simpleName!!
        val eventKey = rejectOrderEvent.processedEventKey()
        if (!processedEventPersister.tryMarkProcessed(eventType, eventKey)) {
            logger.info("Duplicate reject order event ignored: type=$eventType key=$eventKey")
            tempEventPersister.removeTempEvent(rejectOrderEvent.ouid, rejectOrderEvent)
            return emptyList()
        }
        val symbol = if (rejectOrderEvent.direction == OrderDirection.ASK) {
            rejectOrderEvent.pair.leftSideName
        } else {
            rejectOrderEvent.pair.rightSideName
        }
        //check uuid
        //lookup for parent fa
        val parentFinancialAction = financeActionLoader.findLast(rejectOrderEvent.uuid, rejectOrderEvent.ouid)
        //create fa for transfer remaining transfered uuid symbol exchange wallet to uuid main exchange wallet
        val financialAction = FinancialAction(
            parentFinancialAction,
            RejectOrderEvent::class.simpleName!!,
            rejectOrderEvent.ouid,
            symbol,
            order.remainedTransferAmount,
            rejectOrderEvent.uuid,
            WalletType.EXCHANGE,
            rejectOrderEvent.uuid,
            WalletType.MAIN,
            LocalDateTime.now(),
            FinancialActionCategory.ORDER_CANCEL
        )
        //update order status
        order.status = OrderStatus.REJECTED.code
        orderPersister.save(order)
        val richOrderUpdate = RichOrderUpdate(
            order.ouid,
            order.price.toBigDecimal(),
            order.quantity.toBigDecimal(),
            BigDecimal.ZERO,
            OrderStatus.REJECTED
        )
        return financialActionPersister.persist(listOf(financialAction)).also {
            richOrderPublisher.publish(richOrderUpdate)
            tempEventPersister.removeTempEvent(rejectOrderEvent.ouid, rejectOrderEvent)
        }
        /*publishFinancialAction(financialAction)
        return fa*/
    }


    @Transactional
    override suspend fun handleCancelOrder(cancelOrderEvent: CancelOrderEvent): List<FinancialAction> {
        return OrderEventLocks.withOrderLock(cancelOrderEvent.ouid) {
            handleCancelOrderLocked(cancelOrderEvent)
        }
    }

    private suspend fun handleCancelOrderLocked(cancelOrderEvent: CancelOrderEvent): List<FinancialAction> {
        //order by ouid
        val order = orderPersister.load(cancelOrderEvent.ouid)
        if (order == null) {
            tempEventPersister.saveTempEvent(cancelOrderEvent.ouid, cancelOrderEvent)
            return emptyList()
        }
        if (isTerminalOrderStatus(order.status)) {
            tempEventPersister.removeTempEvent(cancelOrderEvent.ouid, cancelOrderEvent)
            return emptyList()
        }
        val expectedFilledQuantity = cancelOrderEvent.quantity - cancelOrderEvent.remainedQuantity
        if (!isCancelEventForOrder(cancelOrderEvent, order) || order.filledQuantity > expectedFilledQuantity) {
            logger.warn("Inconsistent cancel order event ignored: ouid={}", cancelOrderEvent.ouid)
            tempEventPersister.removeTempEvent(cancelOrderEvent.ouid, cancelOrderEvent)
            return emptyList()
        }
        if (order.filledQuantity < expectedFilledQuantity) {
            tempEventPersister.saveTempEvent(cancelOrderEvent.ouid, cancelOrderEvent)
            return emptyList()
        }
        val eventType = CancelOrderEvent::class.simpleName!!
        val eventKey = cancelOrderEvent.processedEventKey()
        if (!processedEventPersister.tryMarkProcessed(eventType, eventKey)) {
            logger.info("Duplicate cancel order event ignored: type=$eventType key=$eventKey")
            tempEventPersister.removeTempEvent(cancelOrderEvent.ouid, cancelOrderEvent)
            return emptyList()
        }
        val symbol = if (cancelOrderEvent.direction == OrderDirection.ASK) {
            cancelOrderEvent.pair.leftSideName
        } else {
            cancelOrderEvent.pair.rightSideName
        }
        //check uuid
        //lookup for parent fa
        val parentFinancialAction = financeActionLoader.findLast(cancelOrderEvent.uuid, cancelOrderEvent.ouid)
        //create fa for transfer remaining transfered uuid symbol exchange wallet to uuid main exchange wallet
        val financialAction = FinancialAction(
            parentFinancialAction,
            CancelOrderEvent::class.simpleName!!,
            cancelOrderEvent.ouid,
            symbol,
            order.remainedTransferAmount,
            cancelOrderEvent.uuid,
            WalletType.EXCHANGE,
            cancelOrderEvent.uuid,
            WalletType.MAIN,
            LocalDateTime.now(),
            FinancialActionCategory.ORDER_CANCEL
        )
        //update order status
        order.status = OrderStatus.CANCELED.code
        orderPersister.save(order)
        val richOrderUpdate = RichOrderUpdate(
            order.ouid,
            order.price.toBigDecimal(),
            order.quantity.toBigDecimal(),
            cancelOrderEvent.remainedQuantity.toBigDecimal(),
            OrderStatus.CANCELED
        )
        return financialActionPersister.persist(listOf(financialAction)).also {
            richOrderPublisher.publish(richOrderUpdate)
            tempEventPersister.removeTempEvent(cancelOrderEvent.ouid, cancelOrderEvent)
        }
        /*publishFinancialAction(financialAction)
        return fa*/
    }


    private suspend fun publishRichOrder(order: Order, remainedQuantity: BigDecimal, status: OrderStatus? = null) {
        richOrderPublisher.publish(
            RichOrder(
                order.matchingEngineId ?: order.id,
                order.pair,
                order.ouid,
                order.uuid,
                order.userLevel,
                order.makerFee,
                order.takerFee,
                order.leftSideFraction,
                order.rightSideFraction,
                order.direction,
                order.matchConstraint,
                order.orderType,
                order.origPrice,
                order.origQuantity,
                order.origPrice.multiply(order.origQuantity),
                order.quantity.toBigDecimal().subtract(remainedQuantity)
                    .multiply(order.leftSideFraction),
                order.origPrice.multiply(
                    order.quantity.toBigDecimal().subtract(remainedQuantity)
                ),
                status?.code ?: if (remainedQuantity.compareTo(BigDecimal.ZERO) == 0) {
                    OrderStatus.FILLED.code
                } else if (remainedQuantity.compareTo(order.quantity.toBigDecimal()) == 0) {
                    OrderStatus.NEW.code
                } else {
                    OrderStatus.PARTIALLY_FILLED.code
                }
            )
        )
    }

    private suspend fun publishFinancialAction(financialAction: FinancialAction) {
        if (financialAction.parent != null)
            publishFinancialAction(financialAction.parent)

        if (financialAction.status == FinancialActionStatus.CREATED) {
            financialActionPublisher.publish(financialAction)
            financialActionPersister.updateStatus(financialAction.uuid, FinancialActionStatus.SENT)
        }
    }

    private fun reserveAmount(
        direction: OrderDirection,
        price: Long,
        remainedQuantity: Long,
        leftSideFraction: BigDecimal,
        rightSideFraction: BigDecimal
    ): BigDecimal {
        val baseAmount = remainedQuantity.toBigDecimal().multiply(leftSideFraction)
        return if (direction == OrderDirection.ASK) {
            baseAmount
        } else {
            baseAmount.multiply(price.toBigDecimal()).multiply(rightSideFraction)
        }
    }

    private fun isUpdateEventForOrder(event: UpdatedOrderEvent, order: Order): Boolean {
        return order.uuid == event.uuid &&
            order.pair == event.pair.toString() &&
            order.direction == event.direction &&
            (order.matchingEngineId == null || order.matchingEngineId == event.orderId)
    }

    private fun isRejectEventForOrder(event: RejectOrderEvent, order: Order): Boolean {
        return order.uuid == event.uuid &&
            order.pair == event.pair.toString() &&
            order.price == event.price &&
            order.quantity == event.quantity &&
            order.direction == event.direction &&
            order.matchConstraint == event.matchConstraint &&
            order.orderType == event.orderType
    }

    private fun isCancelEventForOrder(event: CancelOrderEvent, order: Order): Boolean {
        return order.uuid == event.uuid &&
            order.pair == event.pair.toString() &&
            (order.matchingEngineId == null || order.matchingEngineId == event.orderId) &&
            order.price == event.price &&
            order.quantity == event.quantity &&
            order.direction == event.direction &&
            order.matchConstraint == event.matchConstraint &&
            order.orderType == event.orderType
    }

    private fun isTerminalOrderStatus(statusCode: Int): Boolean {
        return when (OrderStatus.fromCode(statusCode)) {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED -> true
            else -> false
        }
    }

    private fun createMap(rejectOrderEvent: RejectOrderEvent, order: Order): Map<String, Any> {
        val orderMap: Map<String, Any> = jsonMapper.toMap(order)
        val eventMap: Map<String, Any> = jsonMapper.toMap(rejectOrderEvent)
        return orderMap + eventMap
    }

    private fun createMap(cancelOrderEvent: CancelOrderEvent, order: Order): Map<String, Any> {
        val orderMap: Map<String, Any> = jsonMapper.toMap(order)
        val eventMap: Map<String, Any> = jsonMapper.toMap(cancelOrderEvent)
        return orderMap + eventMap
    }

    private fun createMap(submitOrderEvent: SubmitOrderEvent, order: Order): Map<String, Any> {
        val orderMap: Map<String, Any> = jsonMapper.toMap(order)
        val eventMap: Map<String, Any> = jsonMapper.toMap(submitOrderEvent)
        return orderMap + eventMap
    }

    private fun RejectOrderEvent.processedEventKey(): String {
        return listOf(
            pair.toString(),
            ouid,
            uuid,
            orderId,
            price,
            quantity,
            direction,
            requestedOperation,
            reason
        ).joinToString(":")
    }

    private fun CancelOrderEvent.processedEventKey(): String {
        return listOf(
            pair.toString(),
            ouid,
            uuid,
            orderId,
            price,
            quantity,
            remainedQuantity,
            direction
        ).joinToString(":")
    }

}
