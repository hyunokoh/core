package co.nilin.opex.accountant.core.service

import co.nilin.opex.accountant.core.inout.OrderStatus
import co.nilin.opex.accountant.core.inout.RichOrder
import co.nilin.opex.accountant.core.inout.RichOrderUpdate
import co.nilin.opex.accountant.core.model.*
import co.nilin.opex.matching.engine.core.eventh.events.CancelOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.CreateOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.RejectOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.SubmitOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.UpdatedOrderEvent
import co.nilin.opex.matching.engine.core.inout.RejectReason
import co.nilin.opex.matching.engine.core.inout.RequestedOperation
import co.nilin.opex.matching.engine.core.model.MatchConstraint
import co.nilin.opex.matching.engine.core.model.OrderDirection
import co.nilin.opex.matching.engine.core.model.OrderType
import co.nilin.opex.matching.engine.core.model.Pair
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import java.math.BigDecimal

internal class OrderManagerImplTest {

    private val financialActionStore = RecordingFinancialActionStore()
    private val orderPersister = InMemoryOrderPersister()
    private val tempEventPersister = InMemoryTempEventPersister()
    private val pairConfigLoader = MapPairConfigLoader()
    private val richOrderPublisher = RecordingRichOrderPublisher()
    private val userLevelLoader = StaticUserLevelLoader()
    private val financialActionPublisher = RecordingFinancialActionPublisher()
    private val processedEventPersister = RecordingProcessedEventPersister()

    private val orderManager = OrderManagerImpl(
        pairConfigLoader,
        userLevelLoader,
        financialActionStore,
        financialActionStore,
        orderPersister,
        tempEventPersister,
        richOrderPublisher,
        financialActionPublisher,
        JsonMapperTestImpl(),
        processedEventPersister
    )

    @Test
    fun givenAskOrder_whenHandleRequestOrder_thenFAMatch(): Unit = runBlocking {
        //given
        val pair = Pair("ETH", "BTC")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.valueOf(1.0),
            BigDecimal.valueOf(0.001)
        )
        val submitOrderEvent = SubmitOrderEvent(
            "ouid", "uuid", null, pair, 30, 60, 0, OrderDirection.ASK, MatchConstraint.GTC, OrderType.LIMIT_ORDER,
            clientOrderId = "client-1"
        )

        pairConfigLoader.put(
            pairConfig,
            submitOrderEvent.direction,
            "",
            BigDecimal.valueOf(0.1),
            BigDecimal.valueOf(0.12)
        )

        //when
        val financialActions = orderManager.handleRequestOrder(submitOrderEvent)

        //then
        assertThat(financialActions.size).isEqualTo(1)
        val expectedFinancialAction = FinancialAction(
            null,
            SubmitOrderEvent::class.simpleName!!,
            submitOrderEvent.ouid,
            pair.leftSideName,
            pairConfig.leftSideFraction.multiply(submitOrderEvent.quantity.toBigDecimal()),
            submitOrderEvent.uuid,
            WalletType.MAIN,
            submitOrderEvent.uuid,
            WalletType.EXCHANGE,
            Valid.currentTime,
            FinancialActionCategory.ORDER_CREATE
        )

        with(expectedFinancialAction) {
            assertThat(eventType).isEqualTo(financialActions[0].eventType)
            assertThat(symbol).isEqualTo(financialActions[0].symbol)
            assertThat(amount).isEqualTo(financialActions[0].amount)
            assertThat(sender).isEqualTo(financialActions[0].sender)
            assertThat(senderWalletType).isEqualTo(financialActions[0].senderWalletType)
            assertThat(receiver).isEqualTo(financialActions[0].receiver)
            assertThat(receiverWalletType).isEqualTo(financialActions[0].receiverWalletType)
        }
        assertThat(orderPersister.orders.getValue(submitOrderEvent.ouid).clientOrderId).isEqualTo("client-1")
    }

    @Test
    fun givenBidOrder_whenHandleRequestOrder_thenFAMatch(): Unit = runBlocking {
        //given
        val pair = Pair("eth", "btc")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.valueOf(1.0),
            BigDecimal.valueOf(0.001)
        )
        val submitOrderEvent = SubmitOrderEvent(
            "ouid", "uuid", null, pair, 35, 14, 0, OrderDirection.BID, MatchConstraint.GTC, OrderType.LIMIT_ORDER
        )

        pairConfigLoader.put(
            pairConfig,
            submitOrderEvent.direction,
            "",
            BigDecimal.valueOf(0.08),
            BigDecimal.valueOf(0.1)
        )

        //when
        val financialActions = orderManager.handleRequestOrder(submitOrderEvent)

        //then
        assertThat(financialActions.size).isEqualTo(1)
        val expectedFinancialAction = FinancialAction(
            null,
            SubmitOrderEvent::class.simpleName!!,
            submitOrderEvent.ouid,
            pair.rightSideName,
            pairConfig.leftSideFraction.multiply(submitOrderEvent.quantity.toBigDecimal())
                .multiply(pairConfig.rightSideFraction)
                .multiply(submitOrderEvent.price.toBigDecimal()),
            submitOrderEvent.uuid,
            WalletType.MAIN,
            submitOrderEvent.uuid,
            WalletType.EXCHANGE,
            Valid.currentTime,
            FinancialActionCategory.ORDER_CREATE
        )
        with(expectedFinancialAction) {
            assertThat(eventType).isEqualTo(financialActions[0].eventType)
            assertThat(symbol).isEqualTo(financialActions[0].symbol)
            assertThat(amount).isEqualTo(financialActions[0].amount)
            assertThat(sender).isEqualTo(financialActions[0].sender)
            assertThat(senderWalletType).isEqualTo(financialActions[0].senderWalletType)
            assertThat(receiver).isEqualTo(financialActions[0].receiver)
            assertThat(receiverWalletType).isEqualTo(financialActions[0].receiverWalletType)
        }
        assertThat(financialActions[0].category).isEqualTo(FinancialActionCategory.ORDER_CREATE)

    }

    @Test
    fun givenDuplicateSubmitOrderEvent_whenHandleRequestOrder_thenReturnNoFinancialActions(): Unit = runBlocking {
        val pair = Pair("ETH", "BTC")
        val submitOrderEvent = SubmitOrderEvent(
            "duplicate_ouid", "uuid", null, pair, 30, 60, 0, OrderDirection.ASK, MatchConstraint.GTC, OrderType.LIMIT_ORDER
        )
        val existingOrder = Valid.order.copy(ouid = submitOrderEvent.ouid, uuid = submitOrderEvent.uuid)
        orderPersister.orders[submitOrderEvent.ouid] = existingOrder

        val financialActions = orderManager.handleRequestOrder(submitOrderEvent)

        assertThat(financialActions).isEmpty()
        assertThat(orderPersister.orders).hasSize(1)
        assertThat(financialActionStore.persisted).isEmpty()
    }

    @Test
    fun givenNewOrderEventReceived_whenUpdatingOrder_matchingEngineIdMatch(): Unit = runBlocking {
        val orderEvent = CreateOrderEvent(
            "order_ouid",
            "user_1",
            55,
            Pair("BTC", "USDT"),
            100000,
            1000,
            0,
            OrderDirection.BID
        )

        val order = Valid.order.copy(ouid = orderEvent.ouid)
        orderPersister.orders[orderEvent.ouid] = order

        val fa = orderManager.handleNewOrder(orderEvent)

        assertThat(fa.size).isEqualTo(0)
        assertThat(order.matchingEngineId).isEqualTo(55)
        assertThat(richOrderPublisher.published).hasSize(1)
        assertThat((richOrderPublisher.published.single() as RichOrder).orderId).isEqualTo(55)
    }

    @Test
    fun givenNewOrderEventReceivedAfterTradeStateChanged_whenUpdatingMatchingId_thenPreserveTradeState(): Unit =
        runBlocking {
            val orderEvent = CreateOrderEvent(
                "partially_filled_ouid",
                "user_1",
                56,
                Pair("BTC", "USDT"),
                100000,
                1000,
                500,
                OrderDirection.BID
            )
            val order = Valid.order.copy(
                ouid = orderEvent.ouid,
                filledQuantity = 500,
                remainedTransferAmount = BigDecimal.valueOf(50),
                status = OrderStatus.PARTIALLY_FILLED.code
            )
            orderPersister.orders[orderEvent.ouid] = order

            val fa = orderManager.handleNewOrder(orderEvent)

            val persistedOrder = orderPersister.orders.getValue(orderEvent.ouid)
            assertThat(fa).isEmpty()
            assertThat(persistedOrder.matchingEngineId).isEqualTo(56)
            assertThat(persistedOrder.filledQuantity).isEqualTo(500)
            assertThat(persistedOrder.remainedTransferAmount).isEqualByComparingTo(BigDecimal.valueOf(50))
            assertThat(persistedOrder.status).isEqualTo(OrderStatus.PARTIALLY_FILLED.code)
            val richOrder = richOrderPublisher.published.single() as RichOrder
            assertThat(richOrder.status).isEqualTo(OrderStatus.PARTIALLY_FILLED.code)
            assertThat(richOrder.executedQuantity).isEqualByComparingTo(BigDecimal("0.0005000"))
        }

    @Test
    fun givenNewOrderEventDeferred_whenRequestOrderArrives_thenReplayTempEvent(): Unit = runBlocking {
        val pair = Pair("BTC", "USDT")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.valueOf(1.0),
            BigDecimal.valueOf(0.01)
        )
        val submitOrderEvent = SubmitOrderEvent(
            "deferred_ouid",
            "user_1",
            null,
            pair,
            100000,
            1000,
            1000,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        val createOrderEvent = CreateOrderEvent(
            submitOrderEvent.ouid,
            submitOrderEvent.uuid,
            77,
            pair,
            submitOrderEvent.price,
            submitOrderEvent.quantity,
            submitOrderEvent.remainedQuantity,
            submitOrderEvent.direction
        )
        pairConfigLoader.put(
            pairConfig,
            submitOrderEvent.direction,
            "",
            BigDecimal.valueOf(0.1),
            BigDecimal.valueOf(0.12)
        )
        tempEventPersister.saveTempEvent(createOrderEvent.ouid, createOrderEvent)

        val financialActions = orderManager.handleRequestOrder(submitOrderEvent)

        assertThat(financialActions).hasSize(1)
        assertThat(orderPersister.orders.getValue(submitOrderEvent.ouid).matchingEngineId).isEqualTo(77)
        assertThat(richOrderPublisher.published).hasSize(1)
        assertThat((richOrderPublisher.published.single() as RichOrder).orderId).isEqualTo(77)
        assertThat(tempEventPersister.loadTempEvents(submitOrderEvent.ouid)).isEmpty()
    }

    @Test
    fun givenNewOrderEventReceived_whenLocalOrderNull_saveTempEvent(): Unit = runBlocking {
        val orderEvent = CreateOrderEvent(
            "order_ouid",
            "user_1",
            55,
            Pair("BTC", "USDT"),
            100000,
            1000,
            0,
            OrderDirection.BID
        )

        val fa = orderManager.handleNewOrder(orderEvent)

        assertThat(fa.size).isEqualTo(0)
        assertThat(tempEventPersister.saved).hasSize(1)
        assertThat(tempEventPersister.saved[0].ouid).isEqualTo(orderEvent.ouid)
    }

    @Test
    fun givenUpdateOrderIncreasesBidReserve_whenLocalFound_persistAdditionalReserveFA(): Unit = runBlocking {
        val orderEvent = UpdatedOrderEvent(
            "order_ouid",
            "user_id",
            88,
            Pair("BTC", "USDT"),
            100000,
            1000,
            120000,
            1000,
            1000,
            OrderDirection.BID
        )
        val order = Valid.order.copy(
            ouid = orderEvent.ouid,
            uuid = orderEvent.uuid,
            matchingEngineId = orderEvent.orderId,
            price = orderEvent.oldPrice,
            quantity = orderEvent.oldQuantity,
            remainedTransferAmount = BigDecimal("1.000000000"),
            firstTransferAmount = BigDecimal("1.000000000")
        )
        orderPersister.orders[orderEvent.ouid] = order

        val financialActions = orderManager.handleUpdateOrder(orderEvent)

        val updatedOrder = orderPersister.orders.getValue(orderEvent.ouid)
        assertThat(updatedOrder.price).isEqualTo(120000)
        assertThat(updatedOrder.quantity).isEqualTo(1000)
        assertThat(updatedOrder.remainedTransferAmount).isEqualByComparingTo(BigDecimal("1.200000000"))
        assertThat(financialActions).hasSize(1)
        assertThat(financialActions.single().amount).isEqualByComparingTo(BigDecimal("0.200000000"))
        assertThat(financialActions.single().senderWalletType).isEqualTo(WalletType.MAIN)
        assertThat(financialActions.single().receiverWalletType).isEqualTo(WalletType.EXCHANGE)
        assertThat(financialActions.single().category).isEqualTo(FinancialActionCategory.ORDER_CREATE)
        assertThat(richOrderPublisher.published).hasSize(1)
    }

    @Test
    fun givenUpdateOrderDecreasesAskReserve_whenLocalFound_releaseReserveFA(): Unit = runBlocking {
        val orderEvent = UpdatedOrderEvent(
            "ask_ouid",
            "user_id",
            89,
            Pair("BTC", "USDT"),
            100000,
            1000,
            100000,
            700,
            1000,
            OrderDirection.ASK
        )
        val order = Valid.order.copy(
            ouid = orderEvent.ouid,
            uuid = orderEvent.uuid,
            matchingEngineId = orderEvent.orderId,
            direction = OrderDirection.ASK,
            price = orderEvent.oldPrice,
            quantity = orderEvent.oldQuantity,
            remainedTransferAmount = BigDecimal("0.001000"),
            firstTransferAmount = BigDecimal("0.001000")
        )
        orderPersister.orders[orderEvent.ouid] = order

        val financialActions = orderManager.handleUpdateOrder(orderEvent)

        val updatedOrder = orderPersister.orders.getValue(orderEvent.ouid)
        assertThat(updatedOrder.quantity).isEqualTo(700)
        assertThat(updatedOrder.remainedTransferAmount).isEqualByComparingTo(BigDecimal("0.000700"))
        assertThat(financialActions).hasSize(1)
        assertThat(financialActions.single().amount).isEqualByComparingTo(BigDecimal("0.000300"))
        assertThat(financialActions.single().senderWalletType).isEqualTo(WalletType.EXCHANGE)
        assertThat(financialActions.single().receiverWalletType).isEqualTo(WalletType.MAIN)
        assertThat(financialActions.single().category).isEqualTo(FinancialActionCategory.ORDER_CANCEL)
    }

    @Test
    fun givenUpdateOrderPersistFails_whenLocalFound_doNotPublishRichOrderUpdate(): Unit = runBlocking {
        val localRichOrderPublisher = RecordingRichOrderPublisher()
        val localOrderPersister = InMemoryOrderPersister()
        val failingFinancialActionStore = RecordingFinancialActionStore(
            IllegalStateException("financial action persist failed")
        )
        val localOrderManager = OrderManagerImpl(
            pairConfigLoader,
            userLevelLoader,
            failingFinancialActionStore,
            failingFinancialActionStore,
            localOrderPersister,
            tempEventPersister,
            localRichOrderPublisher,
            financialActionPublisher,
            JsonMapperTestImpl(),
            RecordingProcessedEventPersister()
        )
        val orderEvent = UpdatedOrderEvent(
            "persist_fail_update_ouid",
            "user_id",
            88,
            Pair("BTC", "USDT"),
            100000,
            1000,
            120000,
            1000,
            1000,
            OrderDirection.BID
        )
        localOrderPersister.orders[orderEvent.ouid] = Valid.order.copy(
            ouid = orderEvent.ouid,
            uuid = orderEvent.uuid,
            matchingEngineId = orderEvent.orderId,
            price = orderEvent.oldPrice,
            quantity = orderEvent.oldQuantity,
            remainedTransferAmount = BigDecimal("1.000000000"),
            firstTransferAmount = BigDecimal("1.000000000")
        )

        var thrown: Throwable? = null
        try {
            localOrderManager.handleUpdateOrder(orderEvent)
        } catch (e: Throwable) {
            thrown = e
        }

        assertThat(thrown).isInstanceOf(IllegalStateException::class.java)
        assertThat(localRichOrderPublisher.published).isEmpty()
    }

    @Test
    fun givenStaleUpdateOrderEvent_whenLocalOrderAlreadyMoved_thenIgnoreWithoutRegressingOrder(): Unit = runBlocking {
        val orderEvent = UpdatedOrderEvent(
            "stale_update_ouid",
            "user_id",
            91,
            Pair("BTC", "USDT"),
            100000,
            1000,
            110000,
            1000,
            1000,
            OrderDirection.BID
        )
        val order = Valid.order.copy(
            ouid = orderEvent.ouid,
            uuid = orderEvent.uuid,
            matchingEngineId = orderEvent.orderId,
            price = 120000,
            quantity = 1000,
            remainedTransferAmount = BigDecimal("1.200000000"),
            firstTransferAmount = BigDecimal("1.200000000")
        )
        orderPersister.orders[orderEvent.ouid] = order
        tempEventPersister.saveTempEvent(orderEvent.ouid, orderEvent)

        val financialActions = orderManager.handleUpdateOrder(orderEvent)

        val persistedOrder = orderPersister.orders.getValue(orderEvent.ouid)
        assertThat(financialActions).isEmpty()
        assertThat(persistedOrder.price).isEqualTo(120000)
        assertThat(persistedOrder.quantity).isEqualTo(1000)
        assertThat(persistedOrder.remainedTransferAmount).isEqualByComparingTo(BigDecimal("1.200000000"))
        assertThat(financialActionStore.persisted).isEmpty()
        assertThat(richOrderPublisher.published).isEmpty()
        assertThat(orderPersister.saved).isEmpty()
        assertThat(tempEventPersister.loadTempEvents(orderEvent.ouid)).isEmpty()
    }

    @Test
    fun givenUpdateOrderEventWithImpossibleRemainder_whenLocalFound_ignoreWithoutMutatingOrder(): Unit = runBlocking {
        val orderEvent = UpdatedOrderEvent(
            "invalid_update_quantity_ouid",
            "user_id",
            91,
            Pair("BTC", "USDT"),
            100000,
            1000,
            120000,
            1000,
            1001,
            OrderDirection.BID
        )
        val order = Valid.order.copy(
            ouid = orderEvent.ouid,
            uuid = orderEvent.uuid,
            matchingEngineId = orderEvent.orderId,
            price = orderEvent.oldPrice,
            quantity = orderEvent.oldQuantity,
            filledQuantity = 0,
            remainedTransferAmount = BigDecimal("1.000000000"),
            firstTransferAmount = BigDecimal("1.000000000")
        )
        orderPersister.orders[orderEvent.ouid] = order
        tempEventPersister.saveTempEvent(orderEvent.ouid, orderEvent)

        val financialActions = orderManager.handleUpdateOrder(orderEvent)

        val persistedOrder = orderPersister.orders.getValue(orderEvent.ouid)
        assertThat(financialActions).isEmpty()
        assertThat(persistedOrder.price).isEqualTo(orderEvent.oldPrice)
        assertThat(persistedOrder.quantity).isEqualTo(orderEvent.oldQuantity)
        assertThat(persistedOrder.filledQuantity).isZero()
        assertThat(persistedOrder.remainedTransferAmount).isEqualByComparingTo(BigDecimal("1.000000000"))
        assertThat(financialActionStore.persisted).isEmpty()
        assertThat(richOrderPublisher.published).isEmpty()
        assertThat(orderPersister.saved).isEmpty()
        assertThat(tempEventPersister.loadTempEvents(orderEvent.ouid)).isEmpty()
    }

    @Test
    fun givenUpdateOrderEventReceived_whenLocalOrderNull_saveTempEvent(): Unit = runBlocking {
        val orderEvent = UpdatedOrderEvent(
            "missing_ouid",
            "user_id",
            90,
            Pair("BTC", "USDT"),
            100000,
            1000,
            120000,
            1000,
            1000,
            OrderDirection.BID
        )

        val financialActions = orderManager.handleUpdateOrder(orderEvent)

        assertThat(financialActions).isEmpty()
        assertThat(tempEventPersister.saved).hasSize(1)
        assertThat(tempEventPersister.saved.single().ouid).isEqualTo(orderEvent.ouid)
    }

    @Test
    fun givenUpdateOrderEventWithImpossibleRemainder_whenLocalOrderNull_ignoreWithoutSavingTempEvent(): Unit = runBlocking {
        val orderEvent = UpdatedOrderEvent(
            "invalid_missing_update_ouid",
            "user_id",
            90,
            Pair("BTC", "USDT"),
            100000,
            1000,
            120000,
            1000,
            1001,
            OrderDirection.BID
        )

        val financialActions = orderManager.handleUpdateOrder(orderEvent)

        assertThat(financialActions).isEmpty()
        assertThat(tempEventPersister.saved).isEmpty()
        assertThat(financialActionStore.persisted).isEmpty()
        assertThat(richOrderPublisher.published).isEmpty()
        assertThat(orderPersister.saved).isEmpty()
    }

    @Test
    fun givenRejectOrderReceived_whenLocalOrderNull_saveTempEvent(): Unit = runBlocking {
        val orderEvent = RejectOrderEvent(
            "ouid",
            "user_1",
            56,
            Pair("BTC", "USDT"),
            100000,
            1000,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER,
            RequestedOperation.PLACE_ORDER,
            RejectReason.ORDER_NOT_FOUND,
        )

        val fa = orderManager.handleRejectOrder(orderEvent)

        assertThat(fa.size).isEqualTo(0)
        assertThat(tempEventPersister.saved).hasSize(1)
        assertThat(tempEventPersister.saved[0].ouid).isEqualTo(orderEvent.ouid)
    }

    @Test
    fun givenRejectOrderMissingOrderDetails_whenLocalOrderNull_ignoreWithoutSavingTempEvent(): Unit = runBlocking {
        val orderEvent = RejectOrderEvent(
            "invalid_reject_ouid",
            "user_1",
            56,
            Pair("BTC", "USDT"),
            RequestedOperation.PLACE_ORDER,
            RejectReason.ORDER_NOT_FOUND
        )

        val fa = orderManager.handleRejectOrder(orderEvent)

        assertThat(fa).isEmpty()
        assertThat(tempEventPersister.saved).isEmpty()
        assertThat(financialActionStore.persisted).isEmpty()
        assertThat(richOrderPublisher.published).isEmpty()
        assertThat(orderPersister.saved).isEmpty()
        assertThat(processedEventPersister.processed).isEmpty()
    }

    @Test
    fun givenRejectOrderReceived_whenOperationNotPlaceOrder_returnEmptyFA(): Unit = runBlocking {
        val orderEvent = RejectOrderEvent(
            "ouid",
            "user_1",
            56,
            Pair("BTC", "USDT"),
            RequestedOperation.CANCEL_ORDER,
            RejectReason.ORDER_NOT_FOUND
        )

        val fa = orderManager.handleRejectOrder(orderEvent)
        assertThat(fa.size).isEqualTo(0)
    }

    @Test
    fun givenRejectOrderReceived_whenLocalFound_publishRichOrderUpdate(): Unit = runBlocking {
        val orderEvent = RejectOrderEvent(
            "ouid",
            "user_1",
            56,
            Pair("BTC", "USDT"),
            100000,
            1000,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER,
            RequestedOperation.PLACE_ORDER,
            RejectReason.ORDER_NOT_FOUND,
        )
        val order = Valid.order.copy(ouid = orderEvent.ouid)
        orderPersister.orders[orderEvent.ouid] = order

        val fa = orderManager.handleRejectOrder(orderEvent)[0]

        assertThat(fa.amount).isEqualTo(order.remainedTransferAmount)
        assertThat(fa.symbol).isEqualTo(orderEvent.pair.rightSideName)
        assertThat(fa.category).isEqualTo(FinancialActionCategory.ORDER_CANCEL)

        assertThat(order.status).isEqualTo(OrderStatus.REJECTED.code)

        assertThat(richOrderPublisher.published).hasSize(1)
        val richOrderUpdate = richOrderPublisher.published.single() as RichOrderUpdate
        assertThat(richOrderUpdate.price).isEqualByComparingTo(order.origPrice)
        assertThat(richOrderUpdate.quantity).isEqualByComparingTo(order.origQuantity)
        assertThat(richOrderUpdate.remainedQuantity).isEqualByComparingTo(
            order.origQuantity.subtract(order.filledOrigQuantity)
        )
        assertThat(richOrderUpdate.executedQuantity()).isEqualByComparingTo(
            order.filledOrigQuantity
        )
        assertThat(orderPersister.saved).hasSize(1)
    }

    @Test
    fun givenRejectOrderPersistFails_whenLocalFound_doNotPublishRichOrderUpdate(): Unit = runBlocking {
        val localRichOrderPublisher = RecordingRichOrderPublisher()
        val localOrderPersister = InMemoryOrderPersister()
        val failingFinancialActionStore = RecordingFinancialActionStore(
            IllegalStateException("financial action persist failed")
        )
        val localOrderManager = OrderManagerImpl(
            pairConfigLoader,
            userLevelLoader,
            failingFinancialActionStore,
            failingFinancialActionStore,
            localOrderPersister,
            tempEventPersister,
            localRichOrderPublisher,
            financialActionPublisher,
            JsonMapperTestImpl(),
            RecordingProcessedEventPersister()
        )
        val orderEvent = RejectOrderEvent(
            "persist_fail_reject_ouid",
            "user_1",
            56,
            Pair("BTC", "USDT"),
            100000,
            1000,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER,
            RequestedOperation.PLACE_ORDER,
            RejectReason.ORDER_NOT_FOUND,
        )
        localOrderPersister.orders[orderEvent.ouid] = Valid.order.copy(
            ouid = orderEvent.ouid,
            uuid = orderEvent.uuid,
            matchingEngineId = orderEvent.orderId,
            price = orderEvent.price!!,
            quantity = orderEvent.quantity!!,
            direction = orderEvent.direction!!,
            matchConstraint = orderEvent.matchConstraint!!,
            orderType = orderEvent.orderType!!
        )

        var thrown: Throwable? = null
        try {
            localOrderManager.handleRejectOrder(orderEvent)
        } catch (e: Throwable) {
            thrown = e
        }

        assertThat(thrown).isInstanceOf(IllegalStateException::class.java)
        assertThat(localRichOrderPublisher.published).isEmpty()
    }

    @Test
    fun givenCancelOrderReceived_whenLocalOrderNull_saveTempEvent(): Unit = runBlocking {
        val orderEvent = CancelOrderEvent(
            "order_ouid",
            "user_id",
            88,
            Pair("BTC", "USDT"),
            100000,
            1000,
            500,
            OrderDirection.BID
        )

        val fa = orderManager.handleCancelOrder(orderEvent)

        assertThat(fa.size).isEqualTo(0)
        assertThat(tempEventPersister.saved).hasSize(1)
        assertThat(tempEventPersister.saved[0].ouid).isEqualTo(orderEvent.ouid)
    }

    @Test
    fun givenCancelOrderReceived_whenLocalFound_publishRichOrderUpdate(): Unit = runBlocking {
        val orderEvent = CancelOrderEvent(
            "order_ouid",
            "user_1",
            88,
            Pair("BTC", "USDT"),
            100000,
            1000,
            500,
            OrderDirection.BID
        )
        val order = Valid.order.copy(
            ouid = orderEvent.ouid,
            matchingEngineId = orderEvent.orderId,
            filledQuantity = 500,
            origPrice = BigDecimal("1000"),
            origQuantity = BigDecimal("0.001"),
            filledOrigQuantity = BigDecimal("0.0005")
        )
        orderPersister.orders[orderEvent.ouid] = order

        val fa = orderManager.handleCancelOrder(orderEvent)[0]

        assertThat(fa.eventType).isEqualTo(CancelOrderEvent::class.simpleName!!)
        assertThat(fa.amount).isEqualTo(order.remainedTransferAmount)
        assertThat(fa.symbol).isEqualTo(orderEvent.pair.rightSideName)
        assertThat(fa.category).isEqualTo(FinancialActionCategory.ORDER_CANCEL)
        assertThat(order.status).isEqualTo(OrderStatus.CANCELED.code)

        assertThat(richOrderPublisher.published).hasSize(1)
        val richOrderUpdate = richOrderPublisher.published.single() as RichOrderUpdate
        assertThat(richOrderUpdate.price).isEqualByComparingTo(order.origPrice)
        assertThat(richOrderUpdate.quantity).isEqualByComparingTo(order.origQuantity)
        assertThat(richOrderUpdate.remainedQuantity).isEqualByComparingTo(
            orderEvent.remainedQuantity.toBigDecimal().multiply(order.leftSideFraction)
        )
        assertThat(richOrderUpdate.executedQuantity()).isEqualByComparingTo(order.filledOrigQuantity)
        assertThat(orderPersister.saved).hasSize(1)
    }

    @Test
    fun givenCancelOrderPersistFails_whenLocalFound_doNotPublishRichOrderUpdate(): Unit = runBlocking {
        val localRichOrderPublisher = RecordingRichOrderPublisher()
        val localOrderPersister = InMemoryOrderPersister()
        val failingFinancialActionStore = RecordingFinancialActionStore(
            IllegalStateException("financial action persist failed")
        )
        val localOrderManager = OrderManagerImpl(
            pairConfigLoader,
            userLevelLoader,
            failingFinancialActionStore,
            failingFinancialActionStore,
            localOrderPersister,
            tempEventPersister,
            localRichOrderPublisher,
            financialActionPublisher,
            JsonMapperTestImpl(),
            RecordingProcessedEventPersister()
        )
        val orderEvent = CancelOrderEvent(
            "persist_fail_cancel_ouid",
            "user_1",
            88,
            Pair("BTC", "USDT"),
            100000,
            1000,
            500,
            OrderDirection.BID
        )
        localOrderPersister.orders[orderEvent.ouid] = Valid.order.copy(
            ouid = orderEvent.ouid,
            uuid = orderEvent.uuid,
            matchingEngineId = orderEvent.orderId,
            price = orderEvent.price,
            quantity = orderEvent.quantity,
            direction = orderEvent.direction,
            matchConstraint = orderEvent.matchConstraint,
            orderType = orderEvent.orderType,
            filledQuantity = 500
        )

        var thrown: Throwable? = null
        try {
            localOrderManager.handleCancelOrder(orderEvent)
        } catch (e: Throwable) {
            thrown = e
        }

        assertThat(thrown).isInstanceOf(IllegalStateException::class.java)
        assertThat(localRichOrderPublisher.published).isEmpty()
    }

    @Test
    fun givenCancelOrderReceivedBeforeTradeApplied_whenLocalOrderBehind_saveTempEvent(): Unit = runBlocking {
        val orderEvent = CancelOrderEvent(
            "order_ouid",
            "user_1",
            88,
            Pair("BTC", "USDT"),
            100000,
            1000,
            500,
            OrderDirection.BID
        )
        orderPersister.orders[orderEvent.ouid] =
            Valid.order.copy(ouid = orderEvent.ouid, matchingEngineId = orderEvent.orderId, filledQuantity = 0)

        val fa = orderManager.handleCancelOrder(orderEvent)

        assertThat(fa).isEmpty()
        assertThat(tempEventPersister.saved).hasSize(1)
        assertThat(tempEventPersister.saved[0].ouid).isEqualTo(orderEvent.ouid)
        assertThat(financialActionStore.persisted).isEmpty()
        assertThat(richOrderPublisher.published).isEmpty()
        assertThat(orderPersister.saved).isEmpty()
        assertThat(processedEventPersister.processed).isEmpty()
    }

    @Test
    fun givenCancelOrderEventWithNegativeRemainder_whenLocalFound_ignoreWithoutSavingTempEvent(): Unit = runBlocking {
        val orderEvent = CancelOrderEvent(
            "invalid_cancel_quantity_ouid",
            "user_1",
            88,
            Pair("BTC", "USDT"),
            100000,
            1000,
            -1,
            OrderDirection.BID
        )
        orderPersister.orders[orderEvent.ouid] =
            Valid.order.copy(ouid = orderEvent.ouid, matchingEngineId = orderEvent.orderId, filledQuantity = 500)
        tempEventPersister.saveTempEvent(orderEvent.ouid, orderEvent)

        val financialActions = orderManager.handleCancelOrder(orderEvent)

        assertThat(financialActions).isEmpty()
        assertThat(financialActionStore.persisted).isEmpty()
        assertThat(richOrderPublisher.published).isEmpty()
        assertThat(orderPersister.saved).isEmpty()
        assertThat(tempEventPersister.loadTempEvents(orderEvent.ouid)).isEmpty()
        assertThat(processedEventPersister.processed).isEmpty()
    }

    @Test
    fun givenCancelOrderReceivedTwice_whenLocalFound_ignoreDuplicate(): Unit = runBlocking {
        val orderEvent = CancelOrderEvent(
            "duplicate_cancel_ouid",
            "user_1",
            88,
            Pair("BTC", "USDT"),
            100000,
            1000,
            500,
            OrderDirection.BID
        )
        orderPersister.orders[orderEvent.ouid] =
            Valid.order.copy(ouid = orderEvent.ouid, matchingEngineId = orderEvent.orderId, filledQuantity = 500)

        val first = orderManager.handleCancelOrder(orderEvent)
        val second = orderManager.handleCancelOrder(orderEvent)

        assertThat(first).hasSize(1)
        assertThat(second).isEmpty()
        assertThat(financialActionStore.persisted).hasSize(1)
        assertThat(richOrderPublisher.published).hasSize(1)
        assertThat(orderPersister.saved).hasSize(1)
    }

    @Test
    fun givenDifferentCancelOrderEventAfterOrderCanceled_whenLocalFound_ignoreWithoutReleasingReserveAgain(): Unit =
        runBlocking {
            val firstCancelEvent = CancelOrderEvent(
                "terminal_cancel_ouid",
                "user_1",
                88,
                Pair("BTC", "USDT"),
                100000,
                1000,
                500,
                OrderDirection.BID
            )
            val secondCancelEvent = CancelOrderEvent(
                firstCancelEvent.ouid,
                firstCancelEvent.uuid,
                firstCancelEvent.orderId,
                firstCancelEvent.pair,
                firstCancelEvent.price,
                firstCancelEvent.quantity,
                400,
                firstCancelEvent.direction
            )
            orderPersister.orders[firstCancelEvent.ouid] =
                Valid.order.copy(
                    ouid = firstCancelEvent.ouid,
                    matchingEngineId = firstCancelEvent.orderId,
                    filledQuantity = 500
                )

            val first = orderManager.handleCancelOrder(firstCancelEvent)
            val second = orderManager.handleCancelOrder(secondCancelEvent)

            assertThat(first).hasSize(1)
            assertThat(second).isEmpty()
            assertThat(financialActionStore.persisted).hasSize(1)
            assertThat(richOrderPublisher.published).hasSize(1)
            assertThat(orderPersister.saved).hasSize(1)
        }

    @Test
    fun givenCancelOrderEventDoesNotMatchLocalOrder_whenLocalFound_ignoreWithoutReleasingReserve(): Unit = runBlocking {
        val orderEvent = CancelOrderEvent(
            "mismatched_cancel_ouid",
            "intruder",
            88,
            Pair("BTC", "USDT"),
            100000,
            1000,
            500,
            OrderDirection.BID
        )
        orderPersister.orders[orderEvent.ouid] =
            Valid.order.copy(ouid = orderEvent.ouid, matchingEngineId = orderEvent.orderId, filledQuantity = 500)

        val financialActions = orderManager.handleCancelOrder(orderEvent)

        assertThat(financialActions).isEmpty()
        assertThat(financialActionStore.persisted).isEmpty()
        assertThat(richOrderPublisher.published).isEmpty()
        assertThat(orderPersister.saved).isEmpty()
        assertThat(tempEventPersister.saved).isEmpty()
        assertThat(processedEventPersister.processed).isEmpty()
    }

    @Test
    fun givenCancelOrderEventIsBehindLocalFilledQuantity_whenLocalFound_ignoreAsStale(): Unit = runBlocking {
        val orderEvent = CancelOrderEvent(
            "stale_cancel_ouid",
            "user_1",
            88,
            Pair("BTC", "USDT"),
            100000,
            1000,
            500,
            OrderDirection.BID
        )
        orderPersister.orders[orderEvent.ouid] =
            Valid.order.copy(ouid = orderEvent.ouid, matchingEngineId = orderEvent.orderId, filledQuantity = 600)

        val financialActions = orderManager.handleCancelOrder(orderEvent)

        assertThat(financialActions).isEmpty()
        assertThat(financialActionStore.persisted).isEmpty()
        assertThat(richOrderPublisher.published).isEmpty()
        assertThat(orderPersister.saved).isEmpty()
        assertThat(tempEventPersister.saved).isEmpty()
        assertThat(processedEventPersister.processed).isEmpty()
    }

    @Test
    fun givenRejectOrderReceivedTwice_whenLocalFound_ignoreDuplicate(): Unit = runBlocking {
        val orderEvent = RejectOrderEvent(
            "duplicate_reject_ouid",
            "user_1",
            56,
            Pair("BTC", "USDT"),
            100000,
            1000,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER,
            RequestedOperation.PLACE_ORDER,
            RejectReason.ORDER_NOT_FOUND,
        )
        orderPersister.orders[orderEvent.ouid] = Valid.order.copy(ouid = orderEvent.ouid)

        val first = orderManager.handleRejectOrder(orderEvent)
        val second = orderManager.handleRejectOrder(orderEvent)

        assertThat(first).hasSize(1)
        assertThat(second).isEmpty()
        assertThat(financialActionStore.persisted).hasSize(1)
        assertThat(richOrderPublisher.published).hasSize(1)
        assertThat(orderPersister.saved).hasSize(1)
    }

    @Test
    fun givenDifferentRejectOrderEventAfterOrderRejected_whenLocalFound_ignoreWithoutReleasingReserveAgain(): Unit =
        runBlocking {
            val firstRejectEvent = RejectOrderEvent(
                "terminal_reject_ouid",
                "user_1",
                56,
                Pair("BTC", "USDT"),
                100000,
                1000,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER,
                RequestedOperation.PLACE_ORDER,
                RejectReason.ORDER_NOT_FOUND,
            )
            val secondRejectEvent = RejectOrderEvent(
                firstRejectEvent.ouid,
                firstRejectEvent.uuid,
                firstRejectEvent.orderId,
                firstRejectEvent.pair,
                firstRejectEvent.price!!,
                firstRejectEvent.quantity!!,
                firstRejectEvent.direction!!,
                firstRejectEvent.matchConstraint!!,
                firstRejectEvent.orderType!!,
                firstRejectEvent.requestedOperation,
                RejectReason.INVALID_ORDER,
            )
            orderPersister.orders[firstRejectEvent.ouid] = Valid.order.copy(ouid = firstRejectEvent.ouid)

            val first = orderManager.handleRejectOrder(firstRejectEvent)
            val second = orderManager.handleRejectOrder(secondRejectEvent)

            assertThat(first).hasSize(1)
            assertThat(second).isEmpty()
            assertThat(financialActionStore.persisted).hasSize(1)
            assertThat(richOrderPublisher.published).hasSize(1)
            assertThat(orderPersister.saved).hasSize(1)
        }


}
