package co.nilin.opex.accountant.core.service

import co.nilin.opex.accountant.core.inout.RichOrderEvent
import co.nilin.opex.accountant.core.inout.RichTrade
import co.nilin.opex.accountant.core.model.FinancialAction
import co.nilin.opex.accountant.core.model.FinancialActionStatus
import co.nilin.opex.accountant.core.model.KycLevel
import co.nilin.opex.accountant.core.model.Order
import co.nilin.opex.accountant.core.model.PairConfig
import co.nilin.opex.accountant.core.model.PairFeeConfig
import co.nilin.opex.accountant.core.model.TempEvent
import co.nilin.opex.accountant.core.spi.FinancialActionLoader
import co.nilin.opex.accountant.core.spi.FinancialActionPersister
import co.nilin.opex.accountant.core.spi.FinancialActionPublisher
import co.nilin.opex.accountant.core.spi.OrderPersister
import co.nilin.opex.accountant.core.spi.PairConfigLoader
import co.nilin.opex.accountant.core.spi.ProcessedEventPersister
import co.nilin.opex.accountant.core.spi.RichOrderPublisher
import co.nilin.opex.accountant.core.spi.RichTradePublisher
import co.nilin.opex.accountant.core.spi.TempEventPersister
import co.nilin.opex.accountant.core.spi.UserLevelLoader
import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import co.nilin.opex.matching.engine.core.model.OrderDirection
import java.time.LocalDateTime

internal class RecordingFinancialActionStore : FinancialActionPersister, FinancialActionLoader {
    val persisted = mutableListOf<FinancialAction>()
    val statusByUuid = mutableMapOf<String, FinancialActionStatus>()

    override suspend fun persist(financialActions: List<FinancialAction>): List<FinancialAction> {
        persisted.addAll(financialActions)
        return financialActions
    }

    override suspend fun persistWithStatus(financialAction: FinancialAction, status: FinancialActionStatus) {
        persisted.add(financialAction)
        statusByUuid[financialAction.uuid] = status
    }

    override suspend fun updateWithError(
        financialAction: FinancialAction,
        error: String,
        message: String?,
        body: String?
    ) {
        statusByUuid[financialAction.uuid] = FinancialActionStatus.ERROR
    }

    override suspend fun updateStatus(financialAction: FinancialAction, status: FinancialActionStatus) {
        statusByUuid[financialAction.uuid] = status
    }

    override suspend fun updateStatus(faUuid: String, status: FinancialActionStatus) {
        statusByUuid[faUuid] = status
    }

    override suspend fun updateBatchStatus(financialAction: List<FinancialAction>, status: FinancialActionStatus) {
        financialAction.forEach { statusByUuid[it.uuid] = status }
    }

    override suspend fun updateStatusNewTx(financialAction: FinancialAction, status: FinancialActionStatus) {
        statusByUuid[financialAction.uuid] = status
    }

    override suspend fun retrySuccessful(financialAction: FinancialAction) {
        statusByUuid[financialAction.uuid] = FinancialActionStatus.PROCESSED
    }

    override suspend fun findLast(userUuid: String, ouid: String): FinancialAction? {
        return persisted.lastOrNull { it.pointer == ouid && (it.sender == userUuid || it.receiver == userUuid) }
    }

    override suspend fun loadUnprocessed(offset: Long, size: Long): List<FinancialAction> = emptyList()

    override suspend fun countUnprocessed(userUuid: String, symbol: String, eventType: String): Long = 0

    override suspend fun loadReadyToProcess(offset: Long, size: Long): List<FinancialAction> = emptyList()

    override suspend fun loadFinancialAction(id: Long?): FinancialAction? = persisted.firstOrNull { it.id == id }

    override suspend fun loadRetries(limit: Int): List<FinancialAction> = emptyList()
}

internal open class InMemoryOrderPersister : OrderPersister {
    val orders = mutableMapOf<String, Order>()
    val saved = mutableListOf<Order>()

    override suspend open fun load(ouid: String): Order? = orders[ouid]

    override suspend open fun save(order: Order): Order {
        orders[order.ouid] = order
        saved.add(order)
        return order
    }
}

internal class RecordingProcessedEventPersister : ProcessedEventPersister {
    val processed = mutableSetOf<Pair<String, String>>()

    override suspend fun tryMarkProcessed(eventType: String, eventKey: String): Boolean {
        return processed.add(eventType to eventKey)
    }
}

internal class InMemoryTempEventPersister : TempEventPersister {
    val events = mutableMapOf<String, MutableList<CoreEvent>>()
    val saved = mutableListOf<RecordedTempEvent>()

    override suspend fun saveTempEvent(ouid: String, event: CoreEvent) {
        events.getOrPut(ouid) { mutableListOf() }.add(event)
        saved.add(RecordedTempEvent(ouid, event))
    }

    override suspend fun loadTempEvents(ouid: String): List<CoreEvent> = events[ouid].orEmpty()

    override suspend fun removeTempEvent(ouid: String, event: CoreEvent) {
        events[ouid]?.removeIf { it == event }
        saved.removeIf { it.ouid == ouid && it.event == event }
    }

    override suspend fun removeTempEvents(ouid: String) {
        events.remove(ouid)
    }

    override suspend fun removeTempEvents(tempEvents: List<TempEvent>) {
        tempEvents.forEach { events.remove(it.ouid) }
    }

    override suspend fun fetchTempEvents(offset: Long, size: Long): List<TempEvent> {
        return saved.drop(offset.toInt()).take(size.toInt()).mapIndexed { index, tempEvent ->
            TempEvent(index.toLong(), tempEvent.ouid, tempEvent.event, LocalDateTime.now())
        }
    }
}

internal data class RecordedTempEvent(val ouid: String, val event: CoreEvent)

internal class MapPairConfigLoader : PairConfigLoader {
    private val feeConfigs = mutableListOf<PairFeeConfig>()

    fun put(
        pairConfig: PairConfig,
        direction: OrderDirection,
        userLevel: String,
        makerFee: java.math.BigDecimal,
        takerFee: java.math.BigDecimal
    ) {
        feeConfigs.removeIf {
            it.pairConfig.pair == pairConfig.pair && it.direction == direction.toString() && it.userLevel == userLevel
        }
        feeConfigs.add(PairFeeConfig(pairConfig, direction.toString(), userLevel, makerFee, takerFee))
    }

    override suspend fun loadPairConfigs(): List<PairConfig> = feeConfigs.map { it.pairConfig }.distinctBy { it.pair }

    override suspend fun loadPairFeeConfigs(): List<PairFeeConfig> = feeConfigs

    override suspend fun loadPairFeeConfigs(direction: OrderDirection, userLevel: String): List<PairFeeConfig> {
        return feeConfigs.filter { it.direction == direction.toString() && it.userLevel == userLevel }
    }

    override suspend fun loadPairFeeConfigs(
        pair: String,
        direction: OrderDirection,
        userLevel: String
    ): PairFeeConfig? {
        return feeConfigs.firstOrNull {
            it.pairConfig.pair == pair && it.direction == direction.toString() && it.userLevel == userLevel
        } ?: feeConfigs.firstOrNull {
            it.pairConfig.pair == pair && it.direction == direction.toString()
        }
    }

    override suspend fun load(pair: String, direction: OrderDirection, userLevel: String): PairFeeConfig {
        return loadPairFeeConfigs(pair, direction, userLevel)
            ?: error("Missing pair fee config for pair=$pair direction=$direction userLevel=$userLevel")
    }

    override suspend fun load(pair: String, direction: OrderDirection): PairConfig {
        return feeConfigs.firstOrNull { it.pairConfig.pair == pair && it.direction == direction.toString() }?.pairConfig
            ?: error("Missing pair config for pair=$pair direction=$direction")
    }
}

internal class RecordingRichOrderPublisher : RichOrderPublisher {
    val published = mutableListOf<RichOrderEvent>()

    override suspend fun publish(order: RichOrderEvent) {
        published.add(order)
    }
}

internal class RecordingRichTradePublisher : RichTradePublisher {
    val published = mutableListOf<RichTrade>()

    override suspend fun publish(trade: RichTrade) {
        published.add(trade)
    }
}

internal class StaticUserLevelLoader(private val level: String = "*") : UserLevelLoader {
    override suspend fun load(uuid: String): String = level

    override suspend fun update(uuid: String, userLevel: KycLevel) = Unit
}

internal class RecordingFinancialActionPublisher : FinancialActionPublisher {
    val published = mutableListOf<FinancialAction>()

    override suspend fun publish(fa: FinancialAction) {
        published.add(fa)
    }
}
