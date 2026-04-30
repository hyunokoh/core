package co.nilin.opex.accountant.app.scheduler

import co.nilin.opex.accountant.app.KafkaEnabledTest
import co.nilin.opex.accountant.core.api.FinancialActionJobManager
import co.nilin.opex.accountant.core.model.FinancialAction
import co.nilin.opex.accountant.core.model.FinancialActionCategory
import co.nilin.opex.accountant.core.model.FinancialActionStatus
import co.nilin.opex.accountant.core.model.WalletType
import co.nilin.opex.accountant.core.spi.FinancialActionLoader
import co.nilin.opex.accountant.core.spi.FinancialActionPersister
import co.nilin.opex.accountant.core.spi.WalletProxy
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.context.TestConfiguration
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Primary
import java.math.BigDecimal
import java.time.LocalDateTime
import java.util.Collections
import java.util.UUID

class FinancialActionJobManagerIT : KafkaEnabledTest() {

    @Autowired
    lateinit var financialActionJobManager: FinancialActionJobManager

    @Autowired
    lateinit var financialActionLoader: FinancialActionLoader

    @Autowired
    lateinit var financialActionPersister: FinancialActionPersister

    @Autowired
    lateinit var walletProxy: RecordingWalletProxy

    @BeforeEach
    fun resetWalletProxy() {
        walletProxy.reset()
    }

    @Test
    fun givenFailedParentCreatedChildActions_whenProcessFinancialActions_thenSkipParentAndChild() {
        val uuid = UUID.randomUUID().toString()
        val ouid = UUID.randomUUID().toString()
        val symbol = "SY"
        val parent1 = financialAction(
            name = "Parent",
            ouid = ouid,
            symbol = symbol,
            amount = BigDecimal.TEN,
            uuid = uuid,
            category = FinancialActionCategory.ORDER_CREATE
        )

        runBlocking {
            financialActionPersister.persist(listOf(parent1))
            val parent1Saved = financialActionLoader.findLast(uuid, ouid)!!
            financialActionPersister.updateStatus(parent1Saved, FinancialActionStatus.ERROR)

            val child1 = financialAction(
                parent = parent1Saved,
                name = "Child",
                ouid = ouid,
                symbol = symbol,
                amount = BigDecimal.TEN,
                uuid = uuid,
                senderWalletType = WalletType.EXCHANGE,
                receiverWalletType = WalletType.MAIN,
                category = FinancialActionCategory.TRADE
            )
            val parent2 = financialAction(
                name = "Parent",
                ouid = UUID.randomUUID().toString(),
                symbol = symbol,
                amount = BigDecimal.ONE,
                uuid = uuid,
                category = FinancialActionCategory.ORDER_CREATE
            )

            financialActionPersister.persist(listOf(child1, parent2))
            financialActionJobManager.processFinancialActions(0, 100)

            assertEquals(1, financialActionLoader.countUnprocessed(uuid, symbol, child1.eventType))
            assertEquals(listOf(TransferCall.from(parent2, "accountant:fiActions:${parent2.uuid}")), walletProxy.transfers())
        }
    }

    private fun financialAction(
        parent: FinancialAction? = null,
        name: String,
        ouid: String,
        symbol: String,
        amount: BigDecimal,
        uuid: String,
        senderWalletType: WalletType = WalletType.MAIN,
        receiverWalletType: WalletType = WalletType.EXCHANGE,
        category: FinancialActionCategory
    ): FinancialAction {
        return FinancialAction(
            parent,
            name,
            ouid,
            symbol,
            amount,
            uuid,
            senderWalletType,
            uuid,
            receiverWalletType,
            LocalDateTime.now(),
            category
        )
    }

    @TestConfiguration
    class WalletProxyTestConfig {
        @Bean
        @Primary
        fun walletProxy(): RecordingWalletProxy = RecordingWalletProxy()
    }
}

data class TransferCall(
    val symbol: String,
    val senderWalletType: WalletType,
    val senderUuid: String,
    val receiverWalletType: WalletType,
    val receiverUuid: String,
    val amount: BigDecimal,
    val description: String?,
    val transferRef: String?,
    val transferCategory: String
) {
    companion object {
        fun from(financialAction: FinancialAction, transferRef: String): TransferCall {
            return TransferCall(
                financialAction.symbol,
                financialAction.senderWalletType,
                financialAction.sender,
                financialAction.receiverWalletType,
                financialAction.receiver,
                financialAction.amount,
                financialAction.eventType + financialAction.pointer,
                transferRef,
                financialAction.category.toString()
            )
        }
    }
}

class RecordingWalletProxy : WalletProxy {

    private val transferCalls = Collections.synchronizedList(mutableListOf<TransferCall>())

    override suspend fun transfer(
        symbol: String,
        senderWalletType: WalletType,
        senderUuid: String,
        receiverWalletType: WalletType,
        receiverUuid: String,
        amount: BigDecimal,
        description: String?,
        transferRef: String?,
        transferCategory: String
    ) {
        transferCalls.add(
            TransferCall(
                symbol,
                senderWalletType,
                senderUuid,
                receiverWalletType,
                receiverUuid,
                amount,
                description,
                transferRef,
                transferCategory
            )
        )
    }

    override suspend fun canFulfil(symbol: String, walletType: WalletType, uuid: String, amount: BigDecimal): Boolean = true

    fun transfers(): List<TransferCall> = transferCalls.toList()

    fun reset() {
        transferCalls.clear()
    }
}
