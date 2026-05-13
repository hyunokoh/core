package co.nilin.opex.wallet.ports.postgres.impl

import co.nilin.opex.wallet.core.model.Amount
import co.nilin.opex.wallet.core.model.WalletLimitAction
import co.nilin.opex.wallet.core.model.WalletType
import co.nilin.opex.wallet.ports.postgres.WalletPostgresIntegrationTest
import co.nilin.opex.wallet.ports.postgres.dao.TransactionRepository
import co.nilin.opex.wallet.ports.postgres.dao.WalletConfigRepository
import co.nilin.opex.wallet.ports.postgres.dao.WalletLimitsRepository
import co.nilin.opex.wallet.ports.postgres.dao.WalletOwnerRepository
import co.nilin.opex.wallet.ports.postgres.dto.toModel
import co.nilin.opex.wallet.ports.postgres.dto.toPlainObject
import co.nilin.opex.wallet.ports.postgres.impl.sample.VALID
import co.nilin.opex.wallet.ports.postgres.model.WalletLimitsModel
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

private class WalletOwnerManagerTest : WalletPostgresIntegrationTest() {
    @Autowired
    private lateinit var databaseClient: DatabaseClient

    @Autowired
    private lateinit var walletLimitsRepository: WalletLimitsRepository

    @Autowired
    private lateinit var transactionRepository: TransactionRepository

    @Autowired
    private lateinit var walletConfigRepository: WalletConfigRepository

    @Autowired
    private lateinit var walletOwnerRepository: WalletOwnerRepository

    private val walletOwnerManager by lazy {
        WalletOwnerManagerImpl(walletLimitsRepository, transactionRepository, walletConfigRepository, walletOwnerRepository)
    }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        databaseClient.executeSql("truncate table transaction, wallet_limits, wallet_config, wallet, wallet_owner, currency restart identity cascade")
        databaseClient.executeSql("insert into currency(symbol, name, precision) values ('${VALID.CURRENCY.symbol}', '${VALID.CURRENCY.name}', ${VALID.CURRENCY.precision})")
    }

    @Test
    fun givenOwnerWithNoLimit_whenIsWithdrawAllowed_thenReturnTrue(): Unit = runBlocking {
        val owner = seedOwner()
        seedWalletConfig()

        val isAllowed = walletOwnerManager.isWithdrawAllowed(owner, Amount(VALID.CURRENCY, BigDecimal.valueOf(0.5)))

        assertThat(isAllowed).isTrue()
    }

    @Test
    fun givenNoLimit_whenIsWithdrawAllowedByNegativeAmount_thenThrow(): Unit = runBlocking {
        val owner = seedOwner()

        assertThatThrownBy {
            runBlocking { walletOwnerManager.isWithdrawAllowed(owner, Amount(VALID.CURRENCY, BigDecimal.valueOf(-5))) }
        }.isInstanceOf(IllegalArgumentException::class.java)
    }

    @Test
    fun givenOwnerWithLimit_whenIsWithdrawAllowedWithCrossedAmount_thenReturnFalse(): Unit = runBlocking {
        val owner = seedOwner()
        seedWalletConfig()
        seedUserLimit(ownerId = owner.id, level = owner.level, action = WalletLimitAction.WITHDRAW, dailyTotal = BigDecimal.TEN)

        val isAllowed = walletOwnerManager.isWithdrawAllowed(owner, Amount(VALID.CURRENCY, BigDecimal.valueOf(120)))

        assertThat(isAllowed).isFalse()
    }

    @Test
    fun givenLevelWithLimit_whenIsDepositAllowedCrossedAmount_thenReturnFalse(): Unit = runBlocking {
        val owner = seedOwner()
        seedWalletConfig()
        seedUserLimit(ownerId = null, level = owner.level, action = WalletLimitAction.DEPOSIT, dailyTotal = BigDecimal.TEN)

        val isAllowed = walletOwnerManager.isDepositAllowed(owner, Amount(VALID.CURRENCY, BigDecimal.valueOf(120)))

        assertThat(isAllowed).isFalse()
    }

    @Test
    fun givenWalletOwner_whenFindWalletOwner_thenReturnWalletOwner(): Unit = runBlocking {
        val owner = seedOwner()

        val found = walletOwnerManager.findWalletOwner(owner.uuid)

        assertThat(found!!.id).isEqualTo(owner.id)
        assertThat(found.uuid).isEqualTo(owner.uuid)
    }

    @Test
    fun givenWalletOwner_whenCreateWalletOwner_thenReturnWalletOwner(): Unit = runBlocking {
        val owner = walletOwnerManager.createWalletOwner(
            VALID.WALLET_OWNER.uuid,
            VALID.WALLET_OWNER.title,
            VALID.WALLET_OWNER.level
        )

        assertThat(owner.id).isNotNull
        assertThat(owner.uuid).isEqualTo(VALID.WALLET_OWNER.uuid)
    }

    @Test
    fun givenExistingWalletOwner_whenCreateWalletOwner_thenReturnExistingWalletOwner(): Unit = runBlocking {
        val existing = seedOwner()

        val owner = walletOwnerManager.createWalletOwner(
            existing.uuid,
            "ignored",
            "2"
        )

        assertThat(owner.id).isEqualTo(existing.id)
        assertThat(owner.uuid).isEqualTo(existing.uuid)
        assertThat(owner.title).isEqualTo(existing.title)
        assertThat(owner.level).isEqualTo(existing.level)
    }

    private suspend fun seedOwner() =
        walletOwnerRepository.save(VALID.WALLET_OWNER.copy(id = null).toModel()).awaitSingle().toPlainObject()

    private suspend fun seedWalletConfig() {
        databaseClient.executeSql("insert into wallet_config(name, main_currency) values ('default', '${VALID.CURRENCY.symbol}')")
    }

    private suspend fun seedUserLimit(ownerId: Long?, level: String, action: WalletLimitAction, dailyTotal: BigDecimal) {
        walletLimitsRepository.save(
            WalletLimitsModel(
                id = null,
                level = level,
                owner = ownerId,
                action = action,
                currency = VALID.CURRENCY.symbol,
                walletType = WalletType.MAIN,
                walletId = null,
                dailyTotal = dailyTotal,
                dailyCount = null,
                monthlyTotal = null,
                monthlyCount = null
            )
        ).awaitSingleOrNull()
    }
}
