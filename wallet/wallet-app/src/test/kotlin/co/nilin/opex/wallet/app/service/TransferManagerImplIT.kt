package co.nilin.opex.wallet.app.service

import co.nilin.opex.common.OpexError
import co.nilin.opex.wallet.app.KafkaEnabledTest
import co.nilin.opex.wallet.core.exc.ConcurrentBalanceChangException
import co.nilin.opex.wallet.core.inout.TransferCommand
import co.nilin.opex.wallet.core.model.Amount
import co.nilin.opex.wallet.core.model.TransferCategory
import co.nilin.opex.wallet.core.model.WalletLimitAction
import co.nilin.opex.wallet.core.model.WalletType
import co.nilin.opex.wallet.core.spi.*
import co.nilin.opex.wallet.ports.postgres.dao.WalletLimitsRepository
import co.nilin.opex.wallet.ports.postgres.model.WalletLimitsModel
import co.nilin.opex.utility.error.data.OpexException
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.reactive.awaitFirst
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import java.math.BigDecimal
import java.time.LocalDateTime
import java.util.*


class TransferManagerImplIT : KafkaEnabledTest() {

    @Autowired
    lateinit var transferManager: TransferManager

    @Autowired
    lateinit var currencyService: CurrencyService

    @Autowired
    lateinit var walletManager: WalletManager

    @Autowired
    lateinit var walletOwnerManager: WalletOwnerManager

    @Autowired
    lateinit var transactionManager: TransactionManager

    @Autowired
    lateinit var walletLimitsRepository: WalletLimitsRepository

    lateinit var cc: String
    val amount = BigDecimal.valueOf(10)
    var sourceUuid: String? = null
    var destUuid: String? = null

    @BeforeEach
    fun setup() {
        cc = "CC${UUID.randomUUID().toString().take(8).uppercase()}"
        sourceUuid = UUID.randomUUID().toString()
        setupWallets(sourceUuid!!)
    }

    @Test
    fun givenDuplicateTransferRef_whenTransfer_thenRejectAsBadRequestAndRollbackBalances() {
        runBlocking {
            val currency = currencyService.getCurrency(cc)!!
            val owner = walletOwnerManager.findWalletOwner(sourceUuid!!)!!
            val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.MAIN, currency)!!
            val receiverWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)!!
            val transferRef = "duplicate-ref-${UUID.randomUUID()}"

            transferManager.transfer(
                TransferCommand(
                    sourceWallet,
                    receiverWallet,
                    Amount(sourceWallet.currency, BigDecimal.ONE),
                    "first transfer",
                    transferRef,
                    TransferCategory.NORMAL
                )
            )
            val sourceBalanceAfterFirstTransfer =
                walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.MAIN, currency)!!.balance.amount
            val receiverBalanceAfterFirstTransfer =
                walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)!!.balance.amount

            val exception = assertThrows(OpexException::class.java) {
                runBlocking {
                    val refreshedSourceWallet =
                        walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.MAIN, currency)!!
                    val refreshedReceiverWallet =
                        walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)!!
                    transferManager.transfer(
                        TransferCommand(
                            refreshedSourceWallet,
                            refreshedReceiverWallet,
                            Amount(refreshedSourceWallet.currency, BigDecimal.ONE),
                            "duplicate transfer",
                            transferRef,
                            TransferCategory.NORMAL
                        )
                    )
                }
            }

            assertEquals(OpexError.BadRequest, exception.error)
            val updatedSourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.MAIN, currency)!!
            val updatedReceiverWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)!!
            assertEquals(sourceBalanceAfterFirstTransfer, updatedSourceWallet.balance.amount)
            assertEquals(receiverBalanceAfterFirstTransfer, updatedReceiverWallet.balance.amount)
        }
    }

    @Test
    fun givenSameSenderWallet_whenConcurrentTransfers_thenSecondTransferFail() {

        val block: () -> Unit = {
            runBlocking {
                val currency = currencyService.getCurrency(cc)!!
                val owner = walletOwnerManager.findWalletOwner(sourceUuid!!)
                val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner!!, WalletType.MAIN, currency)
                val receiverWallet =
                    walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)

                launch {
                    transferManager.transfer(
                        TransferCommand(
                            sourceWallet!!,
                            receiverWallet!!,
                            Amount(sourceWallet.currency, amount),
                            "Amount1 ${System.currentTimeMillis()}", "Ref1 ${System.currentTimeMillis()}", TransferCategory.NORMAL
                        )
                    )
                }
                launch {
                    transferManager.transfer(
                        TransferCommand(
                            sourceWallet!!,
                            receiverWallet!!,
                            Amount(sourceWallet.currency, amount),
                            "Amount2 ${System.currentTimeMillis()}", "Ref2 ${System.currentTimeMillis()}", TransferCategory.NORMAL
                        )
                    )
                }
            }
        }
        try {
            block.invoke()
        } catch (_: ConcurrentBalanceChangException) {

        }
        runBlocking {
            val currency = currencyService.getCurrency(cc)!!
            val owner = walletOwnerManager.findWalletOwner(sourceUuid!!)
            val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner!!, WalletType.MAIN, currency)
            val receiverWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)

            assertEquals(amount, sourceWallet!!.balance.amount)
            assertEquals(amount, receiverWallet!!.balance.amount)
        }
    }

    @Test
    fun givenSameReceiverWallet_whenConcurrentTransfers_thenTransfersSuccess() {
        runBlocking {
            val currency = currencyService.getCurrency(cc)!!
            val owner = walletOwnerManager.findWalletOwner(sourceUuid!!)
            val receiverWallet =
                walletManager.findWalletByOwnerAndCurrencyAndType(owner!!, WalletType.EXCHANGE, currency)

            val source2Uuid = UUID.randomUUID().toString()
            setupWallets(source2Uuid)
            val sourceOwner2 = walletOwnerManager.findWalletOwner(source2Uuid)

            val t1 = async {
                val sourceWallet1 = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.MAIN, currency)
                transferManager.transfer(
                    TransferCommand(
                        sourceWallet1!!,
                        receiverWallet!!,
                        Amount(sourceWallet1.currency, amount),
                        "Amount1 ${System.currentTimeMillis()}", "Ref1 ${System.currentTimeMillis()}", TransferCategory.NORMAL
                    )
                )
            }
            val t2 = async {
                val sourceWallet2 =
                    walletManager.findWalletByOwnerAndCurrencyAndType(sourceOwner2!!, WalletType.MAIN, currency)
                transferManager.transfer(
                    TransferCommand(
                        sourceWallet2!!,
                        receiverWallet!!,
                        Amount(sourceWallet2.currency, amount),
                        "Amount2 ${System.currentTimeMillis()}", "Ref2 ${System.currentTimeMillis()}", TransferCategory.NORMAL
                    )
                )
            }
            t1.await()
            t2.await()

            val sourceWallet1Refresh =
                walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.MAIN, currency)
            val sourceWallet2Refresh =
                walletManager.findWalletByOwnerAndCurrencyAndType(sourceOwner2!!, WalletType.MAIN, currency)
            val receiverWalletRefresh =
                walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)

            assertEquals(amount, sourceWallet1Refresh!!.balance.amount)
            assertEquals(amount, sourceWallet2Refresh!!.balance.amount)
            assertEquals(amount.plus(amount), receiverWalletRefresh!!.balance.amount)
        }


    }

    @Test
    fun givenSameSenderWallet_whenSequentialTransfers_thenTransfersSuccess() {
        runBlocking {
            val currency = currencyService.getCurrency(cc)!!
            val owner = walletOwnerManager.findWalletOwner(sourceUuid!!)

            async {
                val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner!!, WalletType.MAIN, currency)
                val receiverWallet =
                    walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)

                transferManager.transfer(
                    TransferCommand(
                        sourceWallet!!,
                        receiverWallet!!,
                        Amount(sourceWallet.currency, amount),
                        "Amount1 ${System.currentTimeMillis()}", "Ref1 ${System.currentTimeMillis()}", TransferCategory.NORMAL
                    )
                )
            }.await()
            async {
                val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner!!, WalletType.MAIN, currency)
                val receiverWallet =
                    walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)

                transferManager.transfer(
                    TransferCommand(
                        sourceWallet!!,
                        receiverWallet!!,
                        Amount(sourceWallet.currency, amount),
                        "Amount2 ${System.currentTimeMillis()}", "Ref2 ${System.currentTimeMillis()}", TransferCategory.NORMAL,
                    )
                )
            }.await()
            val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner!!, WalletType.MAIN, currency)
            val receiverWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)

            assertEquals(BigDecimal.ZERO, sourceWallet!!.balance.amount)
            assertEquals(amount.plus(amount), receiverWallet!!.balance.amount)
        }
    }

    @Test
    fun givenDailyWithdrawLimitBelowTransferAmount_whenTransfer_thenTransferFailsAndBalancesAreUnchanged() {
        runBlocking {
            val currency = currencyService.getCurrency(cc)!!
            val owner = walletOwnerManager.findWalletOwner(sourceUuid!!)!!
            val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.MAIN, currency)!!
            val receiverWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)!!

            walletLimitsRepository.save(
                WalletLimitsModel(
                    id = null,
                    level = null,
                    owner = owner.id,
                    action = WalletLimitAction.WITHDRAW,
                    currency = currency.symbol,
                    walletType = WalletType.MAIN,
                    walletId = sourceWallet.id,
                    dailyTotal = amount.subtract(BigDecimal.ONE),
                    dailyCount = null,
                    monthlyTotal = null,
                    monthlyCount = null
                )
            ).awaitFirst()

            assertThrows(OpexException::class.java) {
                runBlocking {
                    transferManager.transfer(newTransferCommand(sourceWallet, receiverWallet, "withdraw-limit"))
                }
            }

            val refreshedSource = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.MAIN, currency)!!
            val refreshedReceiver = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)!!
            assertEquals(amount.multiply(BigDecimal.valueOf(2)), refreshedSource.balance.amount)
            assertEquals(BigDecimal.ZERO, refreshedReceiver.balance.amount)
        }
    }

    @Test
    fun givenDailyDepositLimitBelowTransferAmount_whenTransfer_thenTransferFailsAndBalancesAreUnchanged() {
        runBlocking {
            val currency = currencyService.getCurrency(cc)!!
            val owner = walletOwnerManager.findWalletOwner(sourceUuid!!)!!
            val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.MAIN, currency)!!
            val receiverWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)!!

            walletLimitsRepository.save(
                WalletLimitsModel(
                    id = null,
                    level = null,
                    owner = owner.id,
                    action = WalletLimitAction.DEPOSIT,
                    currency = currency.symbol,
                    walletType = WalletType.EXCHANGE,
                    walletId = receiverWallet.id,
                    dailyTotal = amount.subtract(BigDecimal.ONE),
                    dailyCount = null,
                    monthlyTotal = null,
                    monthlyCount = null
                )
            ).awaitFirst()

            assertThrows(OpexException::class.java) {
                runBlocking {
                    transferManager.transfer(newTransferCommand(sourceWallet, receiverWallet, "deposit-limit"))
                }
            }

            val refreshedSource = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.MAIN, currency)!!
            val refreshedReceiver = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)!!
            assertEquals(amount.multiply(BigDecimal.valueOf(2)), refreshedSource.balance.amount)
            assertEquals(BigDecimal.ZERO, refreshedReceiver.balance.amount)
        }
    }

    @Test
    fun dwhenTransferWithAdditionalData_thenDataIsPersistedAndRetrievable() {
        runBlocking {
            val currency = currencyService.getCurrency(cc)!!
            val owner = walletOwnerManager.findWalletOwner(sourceUuid!!)

            val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner!!, WalletType.MAIN, currency)
            val receiverWallet = walletManager.findWalletByOwnerAndCurrencyAndType(owner, WalletType.EXCHANGE, currency)

            val additionalData = mapOf(Pair("key1", "value"), Pair("key2", "value"))
            val result = transferManager.transfer(
                TransferCommand(
                    sourceWallet!!,
                    receiverWallet!!,
                    Amount(sourceWallet.currency, amount),
                    "Amount1 ${System.currentTimeMillis()}", "Ref1 ${System.currentTimeMillis()}",
                    TransferCategory.NORMAL
                )
            )

            val thw = transactionManager.findWithdrawTransactions(
                owner.uuid, currency.symbol, LocalDateTime.now().minusHours(1), LocalDateTime.now(), 100, 0
            )

            val thd = transactionManager.findDepositTransactions(
                owner.uuid, currency.symbol, LocalDateTime.now().minusHours(1), LocalDateTime.now(), 100, 0
            )
            val thwMatch = thw.find { th -> th.id.toString().equals(result.tx) }
            assertNotNull(thwMatch)

            val thdMatch = thd.find { th -> th.id.toString().equals(result.tx) }
            assertNotNull(thdMatch)

            val th = transactionManager.findTransactions(
                owner.uuid,
                currency.symbol,
                TransferCategory.NORMAL,
                LocalDateTime.now().minusHours(1),
                LocalDateTime.now(),
                true,
                100,
                0
            )

            val thMatch = th.find { i -> i.id.toString().equals(result.tx) }
        }
    }

    @Test
    fun whenTransfer_thenWithdrawFlagIsCorrect() {
        runBlocking {
            val currency = currencyService.getCurrency(cc)!!

            destUuid = UUID.randomUUID().toString()
            setupWallets(destUuid!!)

            val sender = walletOwnerManager.findWalletOwner(sourceUuid!!)
            val receiver = walletOwnerManager.findWalletOwner(destUuid!!)


            val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(sender!!, WalletType.MAIN, currency)
            val receiverWallet =
                walletManager.findWalletByOwnerAndCurrencyAndType(receiver!!, WalletType.EXCHANGE, currency)

            val result = transferManager.transfer(
                TransferCommand(
                    sourceWallet!!,
                    receiverWallet!!,
                    Amount(sourceWallet.currency, amount),
                    "Amount1 ${System.currentTimeMillis()}", "Ref1 ${System.currentTimeMillis()}",
                    TransferCategory.NORMAL
                )
            )

            val thw = transactionManager.findWithdrawTransactions(
                sender.uuid, currency.symbol, LocalDateTime.now().minusHours(1), LocalDateTime.now(), 100, 0
            )

            val thd = transactionManager.findDepositTransactions(
                receiver.uuid, currency.symbol, LocalDateTime.now().minusHours(1), LocalDateTime.now(), 100, 0
            )

            val thwMatch = thw.find { th -> th.id.toString() == result.tx }
            assertNotNull(thwMatch)
            assertEquals(TransferCategory.NORMAL, thwMatch!!.category)

            val thdMatch = thd.find { th -> th.id.toString() == result.tx }
            assertNotNull(thdMatch)
            assertEquals(TransferCategory.NORMAL, thdMatch!!.category)

            val thSender = transactionManager.findTransactions(
                sender.uuid,
                currency.symbol,
                TransferCategory.NORMAL,
                LocalDateTime.now().minusHours(1),
                LocalDateTime.now(),
                true,
                100,
                0
            )
            val thSenderMatch = thSender.find { i -> i.id.toString().equals(result.tx) }
            assertEquals(sender.uuid, thSenderMatch!!.senderUuid)
            assertEquals(TransferCategory.NORMAL, thSenderMatch.category)


            val thReceiver = transactionManager.findTransactions(
                receiver.uuid,
                currency.symbol,
                TransferCategory.NORMAL,
                LocalDateTime.now().minusHours(1),
                LocalDateTime.now(),
                true,
                100,
                0
            )
            val thReceiverMatch = thReceiver.find { i -> i.id.toString().equals(result.tx) }
            assertEquals(receiver.uuid, thReceiverMatch!!.receiverUuid)
            assertEquals(TransferCategory.NORMAL, thReceiverMatch.category)

        }
    }

    fun setupWallets(sourceUuid: String) {
        runBlocking {
            try {
                currencyService.deleteCurrency(cc)
            } catch (_: Throwable) {

            }
            currencyService.addCurrency(cc, cc, BigDecimal.ONE)
            val currency = currencyService.getCurrency(cc)

            val sourceOwner = walletOwnerManager.createWalletOwner(sourceUuid, "not set", "")
            walletManager.createWallet(
                sourceOwner,
                Amount(currency!!, amount.multiply(BigDecimal.valueOf(2))),
                currency,
                WalletType.MAIN
            )
            walletManager.createWallet(
                sourceOwner,
                Amount(currency, BigDecimal.ZERO),
                currency,
                WalletType.EXCHANGE
            )

        }
    }

    private fun newTransferCommand(
        sourceWallet: co.nilin.opex.wallet.core.model.Wallet,
        receiverWallet: co.nilin.opex.wallet.core.model.Wallet,
        label: String
    ): TransferCommand {
        return TransferCommand(
            sourceWallet,
            receiverWallet,
            Amount(sourceWallet.currency, amount),
            "$label ${System.currentTimeMillis()}",
            "$label-ref ${UUID.randomUUID()}",
            TransferCategory.NORMAL
        )
    }

}
