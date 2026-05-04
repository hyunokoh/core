package co.nilin.opex.accountant.core.service

import co.nilin.opex.accountant.core.inout.OrderStatus
import co.nilin.opex.accountant.core.model.*
import co.nilin.opex.matching.engine.core.eventh.events.CancelOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.SubmitOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.TradeEvent
import co.nilin.opex.matching.engine.core.model.MatchConstraint
import co.nilin.opex.matching.engine.core.model.OrderDirection
import co.nilin.opex.matching.engine.core.model.OrderType
import co.nilin.opex.matching.engine.core.model.Pair
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.Mutex
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import java.math.BigDecimal

internal class TradeManagerImplTest {

    private val financialActionStore = RecordingFinancialActionStore()
    private val orderPersister = InMemoryOrderPersister()
    private val pairConfigLoader = MapPairConfigLoader()
    private val tempEventPersister = InMemoryTempEventPersister()
    private val richOrderPublisher = RecordingRichOrderPublisher()
    private val richTradePublisher = RecordingRichTradePublisher()
    private val userLevelLoader = StaticUserLevelLoader()
    private val financialActionPublisher = RecordingFinancialActionPublisher()
    private val jsonMapper = JsonMapperTestImpl()
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
        jsonMapper,
        processedEventPersister
    )

    private val tradeManager = TradeManagerImpl(
        financialActionStore,
        financialActionStore,
        orderPersister,
        tempEventPersister,
        richTradePublisher,
        richOrderPublisher,
        FeeCalculatorImpl("0x0", jsonMapper),
        financialActionPublisher,
        jsonMapper,
        processedEventPersister
    )

    @Test
    fun givenSellOrder_WhenMatchBuyOrderCome_thenFAMatched(): Unit = runBlocking {
        //given
        val pair = Pair("eth", "btc")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.valueOf(1.0),
            BigDecimal.valueOf(0.01)
        )
        val makerSubmitOrderEvent = SubmitOrderEvent(
            "mouid",
            "muuid",
            null,
            pair,
            60000,
            2,
            2,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        prepareOrder(pairConfig, makerSubmitOrderEvent, BigDecimal.valueOf(0.1), BigDecimal.valueOf(0.12))

        val takerSubmitOrderEvent = SubmitOrderEvent(
            "touid",
            "tuuid",
            null,
            pair,
            70000,
            2,
            2,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )

        prepareOrder(pairConfig, takerSubmitOrderEvent, BigDecimal.valueOf(0.08), BigDecimal.valueOf(0.1))

        val tradeEvent = makeTradeEvent(pair, takerSubmitOrderEvent, makerSubmitOrderEvent, 1)
        //when
        val tradeFinancialActions = tradeManager.handleTrade(tradeEvent)

        assertThat(tradeFinancialActions.size).isEqualTo(4)
        assertThat(tradeFinancialActions[0].category).isEqualTo(FinancialActionCategory.TRADE)
        assertThat(tradeFinancialActions[1].category).isEqualTo(FinancialActionCategory.TRADE)
        assertThat(tradeFinancialActions[2].category).isEqualTo(FinancialActionCategory.FEE)
        assertThat(tradeFinancialActions[3].category).isEqualTo(FinancialActionCategory.FEE)

        assertThat((makerSubmitOrderEvent.price.toBigDecimal() * pairConfig.rightSideFraction).stripTrailingZeros())
            .isEqualTo(tradeFinancialActions[0].amount.stripTrailingZeros())
    }

    @Test
    fun givenBuyOrder_whenMatchSellOrderCome_thenFAMatched(): Unit = runBlocking {
        //given
        val pair = Pair("eth", "btc")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.valueOf(1.0),
            BigDecimal.valueOf(0.001)
        )
        val makerSubmitOrderEvent = SubmitOrderEvent(
            "mouid",
            "muuid",
            null,
            pair,
            70000,
            2,
            2,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        prepareOrder(pairConfig, makerSubmitOrderEvent, BigDecimal.valueOf(0.1), BigDecimal.valueOf(0.12))

        val takerSubmitOrderEvent = SubmitOrderEvent(
            "touid",
            "tuuid",
            null,
            pair,
            60000,
            2,
            2,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )

        prepareOrder(pairConfig, takerSubmitOrderEvent, BigDecimal.valueOf(0.08), BigDecimal.valueOf(0.1))

        val tradeEvent = makeTradeEvent(pair, takerSubmitOrderEvent, makerSubmitOrderEvent, 1)
        //when
        val tradeFinancialActions = tradeManager.handleTrade(tradeEvent)

        assertThat(tradeFinancialActions.size).isEqualTo(4)
        assertThat((makerSubmitOrderEvent.price.toBigDecimal() * pairConfig.rightSideFraction).stripTrailingZeros())
            .isEqualTo(tradeFinancialActions[1].amount.stripTrailingZeros())
    }

    @Test
    fun givenSellOrderWith1Remains_whenMatchBuyOrderCome_thenFAMatched(): Unit = runBlocking {
        //given
        val pair = Pair("btc", "eth")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.valueOf(1.0),
            BigDecimal.valueOf(0.01)
        )
        val makerSubmitOrderEvent = SubmitOrderEvent(
            "mouid",
            "muuid",
            null,
            pair,
            60000,
            1,
            1,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        prepareOrder(pairConfig, makerSubmitOrderEvent, BigDecimal.valueOf(0.1), BigDecimal.valueOf(0.12))

        val takerSubmitOrderEvent = SubmitOrderEvent(
            "touid",
            "tuuid",
            null,
            pair,
            70000,
            2,
            2,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )

        prepareOrder(pairConfig, takerSubmitOrderEvent, BigDecimal.valueOf(0.08), BigDecimal.valueOf(0.1))

        val tradeEvent = makeTradeEvent(pair, takerSubmitOrderEvent, makerSubmitOrderEvent, 1)
        //when
        val tradeFinancialActions = tradeManager.handleTrade(tradeEvent)

        assertThat(tradeFinancialActions.size).isEqualTo(4)
        assertThat(tradeFinancialActions[0].category).isEqualTo(FinancialActionCategory.TRADE)
        assertThat(tradeFinancialActions[1].category).isEqualTo(FinancialActionCategory.TRADE)
        assertThat(tradeFinancialActions[2].category).isEqualTo(FinancialActionCategory.FEE)
        assertThat(tradeFinancialActions[3].category).isEqualTo(FinancialActionCategory.FEE)

        assertThat((makerSubmitOrderEvent.price.toBigDecimal() * pairConfig.rightSideFraction).stripTrailingZeros())
            .isEqualTo(tradeFinancialActions[0].amount.stripTrailingZeros())
    }

    @Test
    fun givenSellOrder_whenMatchBuyOrderWith1RemainsCome_thenFAMatched(): Unit = runBlocking {
        //given
        val pair = Pair("btc", "eth")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.valueOf(1.0),
            BigDecimal.valueOf(0.01)
        )
        val makerSubmitOrderEvent = SubmitOrderEvent(
            "mouid",
            "muuid",
            null,
            pair,
            60000,
            2,
            2,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        prepareOrder(pairConfig, makerSubmitOrderEvent, BigDecimal.valueOf(0.1), BigDecimal.valueOf(0.12))

        val takerSubmitOrderEvent = SubmitOrderEvent(
            "touid",
            "tuuid",
            null,
            pair,
            70000,
            1,
            1,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )

        prepareOrder(pairConfig, takerSubmitOrderEvent, BigDecimal.valueOf(0.08), BigDecimal.valueOf(0.1))

        val tradeEvent = makeTradeEvent(pair, takerSubmitOrderEvent, makerSubmitOrderEvent, 1)
        //when
        val tradeFinancialActions = tradeManager.handleTrade(tradeEvent)

        assertThat(tradeFinancialActions.size).isEqualTo(5)
        assertThat(tradeFinancialActions[0].category).isEqualTo(FinancialActionCategory.TRADE)
        assertThat(tradeFinancialActions[1].category).isEqualTo(FinancialActionCategory.ORDER_FINALIZED)
        assertThat(tradeFinancialActions[2].category).isEqualTo(FinancialActionCategory.TRADE)
        assertThat(tradeFinancialActions[3].category).isEqualTo(FinancialActionCategory.FEE)
        assertThat(tradeFinancialActions[4].category).isEqualTo(FinancialActionCategory.FEE)

        assertThat((makerSubmitOrderEvent.price.toBigDecimal() * pairConfig.rightSideFraction).stripTrailingZeros())
            .isEqualTo(tradeFinancialActions[0].amount.stripTrailingZeros())
    }

    @Test
    fun givenConcurrentTradesForSameMarketBid_whenOrderFills_thenFinalizeSurplusOnce(): Unit = runBlocking {
        val concurrentOrderPersister = CopyingInMemoryOrderPersister()
        val concurrentFinancialActionStore = RecordingFinancialActionStore()
        val concurrentOrderManager = OrderManagerImpl(
            pairConfigLoader,
            userLevelLoader,
            concurrentFinancialActionStore,
            concurrentFinancialActionStore,
            concurrentOrderPersister,
            tempEventPersister,
            richOrderPublisher,
            financialActionPublisher,
            jsonMapper,
            RecordingProcessedEventPersister()
        )
        val concurrentTradeManager = TradeManagerImpl(
            concurrentFinancialActionStore,
            concurrentFinancialActionStore,
            concurrentOrderPersister,
            tempEventPersister,
            richTradePublisher,
            richOrderPublisher,
            FeeCalculatorImpl("0x0", jsonMapper),
            financialActionPublisher,
            jsonMapper,
            RecordingProcessedEventPersister()
        )
        val pair = Pair("ETH", "USDT")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.ONE,
            BigDecimal.valueOf(0.01)
        )
        val takerBid = SubmitOrderEvent(
            "taker-ouid",
            "taker-uuid",
            null,
            pair,
            20000,
            300000,
            300000,
            OrderDirection.BID,
            MatchConstraint.IOC,
            OrderType.MARKET_ORDER
        )
        val lowAsk = SubmitOrderEvent(
            "low-ask-ouid",
            "low-ask-uuid",
            null,
            pair,
            9000,
            100000,
            100000,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        val highAsk = SubmitOrderEvent(
            "high-ask-ouid",
            "high-ask-uuid",
            null,
            pair,
            10000,
            200000,
            200000,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )

        prepareOrder(
            pairConfig,
            takerBid,
            BigDecimal.valueOf(0.01),
            BigDecimal.valueOf(0.01),
            concurrentOrderManager,
            concurrentOrderPersister
        )
        prepareOrder(
            pairConfig,
            lowAsk,
            BigDecimal.valueOf(0.01),
            BigDecimal.valueOf(0.01),
            concurrentOrderManager,
            concurrentOrderPersister
        )
        prepareOrder(
            pairConfig,
            highAsk,
            BigDecimal.valueOf(0.01),
            BigDecimal.valueOf(0.01),
            concurrentOrderManager,
            concurrentOrderPersister
        )

        val firstTrade = makeTradeEvent(pair, takerBid, lowAsk, 100000)
        val secondTrade = makeTradeEvent(pair, takerBid, highAsk, 200000)

        val financialActions = listOf(
            async { concurrentTradeManager.handleTrade(firstTrade) },
            async { concurrentTradeManager.handleTrade(secondTrade) }
        ).awaitAll().flatten()

        val takerOrder = concurrentOrderPersister.orders.getValue(takerBid.ouid)
        assertThat(takerOrder.filledQuantity).isEqualTo(takerOrder.quantity)
        assertThat(takerOrder.remainedTransferAmount).isEqualByComparingTo(BigDecimal.ZERO)

        val finalizers = financialActions.filter { it.category == FinancialActionCategory.ORDER_FINALIZED }
        assertThat(finalizers).hasSize(1)
        assertThat(finalizers.single().amount).isEqualByComparingTo(BigDecimal.valueOf(31_000_000))
    }

    @Test
    fun givenIocMarketBidCancelArrivesBeforeTrade_whenTradeHandled_thenReplayCancelAndReleaseRemainder(): Unit =
        runBlocking {
            val replayingTradeManager = TradeManagerImpl(
                financialActionStore,
                financialActionStore,
                orderPersister,
                tempEventPersister,
                richTradePublisher,
                richOrderPublisher,
                FeeCalculatorImpl("0x0", jsonMapper),
                financialActionPublisher,
                jsonMapper,
                processedEventPersister,
                orderManager
            )
            val pair = Pair("ETH", "USDT")
            val pairConfig = PairConfig(
                pair.toString(),
                pair.leftSideName,
                pair.rightSideName,
                BigDecimal.ONE,
                BigDecimal.valueOf(0.01)
            )
            val takerBid = SubmitOrderEvent(
                "market-bid-ouid",
                "market-bid-uuid",
                null,
                pair,
                9500,
                200000,
                200000,
                OrderDirection.BID,
                MatchConstraint.IOC,
                OrderType.MARKET_ORDER
            )
            val lowAsk = SubmitOrderEvent(
                "low-ask-ouid",
                "low-ask-uuid",
                null,
                pair,
                9000,
                100000,
                100000,
                OrderDirection.ASK,
                MatchConstraint.GTC,
                OrderType.LIMIT_ORDER
            )

            prepareOrder(pairConfig, takerBid, BigDecimal.valueOf(0.01), BigDecimal.valueOf(0.01))
            prepareOrder(pairConfig, lowAsk, BigDecimal.valueOf(0.01), BigDecimal.valueOf(0.01))

            val cancelEvent = CancelOrderEvent(
                takerBid.ouid,
                takerBid.uuid,
                48,
                pair,
                takerBid.price,
                takerBid.quantity,
                100000,
                takerBid.direction,
                takerBid.matchConstraint,
                takerBid.orderType
            )
            assertThat(orderManager.handleCancelOrder(cancelEvent)).isEmpty()
            assertThat(tempEventPersister.loadTempEvents(takerBid.ouid)).containsExactly(cancelEvent)

            replayingTradeManager.handleTrade(makeTradeEvent(pair, takerBid, lowAsk, 100000))

            val takerOrder = orderPersister.orders.getValue(takerBid.ouid)
            assertThat(takerOrder.status).isEqualTo(OrderStatus.CANCELED.code)
            assertThat(tempEventPersister.loadTempEvents(takerBid.ouid)).isEmpty()
            val releaseAction = financialActionStore.persisted.single {
                it.pointer == takerBid.ouid && it.category == FinancialActionCategory.ORDER_CANCEL
            }
            assertThat(releaseAction.amount).isEqualByComparingTo(BigDecimal.valueOf(10_000_000))
            assertThat(releaseAction.sender).isEqualTo(takerBid.uuid)
            assertThat(releaseAction.senderWalletType).isEqualTo(WalletType.EXCHANGE)
            assertThat(releaseAction.receiver).isEqualTo(takerBid.uuid)
            assertThat(releaseAction.receiverWalletType).isEqualTo(WalletType.MAIN)
        }

    @Test
    fun givenDuplicateTradeEvent_whenHandledAgain_thenIgnoredWithoutExtraFinancialActions(): Unit = runBlocking {
        val pair = Pair("eth", "btc")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.valueOf(1.0),
            BigDecimal.valueOf(0.01)
        )
        val makerSubmitOrderEvent = SubmitOrderEvent(
            "dup-maker-ouid",
            "dup-maker-uuid",
            null,
            pair,
            60000,
            2,
            2,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        val takerSubmitOrderEvent = SubmitOrderEvent(
            "dup-taker-ouid",
            "dup-taker-uuid",
            null,
            pair,
            70000,
            2,
            2,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        prepareOrder(pairConfig, makerSubmitOrderEvent, BigDecimal.valueOf(0.1), BigDecimal.valueOf(0.12))
        prepareOrder(pairConfig, takerSubmitOrderEvent, BigDecimal.valueOf(0.08), BigDecimal.valueOf(0.1))

        val tradeEvent = makeTradeEvent(pair, takerSubmitOrderEvent, makerSubmitOrderEvent, 1)
        val firstResult = tradeManager.handleTrade(tradeEvent)
        val persistedCountAfterFirst = financialActionStore.persisted.size
        val takerFilledAfterFirst = orderPersister.orders.getValue(takerSubmitOrderEvent.ouid).filledQuantity
        val makerFilledAfterFirst = orderPersister.orders.getValue(makerSubmitOrderEvent.ouid).filledQuantity

        val secondResult = tradeManager.handleTrade(tradeEvent)

        assertThat(firstResult).hasSize(4)
        assertThat(secondResult).isEmpty()
        assertThat(financialActionStore.persisted).hasSize(persistedCountAfterFirst)
        assertThat(orderPersister.orders.getValue(takerSubmitOrderEvent.ouid).filledQuantity)
            .isEqualTo(takerFilledAfterFirst)
        assertThat(orderPersister.orders.getValue(makerSubmitOrderEvent.ouid).filledQuantity)
            .isEqualTo(makerFilledAfterFirst)
    }

    @Test
    fun givenInvalidTradeEvent_whenHandled_thenIgnoredBeforeStateMutation(): Unit = runBlocking {
        val pair = Pair("eth", "btc")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.valueOf(1.0),
            BigDecimal.valueOf(0.01)
        )
        val makerSubmitOrderEvent = SubmitOrderEvent(
            "invalid-maker-ouid",
            "invalid-maker-uuid",
            null,
            pair,
            60000,
            2,
            2,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        val takerSubmitOrderEvent = SubmitOrderEvent(
            "invalid-taker-ouid",
            "invalid-taker-uuid",
            null,
            pair,
            70000,
            2,
            2,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        prepareOrder(pairConfig, makerSubmitOrderEvent, BigDecimal.valueOf(0.1), BigDecimal.valueOf(0.12))
        prepareOrder(pairConfig, takerSubmitOrderEvent, BigDecimal.valueOf(0.08), BigDecimal.valueOf(0.1))
        val persistedCountBefore = financialActionStore.persisted.size

        val invalidTrade = makeTradeEvent(pair, takerSubmitOrderEvent, makerSubmitOrderEvent, -1)
        val result = tradeManager.handleTrade(invalidTrade)

        assertThat(result).isEmpty()
        assertThat(financialActionStore.persisted).hasSize(persistedCountBefore)
        assertThat(processedEventPersister.processed).isEmpty()
        assertThat(tempEventPersister.saved).isEmpty()
        assertThat(richTradePublisher.published).isEmpty()
        assertThat(orderPersister.orders.getValue(takerSubmitOrderEvent.ouid).filledQuantity).isZero()
        assertThat(orderPersister.orders.getValue(makerSubmitOrderEvent.ouid).filledQuantity).isZero()
    }

    @Test
    fun givenTradeBeforeBothOrdersExist_whenReplayedFromTempEvents_thenProcessedAfterOrdersExist(): Unit = runBlocking {
        val localProcessedEventPersister = RecordingProcessedEventPersister()
        val localTradeManager = TradeManagerImpl(
            financialActionStore,
            financialActionStore,
            orderPersister,
            tempEventPersister,
            richTradePublisher,
            richOrderPublisher,
            FeeCalculatorImpl("0x0", jsonMapper),
            financialActionPublisher,
            jsonMapper,
            localProcessedEventPersister
        )
        val pair = Pair("eth", "btc")
        val pairConfig = PairConfig(
            pair.toString(),
            pair.leftSideName,
            pair.rightSideName,
            BigDecimal.valueOf(1.0),
            BigDecimal.valueOf(0.01)
        )
        val makerSubmitOrderEvent = SubmitOrderEvent(
            "temp-maker-ouid",
            "temp-maker-uuid",
            null,
            pair,
            60000,
            2,
            2,
            OrderDirection.ASK,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        val takerSubmitOrderEvent = SubmitOrderEvent(
            "temp-taker-ouid",
            "temp-taker-uuid",
            null,
            pair,
            70000,
            2,
            2,
            OrderDirection.BID,
            MatchConstraint.GTC,
            OrderType.LIMIT_ORDER
        )
        prepareOrder(pairConfig, makerSubmitOrderEvent, BigDecimal.valueOf(0.1), BigDecimal.valueOf(0.12))

        val tradeEvent = makeTradeEvent(pair, takerSubmitOrderEvent, makerSubmitOrderEvent, 1)
        val deferredResult = localTradeManager.handleTrade(tradeEvent)

        assertThat(deferredResult).isEmpty()
        assertThat(tempEventPersister.saved.map { it.ouid }).contains(takerSubmitOrderEvent.ouid)
        assertThat(localProcessedEventPersister.processed).isEmpty()

        prepareOrder(pairConfig, takerSubmitOrderEvent, BigDecimal.valueOf(0.08), BigDecimal.valueOf(0.1))
        val replayResult = localTradeManager.handleTrade(tradeEvent)

        assertThat(replayResult).hasSize(4)
        assertThat(localProcessedEventPersister.processed).hasSize(1)
    }

    private fun makeTradeEvent(
        pair: Pair,
        takerSubmitOrderEvent: SubmitOrderEvent,
        makerSubmitOrderEvent: SubmitOrderEvent,
        matchedQuantity: Long
    ): TradeEvent {
        return TradeEvent(
            1,
            pair,
            takerSubmitOrderEvent.ouid,
            takerSubmitOrderEvent.uuid,
            takerSubmitOrderEvent.orderId ?: -1,
            takerSubmitOrderEvent.direction,
            takerSubmitOrderEvent.price,
            takerSubmitOrderEvent.remainedQuantity,
            makerSubmitOrderEvent.ouid,
            makerSubmitOrderEvent.uuid,
            makerSubmitOrderEvent.orderId ?: 1,
            makerSubmitOrderEvent.direction,
            makerSubmitOrderEvent.price,
            makerSubmitOrderEvent.remainedQuantity,
            matchedQuantity
        )
    }

    private suspend fun prepareOrder(
        pairConfig: PairConfig,
        submitOrderEvent: SubmitOrderEvent,
        makerFee: BigDecimal,
        takerFee: BigDecimal
    ) {
        pairConfigLoader.put(
            pairConfig,
            submitOrderEvent.direction,
            "*",
            makerFee,
            takerFee
        )

        val financialActions = orderManager.handleRequestOrder(submitOrderEvent)

        val orderPairFeeConfig = pairConfigLoader.load(
            submitOrderEvent.pair.toString(),
            submitOrderEvent.direction,
            "*"
        )
        val orderMakerFee = orderPairFeeConfig.makerFee * BigDecimal.ONE //user level formula
        val orderTakerFee = orderPairFeeConfig.takerFee * BigDecimal.ONE //user level formula

        orderPersister.orders[submitOrderEvent.ouid] = Order(
            submitOrderEvent.pair.toString(),
            submitOrderEvent.ouid,
            null,
            orderMakerFee,
            orderTakerFee,
            orderPairFeeConfig.pairConfig.leftSideFraction,
            orderPairFeeConfig.pairConfig.rightSideFraction,
            submitOrderEvent.uuid,
            submitOrderEvent.userLevel,
            submitOrderEvent.direction,
            submitOrderEvent.matchConstraint,
            submitOrderEvent.orderType,
            submitOrderEvent.price,
            submitOrderEvent.quantity,
            submitOrderEvent.quantity - submitOrderEvent.remainedQuantity,
            submitOrderEvent.price.toBigDecimal(),
            submitOrderEvent.quantity.toBigDecimal(),
            (submitOrderEvent.quantity - submitOrderEvent.remainedQuantity).toBigDecimal(),
            financialActions[0].amount,
            financialActions[0].amount,
            0
        )
    }

    private suspend fun prepareOrder(
        pairConfig: PairConfig,
        submitOrderEvent: SubmitOrderEvent,
        makerFee: BigDecimal,
        takerFee: BigDecimal,
        orderManager: OrderManagerImpl,
        orderPersister: CopyingInMemoryOrderPersister
    ) {
        pairConfigLoader.put(
            pairConfig,
            submitOrderEvent.direction,
            "*",
            makerFee,
            takerFee
        )

        val financialActions = orderManager.handleRequestOrder(submitOrderEvent)
        val orderPairFeeConfig = pairConfigLoader.load(
            submitOrderEvent.pair.toString(),
            submitOrderEvent.direction,
            "*"
        )
        orderPersister.orders[submitOrderEvent.ouid] = Order(
            submitOrderEvent.pair.toString(),
            submitOrderEvent.ouid,
            null,
            orderPairFeeConfig.makerFee,
            orderPairFeeConfig.takerFee,
            orderPairFeeConfig.pairConfig.leftSideFraction,
            orderPairFeeConfig.pairConfig.rightSideFraction,
            submitOrderEvent.uuid,
            submitOrderEvent.userLevel,
            submitOrderEvent.direction,
            submitOrderEvent.matchConstraint,
            submitOrderEvent.orderType,
            submitOrderEvent.price,
            submitOrderEvent.quantity,
            submitOrderEvent.quantity - submitOrderEvent.remainedQuantity,
            submitOrderEvent.price.toBigDecimal().multiply(orderPairFeeConfig.pairConfig.rightSideFraction),
            submitOrderEvent.quantity.toBigDecimal().multiply(orderPairFeeConfig.pairConfig.leftSideFraction),
            (submitOrderEvent.quantity - submitOrderEvent.remainedQuantity).toBigDecimal(),
            financialActions[0].amount,
            financialActions[0].amount,
            0
        )
    }

    private class CopyingInMemoryOrderPersister : InMemoryOrderPersister() {
        private val locks = mutableMapOf<String, Mutex>()
        private val heldLocks = mutableMapOf<String, Mutex>()

        override suspend fun loadForUpdate(ouid: String): Order? {
            val lock = locks.getOrPut(ouid) { Mutex() }
            lock.lock()
            heldLocks[ouid] = lock
            delay(25)
            val order = orders[ouid]?.copy()
            if (order == null) {
                heldLocks.remove(ouid)
                lock.unlock()
            }
            return order
        }

        override suspend fun load(ouid: String): Order? {
            delay(25)
            return orders[ouid]?.copy()
        }

        override suspend fun save(order: Order): Order {
            delay(25)
            orders[order.ouid] = order.copy()
            saved.add(order.copy())
            heldLocks.remove(order.ouid)?.unlock()
            return order
        }
    }
}
