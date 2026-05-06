package co.nilin.opex.matching.engine.core

import co.nilin.opex.matching.engine.core.eventh.EventDispatcher
import co.nilin.opex.matching.engine.core.eventh.events.CancelOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import co.nilin.opex.matching.engine.core.eventh.events.CreateOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.OrderBookPublishedEvent
import co.nilin.opex.matching.engine.core.eventh.events.RejectOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.TradeEvent
import co.nilin.opex.matching.engine.core.eventh.events.UpdatedOrderEvent
import co.nilin.opex.matching.engine.core.engine.SimpleOrderBook
import co.nilin.opex.matching.engine.core.inout.OrderCancelCommand
import co.nilin.opex.matching.engine.core.inout.OrderCreateCommand
import co.nilin.opex.matching.engine.core.inout.OrderEditCommand
import co.nilin.opex.matching.engine.core.inout.RejectReason
import co.nilin.opex.matching.engine.core.inout.RequestedOperation
import co.nilin.opex.matching.engine.core.model.MatchConstraint
import co.nilin.opex.matching.engine.core.model.OrderDirection
import co.nilin.opex.matching.engine.core.model.OrderType
import co.nilin.opex.matching.engine.core.model.SimpleOrder
import kotlinx.coroutines.Dispatchers
import org.junit.jupiter.api.Assertions
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import java.util.*

class SimpleOrderBookUnitTest {
    private val pair = co.nilin.opex.matching.engine.core.model.Pair("BTC", "USDT")
    private val ETH_BTC_PAIR = co.nilin.opex.matching.engine.core.model.Pair("ETH", "BTC")
    private val uuid = UUID.randomUUID().toString()

    @BeforeEach
    fun resetEventDispatcher() {
        EventDispatcher.clearAll()
    }

    @Test
    fun givenInvalidOrderValues_whenOrderCreated_thenRejectBeforeBookMutation() {
        val orderBook = SimpleOrderBook(pair, false)
        val rejectEvents = mutableListOf<RejectOrderEvent>()
        val publishedEvents = mutableListOf<OrderBookPublishedEvent>()
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectEvents.add(it) }
        EventDispatcher.register(OrderBookPublishedEvent::class.java) { publishedEvents.add(it) }
        val invalidOrders = listOf(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                0,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            ),
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                0,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            ),
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                -1,
                1,
                OrderDirection.ASK,
                MatchConstraint.IOC,
                OrderType.MARKET_ORDER
            )
        )

        invalidOrders.forEach { command ->
            Assertions.assertNull(orderBook.handleNewOrderCommand(command))
        }

        Assertions.assertEquals(3, rejectEvents.count { it.reason == RejectReason.INVALID_ORDER })
        Assertions.assertEquals(3, publishedEvents.size)
        Assertions.assertEquals(0, orderBook.orders.size)
        Assertions.assertEquals(0, orderBook.askOrders.entriesList().size)
        Assertions.assertEquals(0, orderBook.bidOrders.entriesList().size)
        Assertions.assertNull(orderBook.bestAskOrder)
        Assertions.assertNull(orderBook.bestBidOrder)
    }

    @Test
    fun givenRejectedOrderSnapshot_whenCreateCommandReplayedAfterRebuild_thenDuplicateRejectIsIgnored() {
        val orderBook = SimpleOrderBook(pair, false)
        val rejectEvents = mutableListOf<RejectOrderEvent>()
        val publishedEvents = mutableListOf<OrderBookPublishedEvent>()
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectEvents.add(it) }
        EventDispatcher.register(OrderBookPublishedEvent::class.java) { publishedEvents.add(it) }
        val rejectedOuid = UUID.randomUUID().toString()
        val invalidCommand = OrderCreateCommand(
            rejectedOuid,
            uuid,
            pair,
            0,
            1,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )

        orderBook.handleNewOrderCommand(invalidCommand)
        val rebuiltOrderBook = SimpleOrderBook(pair, false)
        rebuiltOrderBook.rebuild(publishedEvents.last().persistentOrderBook)
        rebuiltOrderBook.handleNewOrderCommand(invalidCommand)

        Assertions.assertEquals(1, rejectEvents.count { it.ouid == rejectedOuid })
        Assertions.assertEquals(0, rebuiltOrderBook.orders.size)
        Assertions.assertNull(rebuiltOrderBook.bestAskOrder)
        Assertions.assertNull(rebuiltOrderBook.bestBidOrder)
    }

    @Test
    fun givenOpenClientOrderIdForUser_whenDuplicateClientOrderIdCreated_thenRejectBeforeBookMutation() {
        val orderBook = SimpleOrderBook(pair, false)
        val rejectEvents = mutableListOf<RejectOrderEvent>()
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectEvents.add(it) }
        val clientOrderId = "client-1"
        val firstAsk = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                10,
                2,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER,
                clientOrderId
            )
        ) as SimpleOrder
        val rejectedOuid = UUID.randomUUID().toString()
        val rejectedDuplicate = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                rejectedOuid,
                uuid,
                pair,
                11,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER,
                clientOrderId
            )
        )
        val otherUserAsk = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                UUID.randomUUID().toString(),
                pair,
                12,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER,
                clientOrderId
            )
        )

        Assertions.assertNull(rejectedDuplicate)
        Assertions.assertNotNull(otherUserAsk)
        Assertions.assertEquals(1, rejectEvents.count { it.ouid == rejectedOuid && it.reason == RejectReason.DUPLICATE_CLIENT_ORDER_ID })
        Assertions.assertEquals(2, orderBook.orders.size)
        Assertions.assertEquals(firstAsk, orderBook.bestAskOrder)
        Assertions.assertFalse(orderBook.orders.values.any { it.ouid == rejectedOuid })
    }

    @Test
    fun givenOpenClientOrderIdSnapshot_whenRebuilt_thenDuplicateClientOrderIdIsRejected() {
        val orderBook = SimpleOrderBook(pair, false)
        val rejectEvents = mutableListOf<RejectOrderEvent>()
        val publishedEvents = mutableListOf<OrderBookPublishedEvent>()
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectEvents.add(it) }
        EventDispatcher.register(OrderBookPublishedEvent::class.java) { publishedEvents.add(it) }
        val clientOrderId = "client-1"
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                10,
                2,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER,
                clientOrderId
            )
        )

        val rebuiltOrderBook = SimpleOrderBook(pair, false)
        rebuiltOrderBook.rebuild(publishedEvents.last().persistentOrderBook)
        val rejectedOuid = UUID.randomUUID().toString()
        val rejectedDuplicate = rebuiltOrderBook.handleNewOrderCommand(
            OrderCreateCommand(
                rejectedOuid,
                uuid,
                pair,
                11,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER,
                clientOrderId
            )
        )

        Assertions.assertNull(rejectedDuplicate)
        Assertions.assertEquals(1, rejectEvents.count { it.ouid == rejectedOuid && it.reason == RejectReason.DUPLICATE_CLIENT_ORDER_ID })
        Assertions.assertEquals(1, rebuiltOrderBook.orders.size)
        Assertions.assertEquals(clientOrderId, rebuiltOrderBook.bestAskOrder!!.clientOrderId)
    }

    @Test
    fun givenCrossingOwnOrder_whenGtcLimitOrderCreated_thenSelfTradeIsRejected() {
        val orderBook = SimpleOrderBook(pair, false, preventSelfTrade = true)
        val rejectEvents = mutableListOf<RejectOrderEvent>()
        val tradeEvents = mutableListOf<TradeEvent>()
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectEvents.add(it) }
        EventDispatcher.register(TradeEvent::class.java) { tradeEvents.add(it) }
        val restingAsk = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                10,
                2,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder

        val rejectedBidOuid = UUID.randomUUID().toString()
        val rejectedBid = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                rejectedBidOuid,
                uuid,
                pair,
                10,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )

        Assertions.assertNull(rejectedBid)
        Assertions.assertEquals(1, rejectEvents.count { it.ouid == rejectedBidOuid && it.reason == RejectReason.SELF_TRADE_PREVENTION })
        Assertions.assertEquals(0, tradeEvents.count { it.takerOuid == rejectedBidOuid || it.makerOuid == rejectedBidOuid })
        Assertions.assertEquals(1, orderBook.orders.size)
        Assertions.assertEquals(restingAsk, orderBook.bestAskOrder)
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertEquals(2, orderBook.bestAskOrder!!.remainedQuantity())
    }

    @Test
    fun givenOwnOrderBehindExternalLiquidity_whenGtcLimitOrderCreated_thenOrderIsRejectedBeforeAnyTrade() {
        val orderBook = SimpleOrderBook(pair, false, preventSelfTrade = true)
        val rejectEvents = mutableListOf<RejectOrderEvent>()
        val tradeEvents = mutableListOf<TradeEvent>()
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectEvents.add(it) }
        EventDispatcher.register(TradeEvent::class.java) { tradeEvents.add(it) }
        val externalOwner = UUID.randomUUID().toString()
        val externalAsk = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                externalOwner,
                pair,
                9,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        val ownAsk = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                10,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder

        val rejectedBidOuid = UUID.randomUUID().toString()
        val rejectedBid = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                rejectedBidOuid,
                uuid,
                pair,
                10,
                2,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )

        Assertions.assertNull(rejectedBid)
        Assertions.assertEquals(1, rejectEvents.count { it.ouid == rejectedBidOuid && it.reason == RejectReason.SELF_TRADE_PREVENTION })
        Assertions.assertEquals(0, tradeEvents.count { it.takerOuid == rejectedBidOuid || it.makerOuid == rejectedBidOuid })
        Assertions.assertEquals(2, orderBook.orders.size)
        Assertions.assertEquals(externalAsk, orderBook.bestAskOrder)
        Assertions.assertEquals(ownAsk, externalAsk.worse)
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertEquals(1, externalAsk.remainedQuantity())
        Assertions.assertEquals(1, ownAsk.remainedQuantity())
    }

    @Test
    fun givenDuplicateOuid_whenGtcBidLimitOrderCreatedTwice_thenSecondCreateIsIgnored() {
        val orderBook = SimpleOrderBook(pair, false)
        val ouid = UUID.randomUUID().toString()
        val command = OrderCreateCommand(
            ouid,
            uuid,
            pair,
            1,
            1,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )

        val firstOrder = orderBook.handleNewOrderCommand(command)
        val secondOrder = orderBook.handleNewOrderCommand(command)

        Assertions.assertEquals(firstOrder, secondOrder)
        Assertions.assertEquals(1, orderBook.orders.size)
        Assertions.assertEquals(1, orderBook.bidOrders.get(1).ordersCount)
        Assertions.assertEquals(1, orderBook.bidOrders.get(1).totalQuantity)
    }

    @Test
    fun givenDuplicateOuidAfterFullMatch_whenCreateReplayed_thenNoResidualOrderIsCreated() {
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val takerOuid = UUID.randomUUID().toString()
        val takerCommand = OrderCreateCommand(
            takerOuid,
            uuid,
            pair,
            1,
            1,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )

        orderBook.handleNewOrderCommand(takerCommand)
        val replayedOrder = orderBook.handleNewOrderCommand(takerCommand)

        Assertions.assertNull(replayedOrder)
        Assertions.assertEquals(0, orderBook.orders.size)
        Assertions.assertEquals(0, orderBook.askOrders.entriesList().size)
        Assertions.assertEquals(0, orderBook.bidOrders.entriesList().size)
        Assertions.assertNull(orderBook.bestAskOrder)
        Assertions.assertNull(orderBook.bestBidOrder)
    }

    @Test
    fun givenCanceledOrder_whenCancelCommandReplayed_thenDuplicateCancelIsIgnored() {
        val orderBook = SimpleOrderBook(pair, false)
        var rejectedEvents = 0
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectedEvents++ }
        val ouid = UUID.randomUUID().toString()
        val order = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                ouid,
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val cancelCommand = OrderCancelCommand(ouid, uuid, order!!.id()!!, pair)

        orderBook.handleCancelCommand(cancelCommand)
        orderBook.handleCancelCommand(cancelCommand)

        Assertions.assertEquals(0, rejectedEvents)
        Assertions.assertEquals(0, orderBook.orders.size)
        Assertions.assertNull(orderBook.bestBidOrder)
    }

    @Test
    fun givenCanceledOrderSnapshot_whenCancelCommandReplayedAfterRebuild_thenDuplicateCancelIsIgnored() {
        val orderBook = SimpleOrderBook(pair, false)
        val snapshots = mutableListOf<OrderBookPublishedEvent>()
        var rejectedEvents = 0
        EventDispatcher.register(OrderBookPublishedEvent::class.java) { snapshots.add(it) }
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectedEvents++ }
        val ouid = UUID.randomUUID().toString()
        val order = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                ouid,
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val cancelCommand = OrderCancelCommand(ouid, uuid, order!!.id()!!, pair)

        orderBook.handleCancelCommand(cancelCommand)
        val rebuiltOrderBook = SimpleOrderBook(pair, false)
        rebuiltOrderBook.rebuild(snapshots.last().persistentOrderBook)
        rebuiltOrderBook.handleCancelCommand(cancelCommand)

        Assertions.assertEquals(0, rejectedEvents)
        Assertions.assertEquals(0, rebuiltOrderBook.orders.size)
        Assertions.assertNull(rebuiltOrderBook.bestBidOrder)
    }

    @Test
    fun givenRebuiltOrderBook_whenCancelPublishesSnapshot_thenLastOrderAndNextIdArePreserved() {
        val orderBook = SimpleOrderBook(pair, false)
        val snapshots = mutableListOf<OrderBookPublishedEvent>()
        EventDispatcher.register(OrderBookPublishedEvent::class.java) { snapshots.add(it) }
        val firstOuid = UUID.randomUUID().toString()
        val firstOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                firstOuid,
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )!!
        val secondOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )!!
        val rebuiltOrderBook = SimpleOrderBook(pair, false)
        rebuiltOrderBook.rebuild(snapshots.last().persistentOrderBook)

        rebuiltOrderBook.handleCancelCommand(OrderCancelCommand(firstOuid, uuid, firstOrder.id()!!, pair))
        val snapshotAfterCancel = snapshots.last().persistentOrderBook
        val secondRebuiltOrderBook = SimpleOrderBook(pair, false)
        secondRebuiltOrderBook.rebuild(snapshotAfterCancel)
        val newOrder = secondRebuiltOrderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                3,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )!!

        Assertions.assertEquals(secondOrder.id(), snapshotAfterCancel.lastOrder?.id)
        Assertions.assertTrue(newOrder.id()!! > secondOrder.id()!!)
    }

    @Test
    fun givenEmptyOrderBook_whenGtcBidLimitOrderCreated_then1BucketWithSize1() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        //when
        val order = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        //then
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 1)
        Assertions.assertEquals(orderBook.bestBidOrder, order)
        Dispatchers.Default
    }

    @Test
    fun givenOrderBookWithBidOrders_whenGtcBidLimitOrderWithSamePriceCreated_then() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val bestBidOrder = orderBook.bestBidOrder
        //when
        val order: SimpleOrder =
            orderBook.handleNewOrderCommand(
                OrderCreateCommand(
                    UUID.randomUUID().toString(),
                    uuid,
                    pair,
                    1,
                    1,
                    OrderDirection.BID,
                    MatchConstraint.GTC,
                    OrderType.LIMIT_ORDER
                )
            ) as SimpleOrder
        //then
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 1)
        Assertions.assertEquals(orderBook.bestBidOrder, bestBidOrder)
        Assertions.assertEquals(bestBidOrder!!.worse, order)
        Assertions.assertEquals(order.better, bestBidOrder)
        Assertions.assertEquals(orderBook.bidOrders.get(order.price).lastOrder, order)
        Assertions.assertEquals(orderBook.bidOrders.get(order.price).totalQuantity, 2)
        Assertions.assertEquals(orderBook.bidOrders.get(order.price).ordersCount, 2)
    }

    @Test
    fun givenOrderBookWithBidOrders_whenGtcBidLimitOrderWithLowerPriceCreated_thenBestOrderNotChange() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val bestBidOrder = orderBook.bestBidOrder
        //when
        val order: SimpleOrder =
            orderBook.handleNewOrderCommand(
                OrderCreateCommand(
                    UUID.randomUUID().toString(),
                    uuid,
                    pair,
                    1,
                    1,
                    OrderDirection.BID,
                    MatchConstraint.GTC,
                    OrderType.LIMIT_ORDER
                )
            ) as SimpleOrder
        //then
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 2)
        Assertions.assertEquals(orderBook.bestBidOrder, bestBidOrder)
        Assertions.assertEquals(bestBidOrder!!.worse, order)
        Assertions.assertEquals(order.better, bestBidOrder)
        Assertions.assertEquals(orderBook.bidOrders.get(order.price).lastOrder, order)
        Assertions.assertEquals(orderBook.bidOrders.get(order.price).totalQuantity, 1)
        Assertions.assertEquals(orderBook.bidOrders.get(order.price).ordersCount, 1)
    }

    @Test
    fun givenOrderBookWithBidOrders_whenGtcBidLimitOrderWithHigherPriceCreated_thenBestOrderChanged() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val bestBidOrder = orderBook.bestBidOrder
        //when
        val order: SimpleOrder =
            orderBook.handleNewOrderCommand(
                OrderCreateCommand(
                    UUID.randomUUID().toString(),
                    uuid,
                    pair,
                    2,
                    1,
                    OrderDirection.BID,
                    MatchConstraint.GTC,
                    OrderType.LIMIT_ORDER
                )
            ) as SimpleOrder
        //then
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 2)
        Assertions.assertEquals(orderBook.bestBidOrder, order)
        Assertions.assertEquals(bestBidOrder!!.better, order)
        Assertions.assertEquals(order.worse, bestBidOrder)
        Assertions.assertEquals(orderBook.bidOrders.get(order.price).lastOrder, order)
        Assertions.assertEquals(orderBook.bidOrders.get(order.price).totalQuantity, 1)
        Assertions.assertEquals(orderBook.bidOrders.get(order.price).ordersCount, 1)
    }

    @Test
    fun givenOrderBookWithBidOrders_whenGtcAskLimitOrderWithSamePriceCreated_thenInstantMatch() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        //when
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        //then
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 0)
        Assertions.assertEquals(orderBook.askOrders.entriesList().size, 0)
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertNull(orderBook.bestAskOrder)
    }

    @Test
    fun givenOrderBookWithBidOrders_whenGtcAskLimitOrderWithNotMatchPriceCreated_thenAddToQueue() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        //when
        val order: SimpleOrder =
            orderBook.handleNewOrderCommand(
                OrderCreateCommand(
                    UUID.randomUUID().toString(),
                    uuid,
                    pair,
                    3,
                    1,
                    OrderDirection.ASK,
                    MatchConstraint.GTC,
                    OrderType.LIMIT_ORDER
                )
            ) as SimpleOrder
        //then
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 2)
        Assertions.assertEquals(orderBook.askOrders.entriesList().size, 1)
        Assertions.assertNotNull(orderBook.bestBidOrder)
        Assertions.assertEquals(orderBook.bestAskOrder, order)
    }

    @Test
    fun givenOrderBookWithBidAndAskOrders_whenGtcAskLimitOrderWithMatchPriceGreaterQuantityCreated_thenAddToQueue() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                3,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        //when
        val order: SimpleOrder =
            orderBook.handleNewOrderCommand(
                OrderCreateCommand(
                    UUID.randomUUID().toString(),
                    uuid,
                    pair,
                    1,
                    3,
                    OrderDirection.ASK,
                    MatchConstraint.GTC,
                    OrderType.LIMIT_ORDER
                )
            ) as SimpleOrder
        //then
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 0)
        Assertions.assertEquals(orderBook.askOrders.entriesList().size, 2)
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertEquals(orderBook.bestAskOrder, order)
    }

    @Test
    fun givenOrderBook_whenCancelBestBidOrder_thenBestBidOrderChange() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        val firstOrderId = UUID.randomUUID().toString()
        val secondOrderId = UUID.randomUUID().toString()

        val firstOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                firstOrderId,
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val lastOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                secondOrderId,
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        //when
        orderBook.handleCancelCommand(OrderCancelCommand(firstOrderId, uuid, firstOrder!!.id()!!, pair))
        //then
        Assertions.assertEquals(orderBook.bestBidOrder, lastOrder)
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 1)
    }

    @Test
    fun givenOrderBookWithMoreBids_whenCancelBestBidOrder_thenBestBidOrderChange() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        val firstOrderId = UUID.randomUUID().toString()
        val secondOrderId = UUID.randomUUID().toString()

        val firstOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                firstOrderId,
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val secondOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                secondOrderId,
                uuid,
                pair,
                2,
                3,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        //when
        orderBook.handleCancelCommand(OrderCancelCommand(firstOrderId, uuid, firstOrder!!.id()!!, pair))
        //then
        Assertions.assertEquals(orderBook.bestBidOrder, secondOrder)
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 2)
    }

    @Test
    fun givenOrderBookWithMoreBids_whenCancelABidOrder_thenBestBidOrderNotChange() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        val firstOrderId = UUID.randomUUID().toString()
        val secondOrderId = UUID.randomUUID().toString()

        val firstOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                firstOrderId,
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val secondOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                secondOrderId,
                uuid,
                pair,
                2,
                3,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        //when
        orderBook.handleCancelCommand(OrderCancelCommand(secondOrderId, uuid, secondOrder!!.id()!!, pair))
        //then
        Assertions.assertEquals(orderBook.bestBidOrder, firstOrder)
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 2)
    }

    @Test
    fun givenPartiallyFilledBidOrder_whenCanceled_thenCancelEventUsesCurrentRemainingQuantity() {
        val orderBook = SimpleOrderBook(pair, false)
        val bidOuid = UUID.randomUUID().toString()
        val bidOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                bidOuid,
                uuid,
                pair,
                10,
                5,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )!!
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                UUID.randomUUID().toString(),
                pair,
                10,
                2,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val cancelEvents = mutableListOf<CancelOrderEvent>()
        val publishedEvents = mutableListOf<OrderBookPublishedEvent>()
        EventDispatcher.register(CancelOrderEvent::class.java) { cancelEvents.add(it) }
        EventDispatcher.register(OrderBookPublishedEvent::class.java) { publishedEvents.add(it) }

        orderBook.handleCancelCommand(OrderCancelCommand(bidOuid, uuid, bidOrder.id()!!, pair))

        Assertions.assertEquals(1, cancelEvents.size)
        cancelEvents.single().also {
            Assertions.assertEquals(bidOuid, it.ouid)
            Assertions.assertEquals(uuid, it.uuid)
            Assertions.assertEquals(bidOrder.id(), it.orderId)
            Assertions.assertEquals(10, it.price)
            Assertions.assertEquals(5, it.quantity)
            Assertions.assertEquals(3, it.remainedQuantity)
            Assertions.assertEquals(OrderDirection.BID, it.direction)
            Assertions.assertEquals(MatchConstraint.GTC, it.matchConstraint)
            Assertions.assertEquals(OrderType.LIMIT_ORDER, it.orderType)
        }
        Assertions.assertEquals(1, publishedEvents.size)
        Assertions.assertEquals(0, orderBook.orders.size)
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertNull(orderBook.bestAskOrder)
    }

    @Test
    fun givenOrderBookWithBidOrder_whenDifferentUserCancels_thenOrderRemainsOpen() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        val ownerOrderId = UUID.randomUUID().toString()
        val intruderUuid = UUID.randomUUID().toString()

        val order = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                ownerOrderId,
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )

        //when
        orderBook.handleCancelCommand(OrderCancelCommand(ownerOrderId, intruderUuid, order!!.id()!!, pair))

        //then
        Assertions.assertEquals(orderBook.bestBidOrder, order)
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 1)
    }

    @Test
    fun givenOrderBookWithBidOrder_whenDifferentUserEdits_thenOrderRemainsOpen() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        val ownerOrderId = UUID.randomUUID().toString()
        val intruderUuid = UUID.randomUUID().toString()

        val order = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                ownerOrderId,
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )

        //when
        val editedOrder = orderBook.handleEditCommand(
            OrderEditCommand(
                ownerOrderId,
                intruderUuid,
                order!!.id()!!,
                pair,
                3,
                2
            )
        )

        //then
        Assertions.assertNull(editedOrder)
        Assertions.assertEquals(orderBook.bestBidOrder, order)
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 1)
        Assertions.assertEquals(orderBook.orders.size, 1)
    }

    @Test
    fun givenOwnAskOrder_whenBidEditWouldSelfTrade_thenRejectBeforeBookMutation() {
        val orderBook = SimpleOrderBook(pair, false, preventSelfTrade = true)
        val rejectEvents = mutableListOf<RejectOrderEvent>()
        val tradeEvents = mutableListOf<TradeEvent>()
        val updateEvents = mutableListOf<UpdatedOrderEvent>()
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectEvents.add(it) }
        EventDispatcher.register(TradeEvent::class.java) { tradeEvents.add(it) }
        EventDispatcher.register(UpdatedOrderEvent::class.java) { updateEvents.add(it) }
        val askOuid = UUID.randomUUID().toString()
        val askOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                askOuid,
                uuid,
                pair,
                10,
                2,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        val bidOuid = UUID.randomUUID().toString()
        val bidOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                bidOuid,
                uuid,
                pair,
                9,
                3,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder

        val editedOrder = orderBook.handleEditCommand(
            OrderEditCommand(
                bidOuid,
                uuid,
                bidOrder.id()!!,
                pair,
                10,
                3
            )
        )

        Assertions.assertNull(editedOrder)
        Assertions.assertEquals(1, rejectEvents.count {
            it.ouid == bidOuid &&
                it.requestedOperation == RequestedOperation.EDIT_ORDER &&
                it.reason == RejectReason.SELF_TRADE_PREVENTION
        })
        Assertions.assertTrue(tradeEvents.isEmpty())
        Assertions.assertTrue(updateEvents.isEmpty())
        Assertions.assertEquals(2, orderBook.orders.size)
        Assertions.assertEquals(askOrder, orderBook.bestAskOrder)
        Assertions.assertEquals(bidOrder, orderBook.bestBidOrder)
        Assertions.assertEquals(2, askOrder.remainedQuantity())
        Assertions.assertEquals(3, bidOrder.remainedQuantity())
    }

    @Test
    fun givenOwnBidOrder_whenAskEditWouldSelfTrade_thenRejectBeforeBookMutation() {
        val orderBook = SimpleOrderBook(pair, false, preventSelfTrade = true)
        val rejectEvents = mutableListOf<RejectOrderEvent>()
        val tradeEvents = mutableListOf<TradeEvent>()
        val updateEvents = mutableListOf<UpdatedOrderEvent>()
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectEvents.add(it) }
        EventDispatcher.register(TradeEvent::class.java) { tradeEvents.add(it) }
        EventDispatcher.register(UpdatedOrderEvent::class.java) { updateEvents.add(it) }
        val bidOuid = UUID.randomUUID().toString()
        val bidOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                bidOuid,
                uuid,
                pair,
                10,
                2,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        val askOuid = UUID.randomUUID().toString()
        val askOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                askOuid,
                uuid,
                pair,
                11,
                3,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder

        val editedOrder = orderBook.handleEditCommand(
            OrderEditCommand(
                askOuid,
                uuid,
                askOrder.id()!!,
                pair,
                10,
                3
            )
        )

        Assertions.assertNull(editedOrder)
        Assertions.assertEquals(1, rejectEvents.count {
            it.ouid == askOuid &&
                it.requestedOperation == RequestedOperation.EDIT_ORDER &&
                it.reason == RejectReason.SELF_TRADE_PREVENTION
        })
        Assertions.assertTrue(tradeEvents.isEmpty())
        Assertions.assertTrue(updateEvents.isEmpty())
        Assertions.assertEquals(2, orderBook.orders.size)
        Assertions.assertEquals(askOrder, orderBook.bestAskOrder)
        Assertions.assertEquals(bidOrder, orderBook.bestBidOrder)
        Assertions.assertEquals(3, askOrder.remainedQuantity())
        Assertions.assertEquals(2, bidOrder.remainedQuantity())
    }

    @Test
    fun givenExternalAskBeforeOwnAsk_whenBidEditWouldEventuallySelfTrade_thenRejectWithoutPartialFill() {
        val orderBook = SimpleOrderBook(pair, false, preventSelfTrade = true)
        val rejectEvents = mutableListOf<RejectOrderEvent>()
        val tradeEvents = mutableListOf<TradeEvent>()
        val updateEvents = mutableListOf<UpdatedOrderEvent>()
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectEvents.add(it) }
        EventDispatcher.register(TradeEvent::class.java) { tradeEvents.add(it) }
        EventDispatcher.register(UpdatedOrderEvent::class.java) { updateEvents.add(it) }
        val externalUuid = UUID.randomUUID().toString()
        val externalAskOuid = UUID.randomUUID().toString()
        val externalAskOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                externalAskOuid,
                externalUuid,
                pair,
                9,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        val ownAskOuid = UUID.randomUUID().toString()
        val ownAskOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                ownAskOuid,
                uuid,
                pair,
                10,
                2,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        val bidOuid = UUID.randomUUID().toString()
        val bidOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                bidOuid,
                uuid,
                pair,
                8,
                5,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder

        val editedOrder = orderBook.handleEditCommand(
            OrderEditCommand(
                bidOuid,
                uuid,
                bidOrder.id()!!,
                pair,
                10,
                5
            )
        )

        Assertions.assertNull(editedOrder)
        Assertions.assertEquals(1, rejectEvents.count {
            it.ouid == bidOuid &&
                it.requestedOperation == RequestedOperation.EDIT_ORDER &&
                it.reason == RejectReason.SELF_TRADE_PREVENTION
        })
        Assertions.assertTrue(tradeEvents.isEmpty())
        Assertions.assertTrue(updateEvents.isEmpty())
        Assertions.assertEquals(3, orderBook.orders.size)
        Assertions.assertEquals(externalAskOrder, orderBook.bestAskOrder)
        Assertions.assertEquals(bidOrder, orderBook.bestBidOrder)
        Assertions.assertEquals(1, externalAskOrder.remainedQuantity())
        Assertions.assertEquals(2, ownAskOrder.remainedQuantity())
        Assertions.assertEquals(5, bidOrder.remainedQuantity())
    }

    @Test
    fun givenOrderBookWithBidOrder_whenInvalidEditRequested_thenRejectBeforeBookMutation() {
        val orderBook = SimpleOrderBook(pair, false)
        val rejectEvents = mutableListOf<RejectOrderEvent>()
        EventDispatcher.register(RejectOrderEvent::class.java) { rejectEvents.add(it) }
        val orderOuid = UUID.randomUUID().toString()
        val order = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                orderOuid,
                uuid,
                pair,
                2,
                5,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )!!

        val invalidPriceEdit = orderBook.handleEditCommand(
            OrderEditCommand(
                orderOuid,
                uuid,
                order.id()!!,
                pair,
                0,
                5
            )
        )
        val invalidQuantityEdit = orderBook.handleEditCommand(
            OrderEditCommand(
                orderOuid,
                uuid,
                order.id()!!,
                pair,
                2,
                0
            )
        )

        Assertions.assertNull(invalidPriceEdit)
        Assertions.assertNull(invalidQuantityEdit)
        Assertions.assertEquals(2, rejectEvents.count {
            it.requestedOperation == RequestedOperation.EDIT_ORDER && it.reason == RejectReason.INVALID_ORDER
        })
        Assertions.assertEquals(1, orderBook.orders.size)
        Assertions.assertEquals(order, orderBook.bestBidOrder)
        Assertions.assertEquals(1, orderBook.bidOrders.entriesList().size)
        Assertions.assertEquals(5, orderBook.bidOrders.get(2).totalQuantity)
        Assertions.assertEquals(1, orderBook.bidOrders.get(2).ordersCount)
    }


    @Test
    fun givenOrderBookWithMoreBids_whenEditABidOrder_thenBestBidOrderChange() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val secondOrderOuid = UUID.randomUUID().toString()
        val secondOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                secondOrderOuid,
                uuid,
                pair,
                2,
                3,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        //when
        val order = orderBook.handleEditCommand(
            OrderEditCommand(
                secondOrderOuid,
                uuid,
                secondOrder!!.id()!!,
                pair,
                3,
                2
            )
        )
        //then
        Assertions.assertEquals(secondOrder.id(), order?.id())
        Assertions.assertEquals(orderBook.bestBidOrder, order)
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 3)
    }

    @Test
    fun givenPartiallyFilledBidOrder_whenEditOrder_thenEmitUpdateWithOldRemainingQuantity() {
        val orderBook = SimpleOrderBook(pair, false)
        val updateEvents = mutableListOf<UpdatedOrderEvent>()
        EventDispatcher.register(UpdatedOrderEvent::class.java) { updateEvents.add(it) }
        val bidOuid = UUID.randomUUID().toString()
        val bidOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                bidOuid,
                uuid,
                pair,
                10,
                5,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )!!
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                UUID.randomUUID().toString(),
                pair,
                10,
                2,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )

        val editedOrder = orderBook.handleEditCommand(
            OrderEditCommand(
                bidOuid,
                uuid,
                bidOrder.id()!!,
                pair,
                11,
                6
            )
        )

        Assertions.assertEquals(bidOrder.id(), editedOrder?.id())
        Assertions.assertEquals(1, updateEvents.size)
        updateEvents.single().also {
            Assertions.assertEquals(bidOuid, it.ouid)
            Assertions.assertEquals(uuid, it.uuid)
            Assertions.assertEquals(bidOrder.id(), it.orderId)
            Assertions.assertEquals(10, it.oldPrice)
            Assertions.assertEquals(5, it.oldQuantity)
            Assertions.assertEquals(11, it.price)
            Assertions.assertEquals(6, it.quantity)
            Assertions.assertEquals(3, it.remainedQuantity)
            Assertions.assertEquals(OrderDirection.BID, it.direction)
            Assertions.assertEquals(MatchConstraint.GTC, it.matchConstraint)
            Assertions.assertEquals(OrderType.LIMIT_ORDER, it.orderType)
        }
    }

    @Test
    fun givenOpenOrderAlreadyEdited_whenSameEditReplayed_thenNoDuplicateEvents() {
        val orderBook = SimpleOrderBook(pair, false)
        val bidOuid = UUID.randomUUID().toString()
        val bidOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                bidOuid,
                uuid,
                pair,
                10,
                5,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )!!
        val editEvents = mutableListOf<CoreEvent>()
        EventDispatcher.register(CoreEvent::class.java) { editEvents.add(it) }
        val editCommand = OrderEditCommand(
            bidOuid,
            uuid,
            bidOrder.id()!!,
            pair,
            11,
            5
        )

        val editedOrder = orderBook.handleEditCommand(editCommand) as SimpleOrder
        val replayedOrder = orderBook.handleEditCommand(editCommand) as SimpleOrder

        Assertions.assertSame(editedOrder, replayedOrder)
        Assertions.assertEquals(bidOrder.id(), replayedOrder.id())
        Assertions.assertEquals(11, replayedOrder.price)
        Assertions.assertEquals(5, replayedOrder.quantity)
        Assertions.assertEquals(1, orderBook.orders.size)
        Assertions.assertEquals(
            listOf(
                UpdatedOrderEvent::class.java,
                OrderBookPublishedEvent::class.java
            ),
            editEvents.map { it::class.java }
        )
    }

    @Test
    fun givenBidEditCrossesAsk_whenEditOrder_thenEmitUpdateBeforeTradeAndPublish() {
        val orderBook = SimpleOrderBook(pair, false)
        val askOuid = UUID.randomUUID().toString()
        val askUuid = UUID.randomUUID().toString()
        val askOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                askOuid,
                askUuid,
                pair,
                10,
                2,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )!!
        val bidOuid = UUID.randomUUID().toString()
        val bidOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                bidOuid,
                uuid,
                pair,
                9,
                3,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )!!
        val editEvents = mutableListOf<CoreEvent>()
        EventDispatcher.register(CoreEvent::class.java) { editEvents.add(it) }

        val editedOrder = orderBook.handleEditCommand(
            OrderEditCommand(
                bidOuid,
                uuid,
                bidOrder.id()!!,
                pair,
                11,
                3
            )
        )

        Assertions.assertEquals(bidOrder.id(), editedOrder?.id())
        Assertions.assertEquals(1, (editedOrder as SimpleOrder).remainedQuantity())
        Assertions.assertEquals(1, orderBook.orders.size)
        Assertions.assertEquals(editedOrder, orderBook.bestBidOrder)
        Assertions.assertNull(orderBook.bestAskOrder)
        Assertions.assertEquals(
            listOf(
                UpdatedOrderEvent::class.java,
                TradeEvent::class.java,
                OrderBookPublishedEvent::class.java
            ),
            editEvents.map { it::class.java }
        )
        (editEvents[0] as UpdatedOrderEvent).also {
            Assertions.assertEquals(bidOuid, it.ouid)
            Assertions.assertEquals(9, it.oldPrice)
            Assertions.assertEquals(3, it.oldQuantity)
            Assertions.assertEquals(11, it.price)
            Assertions.assertEquals(3, it.quantity)
            Assertions.assertEquals(3, it.remainedQuantity)
        }
        (editEvents[1] as TradeEvent).also {
            Assertions.assertEquals(bidOuid, it.takerOuid)
            Assertions.assertEquals(bidOrder.id(), it.takerOrderId)
            Assertions.assertEquals(askOuid, it.makerOuid)
            Assertions.assertEquals(askOrder.id(), it.makerOrderId)
            Assertions.assertEquals(11, it.takerPrice)
            Assertions.assertEquals(10, it.makerPrice)
            Assertions.assertEquals(1, it.takerRemainedQuantity)
            Assertions.assertEquals(0, it.makerRemainedQuantity)
            Assertions.assertEquals(2, it.matchedQuantity)
        }
    }

    @Test
    fun givenIocBidOrderPartiallyFills_whenCreated_thenCancelEventUsesOrderState() {
        val orderBook = SimpleOrderBook(pair, false)
        val askOuid = UUID.randomUUID().toString()
        val askOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                askOuid,
                UUID.randomUUID().toString(),
                pair,
                10,
                2,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )!!
        val bidOuid = UUID.randomUUID().toString()
        val createEvents = mutableListOf<CoreEvent>()
        EventDispatcher.register(CoreEvent::class.java) { createEvents.add(it) }

        val bidOrder = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                bidOuid,
                uuid,
                pair,
                11,
                5,
                OrderDirection.BID,
                MatchConstraint.IOC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder

        Assertions.assertEquals(2, bidOrder.filledQuantity)
        Assertions.assertEquals(3, bidOrder.remainedQuantity())
        Assertions.assertEquals(0, orderBook.orders.size)
        Assertions.assertNull(orderBook.bestAskOrder)
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertEquals(
            listOf(
                CreateOrderEvent::class.java,
                TradeEvent::class.java,
                CancelOrderEvent::class.java,
                OrderBookPublishedEvent::class.java
            ),
            createEvents.map { it::class.java }
        )
        (createEvents[0] as CreateOrderEvent).also {
            Assertions.assertEquals(bidOuid, it.ouid)
            Assertions.assertEquals(11, it.price)
            Assertions.assertEquals(5, it.quantity)
            Assertions.assertEquals(5, it.remainedQuantity)
        }
        (createEvents[1] as TradeEvent).also {
            Assertions.assertEquals(bidOuid, it.takerOuid)
            Assertions.assertEquals(bidOrder.id(), it.takerOrderId)
            Assertions.assertEquals(askOuid, it.makerOuid)
            Assertions.assertEquals(askOrder.id(), it.makerOrderId)
            Assertions.assertEquals(11, it.takerPrice)
            Assertions.assertEquals(10, it.makerPrice)
            Assertions.assertEquals(3, it.takerRemainedQuantity)
            Assertions.assertEquals(0, it.makerRemainedQuantity)
            Assertions.assertEquals(2, it.matchedQuantity)
        }
        (createEvents[2] as CancelOrderEvent).also {
            Assertions.assertEquals(bidOuid, it.ouid)
            Assertions.assertEquals(uuid, it.uuid)
            Assertions.assertEquals(bidOrder.id(), it.orderId)
            Assertions.assertEquals(11, it.price)
            Assertions.assertEquals(5, it.quantity)
            Assertions.assertEquals(3, it.remainedQuantity)
            Assertions.assertEquals(OrderDirection.BID, it.direction)
            Assertions.assertEquals(MatchConstraint.IOC, it.matchConstraint)
            Assertions.assertEquals(OrderType.LIMIT_ORDER, it.orderType)
        }
    }

    @Test
    fun givenOrderBookWithBidAndAskOrders_whenEditABidOrder_thenRefill() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val secondBidOuid = UUID.randomUUID().toString()
        val secondBid = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                secondBidOuid,
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                3,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        //when
        val order: SimpleOrder = orderBook.handleEditCommand(
            OrderEditCommand(
                secondBidOuid,
                uuid,
                secondBid!!.id()!!,
                pair,
                3,
                3
            )
        ) as SimpleOrder
        //then
        Assertions.assertEquals(2, orderBook.bidOrders.entriesList().size)
        Assertions.assertEquals(0, orderBook.askOrders.entriesList().size)
        Assertions.assertEquals(orderBook.bestBidOrder, order)
        Assertions.assertNull(orderBook.bestAskOrder)
    }

    @Test
    fun givenEmptyOrderBook_whenGtcBidMarketOrderCreated_thenRejected() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        //when

        val order = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.MARKET_ORDER
            )
        )
        //then
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 0)
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertNull(order)
    }

    @Test
    fun givenEmptyOrderBook_whenIocBidMarketOrderCreated_thenNoOrderCreated() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        //when

        val order = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.MARKET_ORDER
            )
        )
        //then
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 0)
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertNull(order)
    }

    @Test
    fun givenOrderBookWithBidAndAskOrders_whenIocAskMarketOrderWithGreaterQuantityCreated_thenPartiallyFilled() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                3,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val bestAskOrder = orderBook.bestAskOrder
        //when
        val order: SimpleOrder =
            orderBook.handleNewOrderCommand(
                OrderCreateCommand(
                    UUID.randomUUID().toString(),
                    uuid,
                    pair,
                    0,
                    3,
                    OrderDirection.ASK,
                    MatchConstraint.IOC,
                    OrderType.MARKET_ORDER
                )
            ) as SimpleOrder
        //then
        Assertions.assertEquals(2, order.filledQuantity)
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 0)
        Assertions.assertEquals(orderBook.askOrders.entriesList().size, 1)
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertEquals(orderBook.bestAskOrder, bestAskOrder)
    }

    @Test
    fun givenOrderBookWithAskAboveMarketBidCap_whenIocBidMarketOrderCreated_thenOnlyMatchAskAtOrBelowCap() {
        val orderBook = SimpleOrderBook(pair, false)
        val lowAsk = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                90,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        val highAsk = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                UUID.randomUUID().toString(),
                pair,
                110,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder

        val order = orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                UUID.randomUUID().toString(),
                pair,
                100,
                2,
                OrderDirection.BID,
                MatchConstraint.IOC,
                OrderType.MARKET_ORDER
            )
        ) as SimpleOrder

        Assertions.assertEquals(1, order.filledQuantity)
        Assertions.assertEquals(0, lowAsk.remainedQuantity())
        Assertions.assertEquals(1, highAsk.remainedQuantity())
        Assertions.assertEquals(highAsk, orderBook.bestAskOrder)
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertEquals(1, orderBook.askOrders.entriesList().size)
    }

    @Test
    fun givenOrderBookWithBidAndAskOrders_whenIocAskLimitOrderWithHigherPriceAndGreaterQuantityCreated_thenNotFilled() {
        //given
        val orderBook = SimpleOrderBook(pair, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                2,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                1,
                1,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                pair,
                3,
                1,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        )
        val bestAskOrder = orderBook.bestAskOrder
        val bestBidOrder = orderBook.bestBidOrder
        //when
        val order: SimpleOrder =
            orderBook.handleNewOrderCommand(
                OrderCreateCommand(
                    UUID.randomUUID().toString(),
                    uuid,
                    pair,
                    3,
                    3,
                    OrderDirection.ASK,
                    MatchConstraint.IOC,
                    OrderType.LIMIT_ORDER
                )
            ) as SimpleOrder
        //then
        Assertions.assertEquals(0, order.filledQuantity)
        Assertions.assertEquals(orderBook.bidOrders.entriesList().size, 2)
        Assertions.assertEquals(orderBook.askOrders.entriesList().size, 1)
        Assertions.assertEquals(bestBidOrder, orderBook.bestBidOrder)
        Assertions.assertEquals(bestAskOrder, orderBook.bestAskOrder)
    }

    @Test
    fun whenSample1SequenceOfOrdersOccurs_thenAllSuccess() {

        val orderBook = SimpleOrderBook(ETH_BTC_PAIR, false)
        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                ETH_BTC_PAIR,
                5000000,
                10000,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        Assertions.assertNotNull(orderBook.bestBidOrder)
        Assertions.assertEquals(1, orderBook.bidOrders.entriesList().size)
        Assertions.assertEquals(1, orderBook.orders.size)

        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                ETH_BTC_PAIR,
                4900000,
                20000,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertNotNull(orderBook.bestAskOrder)
        Assertions.assertEquals(0, orderBook.bidOrders.entriesList().size)
        Assertions.assertEquals(1, orderBook.askOrders.entriesList().size)
        Assertions.assertEquals(1, orderBook.orders.size)

        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                ETH_BTC_PAIR,
                4800000,
                10000,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        Assertions.assertNull(orderBook.bestBidOrder)
        Assertions.assertNotNull(orderBook.bestAskOrder)
        Assertions.assertEquals(0, orderBook.bidOrders.entriesList().size)
        Assertions.assertEquals(2, orderBook.askOrders.entriesList().size)
        Assertions.assertEquals(2, orderBook.orders.size)

        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                ETH_BTC_PAIR,
                4850000,
                20000,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        Assertions.assertEquals(1, orderBook.bidOrders.entriesList().size)
        Assertions.assertEquals(1, orderBook.askOrders.entriesList().size)
        Assertions.assertEquals(2, orderBook.orders.size)
        Assertions.assertNotNull(orderBook.bestBidOrder)
        Assertions.assertNotNull(orderBook.bestAskOrder)

        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                ETH_BTC_PAIR,
                4850100,
                10000,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        Assertions.assertEquals(1, orderBook.bidOrders.entriesList().size)
        Assertions.assertEquals(2, orderBook.askOrders.entriesList().size)
        Assertions.assertEquals(3, orderBook.orders.size)
        Assertions.assertNotNull(orderBook.bestBidOrder)
        Assertions.assertNotNull(orderBook.bestAskOrder)

        orderBook.handleNewOrderCommand(
            OrderCreateCommand(
                UUID.randomUUID().toString(),
                uuid,
                ETH_BTC_PAIR,
                4849900,
                10000,
                OrderDirection.BID,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )
        ) as SimpleOrder
        Assertions.assertEquals(2, orderBook.bidOrders.entriesList().size)
        Assertions.assertEquals(2, orderBook.askOrders.entriesList().size)
        Assertions.assertEquals(4, orderBook.orders.size)
        Assertions.assertNotNull(orderBook.bestBidOrder)
        Assertions.assertNotNull(orderBook.bestAskOrder)
    }
}
