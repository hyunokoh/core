package co.nilin.opex.wallet.ports.postgres.impl

import co.nilin.opex.utility.error.data.OpexException
import co.nilin.opex.wallet.core.model.Wallet
import co.nilin.opex.wallet.core.model.WalletLimitAction
import co.nilin.opex.wallet.core.model.WalletOwner
import co.nilin.opex.wallet.core.model.WalletType
import co.nilin.opex.wallet.ports.postgres.WalletPostgresIntegrationTest
import co.nilin.opex.wallet.ports.postgres.dao.CurrencyRepository
import co.nilin.opex.wallet.ports.postgres.dao.TransactionRepository
import co.nilin.opex.wallet.ports.postgres.dao.WalletLimitsRepository
import co.nilin.opex.wallet.ports.postgres.dao.WalletOwnerRepository
import co.nilin.opex.wallet.ports.postgres.dao.WalletRepository
import co.nilin.opex.wallet.ports.postgres.dto.toModel
import co.nilin.opex.wallet.ports.postgres.dto.toPlainObject
import co.nilin.opex.wallet.ports.postgres.impl.sample.VALID
import co.nilin.opex.wallet.ports.postgres.model.TransactionModel
import co.nilin.opex.wallet.ports.postgres.model.WalletLimitsModel
import co.nilin.opex.wallet.ports.postgres.model.WalletModel
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.reactor.awaitSingleOrNull
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.r2dbc.core.DatabaseClient
import java.math.BigDecimal

private class WalletManagerTest : WalletPostgresIntegrationTest() {
    @Autowired
    private lateinit var databaseClient: DatabaseClient

    @Autowired
    private lateinit var walletLimitsRepository: WalletLimitsRepository

    @Autowired
    private lateinit var transactionRepository: TransactionRepository

    @Autowired
    private lateinit var walletRepository: WalletRepository

    @Autowired
    private lateinit var walletOwnerRepository: WalletOwnerRepository

    @Autowired
    private lateinit var currencyRepository: CurrencyRepository

    private val walletManager by lazy {
        WalletManagerImpl(walletLimitsRepository, transactionRepository, walletRepository, walletOwnerRepository, currencyRepository)
    }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        databaseClient.executeSql("truncate table transaction, wallet_limits, wallet_config, wallet, wallet_owner, currency restart identity cascade")
    }

    @Test
    fun givenWalletWithNoLimit_whenIsWithdrawAllowed_thenReturnTrue(): Unit = runBlocking {
        val wallet = seedWallet(balance = BigDecimal.valueOf(1.5))

        val isAllowed = walletManager.isWithdrawAllowed(wallet, BigDecimal.valueOf(0.5))

        assertThat(isAllowed).isTrue()
    }

    @Test
    fun givenEmptyWallet_whenIsWithdrawAllowed_thenReturnFalse(): Unit = runBlocking {
        val wallet = seedWallet(balance = BigDecimal.valueOf(1.5))

        val isAllowed = walletManager.isWithdrawAllowed(wallet, BigDecimal.valueOf(5))

        assertThat(isAllowed).isFalse()
    }

    @Test
    fun givenWrongAmount_whenIsWithdrawAllowed_thenThrow(): Unit = runBlocking {
        val wallet = seedWallet(balance = BigDecimal.valueOf(1.5))

        assertThatThrownBy {
            runBlocking { walletManager.isWithdrawAllowed(wallet, BigDecimal.valueOf(-1)) }
        }.isInstanceOf(OpexException::class.java)
    }

    @Test
    fun givenOwnerAndWalletLimit_whenWithdrawStatisticsCrossLimit_thenReturnFalse(): Unit = runBlocking {
        val wallet = seedWallet(balance = BigDecimal.valueOf(10))
        seedWalletLimit(wallet, WalletLimitAction.WITHDRAW, dailyTotal = BigDecimal.ONE)
        seedWithdrawTransaction(wallet, BigDecimal.valueOf(2))

        val isAllowed = walletManager.isWithdrawAllowed(wallet, BigDecimal.ONE)

        assertThat(isAllowed).isFalse()
    }

    @Test
    fun givenOwnerAndWalletTypeLimit_whenDepositStatisticsCrossLimit_thenReturnFalse(): Unit = runBlocking {
        val wallet = seedWallet(balance = BigDecimal.valueOf(10))
        seedWalletLimit(wallet, WalletLimitAction.DEPOSIT, walletId = null, dailyTotal = BigDecimal.ONE)
        seedDepositTransaction(wallet, BigDecimal.valueOf(2))

        val isAllowed = walletManager.isDepositAllowed(wallet, BigDecimal.ONE)

        assertThat(isAllowed).isFalse()
    }

    private suspend fun seedWallet(balance: BigDecimal): Wallet {
        databaseClient.executeSql("insert into currency(symbol, name, precision) values ('${VALID.CURRENCY.symbol}', '${VALID.CURRENCY.name}', ${VALID.CURRENCY.precision})")
        val currency = currencyRepository.findBySymbol(VALID.CURRENCY.symbol)!!.awaitSingle().toPlainObject()
        val owner = walletOwnerRepository.save(VALID.WALLET_OWNER.copy(id = null).toModel()).awaitSingle().toPlainObject()
        val wallet = walletRepository.save(WalletModel(owner.id!!, WalletType.MAIN, currency.symbol, balance)).awaitSingle()
        return Wallet(wallet.id!!, owner, co.nilin.opex.wallet.core.model.Amount(currency, wallet.balance), currency, wallet.type, wallet.version)
    }

    private suspend fun seedWalletLimit(
        wallet: Wallet,
        action: WalletLimitAction,
        walletId: Long? = wallet.id,
        dailyTotal: BigDecimal
    ) {
        walletLimitsRepository.save(
            WalletLimitsModel(
                id = null,
                level = null,
                owner = wallet.owner.id,
                action = action,
                currency = wallet.currency.symbol,
                walletType = wallet.type,
                walletId = walletId,
                dailyTotal = dailyTotal,
                dailyCount = null,
                monthlyTotal = null,
                monthlyCount = null
            )
        ).awaitSingleOrNull()
    }

    private suspend fun seedWithdrawTransaction(wallet: Wallet, amount: BigDecimal) {
        transactionRepository.save(transaction(wallet.owner, wallet.id!!, wallet.id!!, amount, BigDecimal.ZERO)).awaitSingle()
    }

    private suspend fun seedDepositTransaction(wallet: Wallet, amount: BigDecimal) {
        transactionRepository.save(transaction(wallet.owner, wallet.id!!, wallet.id!!, BigDecimal.ZERO, amount)).awaitSingle()
    }

    private fun transaction(owner: WalletOwner, sourceWallet: Long, destWallet: Long, sourceAmount: BigDecimal, destAmount: BigDecimal) =
        TransactionModel(
            id = null,
            sourceWallet = sourceWallet,
            destWallet = destWallet,
            sourceAmount = sourceAmount,
            destAmount = destAmount,
            description = "limit test for ${owner.uuid}",
            transferRef = "limit-${owner.uuid}-${System.nanoTime()}"
        )
}
