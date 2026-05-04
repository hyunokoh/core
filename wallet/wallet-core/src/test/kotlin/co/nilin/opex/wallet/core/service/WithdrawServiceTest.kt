package co.nilin.opex.wallet.core.service

import co.nilin.opex.common.OpexError
import co.nilin.opex.utility.error.data.OpexException
import co.nilin.opex.wallet.core.inout.*
import co.nilin.opex.wallet.core.model.*
import co.nilin.opex.wallet.core.model.otc.CurrencyImplementationResponse
import co.nilin.opex.wallet.core.model.otc.FetchCurrencyInfo
import co.nilin.opex.wallet.core.spi.*
import io.micrometer.core.instrument.simple.SimpleMeterRegistry
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.Test
import java.math.BigDecimal
import java.time.LocalDateTime

private class WithdrawServiceTest {

    @Test
    fun givenAmountAboveBalanceByFee_whenWithdrawRequested_thenRejectBeforeTransfer(): Unit = runBlocking {
        val transferManager = RecordingTransferManager()
        val withdrawPersister = RecordingWithdrawPersister()
        val service = service(
            transferManager = transferManager,
            withdrawPersister = withdrawPersister,
            mainBalance = BigDecimal("100"),
            withdrawFee = BigDecimal("1")
        )

        assertThatThrownBy {
            runBlocking {
                service.requestWithdraw(
                    WithdrawCommand(
                        uuid = "user-1",
                        currency = "USDT",
                        amount = BigDecimal("100.5"),
                        description = "withdraw",
                        destSymbol = "USDT",
                        destAddress = "addr-1",
                        destNetwork = "ETH",
                        destNote = null
                    )
                )
            }
        }.isOpexError(OpexError.WithdrawAmountExceedsWalletBalance)

        assertThat(transferManager.transferCallCount).isZero()
        assertThat(withdrawPersister.persistCallCount).isZero()
    }

    @Test
    fun givenFeeGreaterThanAmount_whenWithdrawRequested_thenRejectBeforeTransfer(): Unit = runBlocking {
        val transferManager = RecordingTransferManager()
        val withdrawPersister = RecordingWithdrawPersister()
        val service = service(
            transferManager = transferManager,
            withdrawPersister = withdrawPersister,
            mainBalance = BigDecimal("100"),
            withdrawFee = BigDecimal("2")
        )

        assertThatThrownBy {
            runBlocking {
                service.requestWithdraw(
                    WithdrawCommand(
                        uuid = "user-1",
                        currency = "USDT",
                        amount = BigDecimal("1.5"),
                        description = "withdraw",
                        destSymbol = "USDT",
                        destAddress = "addr-1",
                        destNetwork = "ETH",
                        destNote = null
                    )
                )
            }
        }.isOpexError(OpexError.InvalidAmount)

        assertThat(transferManager.transferCallCount).isZero()
        assertThat(withdrawPersister.persistCallCount).isZero()
    }

    @Test
    fun givenNetAmountBelowMinimum_whenWithdrawRequested_thenRejectBeforeTransfer(): Unit = runBlocking {
        val transferManager = RecordingTransferManager()
        val withdrawPersister = RecordingWithdrawPersister()
        val service = service(
            transferManager = transferManager,
            withdrawPersister = withdrawPersister,
            mainBalance = BigDecimal("100"),
            withdrawFee = BigDecimal("2"),
            minimumWithdraw = BigDecimal("1")
        )

        assertThatThrownBy {
            runBlocking {
                service.requestWithdraw(
                    WithdrawCommand(
                        uuid = "user-1",
                        currency = "USDT",
                        amount = BigDecimal("2.5"),
                        description = "withdraw",
                        destSymbol = "USDT",
                        destAddress = "addr-1",
                        destNetwork = "ETH",
                        destNote = null
                    )
                )
            }
        }.isOpexError(OpexError.WithdrawAmountLessThanMinimum)

        assertThat(transferManager.transferCallCount).isZero()
        assertThat(withdrawPersister.persistCallCount).isZero()
    }

    @Test
    fun givenZeroDestAmount_whenAcceptWithdrawRequested_thenRejectBeforeTransfer(): Unit = runBlocking {
        val transferManager = RecordingTransferManager()
        val withdrawPersister = RecordingWithdrawPersister(existingWithdraw = createdWithdraw())
        val service = service(transferManager = transferManager, withdrawPersister = withdrawPersister)

        assertThatThrownBy {
            runBlocking {
                service.acceptWithdraw(
                    WithdrawAcceptCommand(
                        withdrawId = 1,
                        destAmount = BigDecimal.ZERO,
                        destTransactionRef = "chain-tx-1",
                        destNote = null
                    )
                )
            }
        }.isOpexError(OpexError.InvalidAmount)

        assertThat(transferManager.transferCallCount).isZero()
        assertThat(withdrawPersister.persistCallCount).isZero()
    }

    @Test
    fun givenDestAmountAboveNetAmount_whenAcceptWithdrawRequested_thenRejectBeforeTransfer(): Unit = runBlocking {
        val transferManager = RecordingTransferManager()
        val withdrawPersister = RecordingWithdrawPersister(existingWithdraw = createdWithdraw())
        val service = service(transferManager = transferManager, withdrawPersister = withdrawPersister)

        assertThatThrownBy {
            runBlocking {
                service.acceptWithdraw(
                    WithdrawAcceptCommand(
                        withdrawId = 1,
                        destAmount = BigDecimal("9.91"),
                        destTransactionRef = "chain-tx-1",
                        destNote = null
                    )
                )
            }
        }.isOpexError(OpexError.InvalidAppliedFee)

        assertThat(transferManager.transferCallCount).isZero()
        assertThat(withdrawPersister.persistCallCount).isZero()
    }

    private fun createdWithdraw(): Withdraw = Withdraw(
        withdrawId = 1,
        ownerUuid = "user-1",
        currency = "USDT",
        wallet = 2,
        amount = BigDecimal("9.9"),
        requestTransaction = "request-tx-1",
        finalizedTransaction = null,
        appliedFee = BigDecimal("0.1"),
        destAmount = null,
        destSymbol = "USDT",
        destAddress = "addr-1",
        destNetwork = "ETH",
        destNote = null,
        destTransactionRef = null,
        statusReason = null,
        status = WithdrawStatus.CREATED
    )

    private fun service(
        transferManager: RecordingTransferManager = RecordingTransferManager(),
        withdrawPersister: RecordingWithdrawPersister = RecordingWithdrawPersister(),
        mainBalance: BigDecimal = BigDecimal("100"),
        withdrawFee: BigDecimal = BigDecimal("1"),
        minimumWithdraw: BigDecimal = BigDecimal.ONE
    ): WithdrawService {
        val currency = Currency("USDT", "Tether", BigDecimal("0.00000001"))
        val owner = WalletOwner(1, "user-1", "User", "*", true, true, true)
        val walletManager = RecordingWalletManager(
            owner = owner,
            currency = currency,
            mainBalance = mainBalance
        )
        return WithdrawService(
            withdrawPersister,
            walletManager,
            RecordingWalletOwnerManager(owner),
            RecordingCurrencyService(currency),
            transferManager,
            SimpleMeterRegistry(),
            RecordingBcGatewayProxy(withdrawFee, minimumWithdraw),
            AllowingZkAmlScreeningService(),
            RecordingZkAmlWithdrawCaseRecorder(),
            "system"
        )
    }

    private fun org.assertj.core.api.AbstractThrowableAssert<*, out Throwable>.isOpexError(error: OpexError) {
        isInstanceOf(OpexException::class.java)
            .extracting("error")
            .isEqualTo(error)
    }

    private class RecordingWithdrawPersister(
        private val existingWithdraw: Withdraw? = null
    ) : WithdrawPersister {
        var persistCallCount = 0

        override suspend fun persist(withdraw: Withdraw): Withdraw {
            persistCallCount += 1
            return withdraw.copy(withdrawId = withdraw.withdrawId ?: 1)
        }

        override suspend fun findById(withdrawId: Long): Withdraw? =
            existingWithdraw?.takeIf { it.withdrawId == withdrawId }

        override suspend fun findWithdrawResponseById(withdrawId: Long): WithdrawResponse? = null

        override suspend fun findByCriteria(
            ownerUuid: String?,
            currency: String?,
            destTxRef: String?,
            destAddress: String?,
            status: List<WithdrawStatus>
        ): List<WithdrawResponse> = emptyList()

        override suspend fun findByCriteria(
            ownerUuid: String?,
            currency: String?,
            destTxRef: String?,
            destAddress: String?,
            status: List<WithdrawStatus>,
            offset: Int,
            size: Int
        ): List<WithdrawResponse> = emptyList()

        override suspend fun countByCriteria(
            ownerUuid: String?,
            currency: String?,
            destTxRef: String?,
            destAddress: String?,
            status: List<WithdrawStatus>
        ): Long = 0

        override suspend fun findWithdrawHistory(
            uuid: String,
            currency: String?,
            startTime: LocalDateTime?,
            endTime: LocalDateTime?,
            limit: Int,
            offset: Int,
            ascendingByTime: Boolean?
        ): List<WithdrawResponse> = emptyList()
    }

    private class RecordingWalletManager(
        private val owner: WalletOwner,
        private val currency: Currency,
        mainBalance: BigDecimal
    ) : WalletManager {
        private val mainWallet = Wallet(1, owner, Amount(currency, mainBalance), currency, WalletType.MAIN, 0)
        private val cashoutWallet = Wallet(2, owner, Amount(currency, BigDecimal.ZERO), currency, WalletType.CASHOUT, 0)

        override suspend fun isDepositAllowed(wallet: Wallet, amount: BigDecimal): Boolean = true

        override suspend fun isWithdrawAllowed(wallet: Wallet, amount: BigDecimal): Boolean = true

        override suspend fun increaseBalance(wallet: Wallet, amount: BigDecimal) {}

        override suspend fun decreaseBalance(wallet: Wallet, amount: BigDecimal) {}

        override suspend fun findWalletByOwnerAndCurrencyAndType(
            owner: WalletOwner,
            walletType: WalletType,
            currency: Currency
        ): Wallet? = when (walletType) {
            WalletType.MAIN -> mainWallet
            WalletType.CASHOUT -> cashoutWallet
            else -> null
        }

        override suspend fun findWallet(ownerId: Long, currency: String, walletType: WalletType): BriefWallet? = null

        override suspend fun findWalletsByOwnerAndType(owner: WalletOwner, walletType: WalletType): List<Wallet> =
            emptyList()

        override suspend fun findWalletsByOwner(owner: WalletOwner): List<Wallet> = emptyList()

        override suspend fun findWalletByOwnerAndSymbol(owner: WalletOwner, symbol: String): List<Wallet> = emptyList()

        override suspend fun createWallet(owner: WalletOwner, balance: Amount, currency: Currency, type: WalletType): Wallet =
            Wallet(3, owner, balance, currency, type, 0)

        override suspend fun createCashoutWallet(owner: WalletOwner, currency: Currency): Wallet = cashoutWallet

        override suspend fun findWalletById(walletId: Long): Wallet? =
            listOf(mainWallet, cashoutWallet).find { it.id == walletId }

        override suspend fun findAllWalletsBriefNotZero(ownerId: Long): List<BriefWallet> = emptyList()
    }

    private class RecordingWalletOwnerManager(private val owner: WalletOwner) : WalletOwnerManager {
        private val systemOwner = WalletOwner(2, "system", "System", "*", true, true, true)

        override suspend fun isDepositAllowed(owner: WalletOwner, amount: Amount): Boolean = true

        override suspend fun isWithdrawAllowed(owner: WalletOwner, amount: Amount): Boolean = true

        override suspend fun findWalletOwner(uuid: String): WalletOwner? =
            listOf(owner, systemOwner).find { it.uuid == uuid }

        override suspend fun createWalletOwner(uuid: String, title: String, userLevel: String): WalletOwner = owner

        override suspend fun findAllWalletOwners(): List<WalletOwner> = listOf(owner)
    }

    private class RecordingCurrencyService(private val currency: Currency) : CurrencyService {
        override suspend fun getCurrency(symbol: String): Currency? = currency.takeIf { it.symbol == symbol }

        override suspend fun addCurrency(name: String, symbol: String, precision: BigDecimal) {}

        override suspend fun addCurrency(request: Currency): Currency? = request

        override suspend fun updateCurrency(request: Currency): Currency? = request

        override suspend fun editCurrency(name: String, symbol: String, precision: BigDecimal) {}

        override suspend fun deleteCurrency(name: String): Currencies = Currencies(emptyList())

        override suspend fun getCurrencies(): Currencies = Currencies(listOf(currency))
    }

    private class RecordingTransferManager : TransferManager {
        var transferCallCount = 0

        override suspend fun transfer(transferCommand: TransferCommand): TransferResultDetailed {
            transferCallCount += 1
            return TransferResultDetailed(
                TransferResult(
                    date = 1,
                    sourceUuid = transferCommand.sourceWallet.owner.uuid,
                    sourceWalletType = transferCommand.sourceWallet.type,
                    sourceBalanceBeforeAction = transferCommand.sourceWallet.balance,
                    sourceBalanceAfterAction = transferCommand.sourceWallet.balance,
                    amount = transferCommand.amount,
                    destUuid = transferCommand.destWallet.owner.uuid,
                    destWalletType = transferCommand.destWallet.type,
                    receivedAmount = transferCommand.destAmount
                ),
                tx = "tx-1"
            )
        }
    }

    private class RecordingBcGatewayProxy(
        private val withdrawFee: BigDecimal,
        private val minimumWithdraw: BigDecimal
    ) : BcGatewayProxy {
        override suspend fun createCurrency(currencyImp: PropagateCurrencyChanges): CurrencyImplementationResponse? = null

        override suspend fun updateCurrency(currencyImp: PropagateCurrencyChanges): CurrencyImplementationResponse? = null

        override suspend fun getCurrencyInfo(symbol: String): FetchCurrencyInfo? = null

        override suspend fun getWithdrawData(symbol: String, network: String): WithdrawData =
            WithdrawData(isEnabled = true, fee = withdrawFee, minimum = minimumWithdraw)
    }

    private class AllowingZkAmlScreeningService : ZkAmlScreeningService {
        override suspend fun screenWithdraw(request: WithdrawScreeningRequest): WithdrawScreeningResult =
            WithdrawScreeningResult(ZkScreeningDecision.ALLOW)
    }

    private class RecordingZkAmlWithdrawCaseRecorder : ZkAmlWithdrawCaseRecorder {
        override suspend fun record(record: ZkAmlWithdrawCaseRecord) {}
    }
}
