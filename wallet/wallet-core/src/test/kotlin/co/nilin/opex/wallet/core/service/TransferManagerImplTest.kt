package co.nilin.opex.wallet.core.service

import co.nilin.opex.common.OpexError
import co.nilin.opex.utility.error.data.OpexException
import co.nilin.opex.wallet.core.inout.TransferCommand
import co.nilin.opex.wallet.core.model.Amount
import co.nilin.opex.wallet.core.model.BriefWallet
import co.nilin.opex.wallet.core.model.Currency
import co.nilin.opex.wallet.core.model.Transaction
import co.nilin.opex.wallet.core.model.TransactionHistory
import co.nilin.opex.wallet.core.model.TransactionWithDetailHistory
import co.nilin.opex.wallet.core.model.TransferCategory
import co.nilin.opex.wallet.core.model.UserTransaction
import co.nilin.opex.wallet.core.model.UserTransactionCategory
import co.nilin.opex.wallet.core.model.UserTransactionHistory
import co.nilin.opex.wallet.core.model.Wallet
import co.nilin.opex.wallet.core.model.WalletOwner
import co.nilin.opex.wallet.core.model.WalletType
import co.nilin.opex.wallet.core.model.ZkPolLiabilityEvent
import co.nilin.opex.wallet.core.spi.TransactionManager
import co.nilin.opex.wallet.core.spi.UserTransactionManager
import co.nilin.opex.wallet.core.spi.WalletListener
import co.nilin.opex.wallet.core.spi.WalletManager
import co.nilin.opex.wallet.core.spi.WalletOwnerManager
import co.nilin.opex.wallet.core.spi.ZkPolLiabilityExporter
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.Test
import java.math.BigDecimal
import java.time.LocalDateTime

private class TransferManagerImplTest {

    @Test
    fun givenNegativeSourceAmount_whenTransfer_thenRejectBeforeMutatingBalances(): Unit = runBlocking {
        val walletManager = RecordingWalletManager()
        val transferManager = transferManager(walletManager)

        assertThatThrownBy {
            runBlocking {
                transferManager.transfer(command(sourceAmount = BigDecimal("-1")))
            }
        }.isOpexError(OpexError.InvalidAmount)

        assertThat(walletManager.decreaseCalls).isZero()
        assertThat(walletManager.increaseCalls).isZero()
    }

    @Test
    fun givenZeroDestinationAmount_whenTransfer_thenRejectBeforeMutatingBalances(): Unit = runBlocking {
        val walletManager = RecordingWalletManager()
        val transferManager = transferManager(walletManager)

        assertThatThrownBy {
            runBlocking {
                transferManager.transfer(command(destAmount = BigDecimal.ZERO))
            }
        }.isOpexError(OpexError.InvalidAmount)

        assertThat(walletManager.decreaseCalls).isZero()
        assertThat(walletManager.increaseCalls).isZero()
    }

    private fun transferManager(walletManager: RecordingWalletManager): TransferManagerImpl {
        return TransferManagerImpl(
            walletManager,
            NoopWalletListener(),
            AllowingWalletOwnerManager(),
            RecordingTransactionManager(),
            NoopUserTransactionManager(),
            NoopZkPolLiabilityExporter()
        )
    }

    private fun command(
        sourceAmount: BigDecimal = BigDecimal.ONE,
        destAmount: BigDecimal = sourceAmount
    ): TransferCommand {
        return TransferCommand(
            SOURCE_WALLET,
            DEST_WALLET,
            Amount(CURRENCY, sourceAmount),
            null,
            null,
            TransferCategory.NORMAL,
            Amount(CURRENCY, destAmount)
        )
    }

    private fun org.assertj.core.api.AbstractThrowableAssert<*, out Throwable>.isOpexError(error: OpexError) {
        isInstanceOf(OpexException::class.java)
            .extracting("error")
            .isEqualTo(error)
    }

    private class RecordingWalletManager : WalletManager {
        var decreaseCalls = 0
        var increaseCalls = 0

        override suspend fun isDepositAllowed(wallet: Wallet, amount: BigDecimal): Boolean = true

        override suspend fun isWithdrawAllowed(wallet: Wallet, amount: BigDecimal): Boolean = true

        override suspend fun increaseBalance(wallet: Wallet, amount: BigDecimal) {
            increaseCalls += 1
        }

        override suspend fun decreaseBalance(wallet: Wallet, amount: BigDecimal) {
            decreaseCalls += 1
        }

        override suspend fun findWalletByOwnerAndCurrencyAndType(
            owner: WalletOwner,
            walletType: WalletType,
            currency: Currency
        ): Wallet? = null

        override suspend fun findWallet(ownerId: Long, currency: String, walletType: WalletType): BriefWallet? = null

        override suspend fun findWalletsByOwnerAndType(owner: WalletOwner, walletType: WalletType): List<Wallet> =
            emptyList()

        override suspend fun findWalletsByOwner(owner: WalletOwner): List<Wallet> = emptyList()

        override suspend fun findWalletByOwnerAndSymbol(owner: WalletOwner, symbol: String): List<Wallet> =
            emptyList()

        override suspend fun createWallet(
            owner: WalletOwner,
            balance: Amount,
            currency: Currency,
            type: WalletType
        ): Wallet = Wallet(3, owner, balance, currency, type, 0)

        override suspend fun createCashoutWallet(owner: WalletOwner, currency: Currency): Wallet =
            Wallet(4, owner, Amount(currency, BigDecimal.ZERO), currency, WalletType.CASHOUT, 0)

        override suspend fun findWalletById(walletId: Long): Wallet? =
            listOf(SOURCE_WALLET, DEST_WALLET).find { it.id == walletId }

        override suspend fun findAllWalletsBriefNotZero(ownerId: Long): List<BriefWallet> = emptyList()
    }

    private class AllowingWalletOwnerManager : WalletOwnerManager {
        override suspend fun isDepositAllowed(owner: WalletOwner, amount: Amount): Boolean = true

        override suspend fun isWithdrawAllowed(owner: WalletOwner, amount: Amount): Boolean = true

        override suspend fun findWalletOwner(uuid: String): WalletOwner? = null

        override suspend fun createWalletOwner(uuid: String, title: String, userLevel: String): WalletOwner =
            SOURCE_OWNER

        override suspend fun findAllWalletOwners(): List<WalletOwner> = listOf(SOURCE_OWNER, DEST_OWNER)
    }

    private class RecordingTransactionManager : TransactionManager {
        override suspend fun save(transaction: Transaction): Long = 1

        override suspend fun findDepositTransactions(
            uuid: String,
            coin: String?,
            startTime: LocalDateTime?,
            endTime: LocalDateTime?,
            limit: Int,
            offset: Int,
            ascendingByTime: Boolean?
        ): List<TransactionHistory> = emptyList()

        override suspend fun findWithdrawTransactions(
            uuid: String,
            coin: String?,
            startTime: LocalDateTime?,
            endTime: LocalDateTime?,
            limit: Int,
            offset: Int
        ): List<TransactionHistory> = emptyList()

        override suspend fun findTransactions(
            uuid: String,
            coin: String?,
            category: TransferCategory?,
            startTime: LocalDateTime?,
            endTime: LocalDateTime?,
            asc: Boolean,
            limit: Int,
            offset: Int
        ): List<TransactionWithDetailHistory> = emptyList()
    }

    private class NoopUserTransactionManager : UserTransactionManager {
        override suspend fun save(tx: UserTransaction) {}

        override suspend fun getTransactionHistoryForUser(
            userId: String,
            currency: String?,
            category: UserTransactionCategory?,
            startTime: LocalDateTime?,
            endTime: LocalDateTime?,
            asc: Boolean,
            limit: Int,
            offset: Int
        ): List<UserTransactionHistory> = emptyList()
    }

    private class NoopWalletListener : WalletListener {
        override suspend fun onDeposit(
            me: Wallet,
            sourceWallet: Wallet,
            amount: Amount,
            finalAmount: BigDecimal,
            transaction: String
        ) {}

        override suspend fun onWithdraw(
            me: Wallet,
            destWallet: Wallet,
            amount: Amount,
            transaction: String
        ) {}
    }

    private class NoopZkPolLiabilityExporter : ZkPolLiabilityExporter {
        override suspend fun export(event: ZkPolLiabilityEvent) {}
    }

    companion object {
        private val CURRENCY = Currency("USDT", "Tether", BigDecimal("0.00000001"))
        private val SOURCE_OWNER = WalletOwner(1, "source", "Source", "*", true, true, true)
        private val DEST_OWNER = WalletOwner(2, "dest", "Dest", "*", true, true, true)
        private val SOURCE_WALLET = Wallet(
            1,
            SOURCE_OWNER,
            Amount(CURRENCY, BigDecimal.TEN),
            CURRENCY,
            WalletType.MAIN,
            0
        )
        private val DEST_WALLET = Wallet(
            2,
            DEST_OWNER,
            Amount(CURRENCY, BigDecimal.ZERO),
            CURRENCY,
            WalletType.MAIN,
            0
        )
    }
}
