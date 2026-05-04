package co.nilin.opex.wallet.ports.postgres.impl

import co.nilin.opex.common.OpexError
import co.nilin.opex.utility.error.data.OpexException
import co.nilin.opex.wallet.core.model.Withdraw
import co.nilin.opex.wallet.core.model.WithdrawStatus
import co.nilin.opex.wallet.core.model.WalletType
import co.nilin.opex.wallet.ports.postgres.WalletPostgresIntegrationTest
import co.nilin.opex.wallet.ports.postgres.dao.CurrencyRepository
import co.nilin.opex.wallet.ports.postgres.dao.WalletOwnerRepository
import co.nilin.opex.wallet.ports.postgres.dao.WalletRepository
import co.nilin.opex.wallet.ports.postgres.dao.WithdrawRepository
import co.nilin.opex.wallet.ports.postgres.dto.toModel
import co.nilin.opex.wallet.ports.postgres.impl.sample.VALID
import co.nilin.opex.wallet.ports.postgres.model.WalletModel
import kotlinx.coroutines.reactive.awaitSingle
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.r2dbc.core.DatabaseClient
import java.math.BigDecimal
import java.util.UUID

private class WithdrawPersisterImplTest : WalletPostgresIntegrationTest() {
    @Autowired
    private lateinit var databaseClient: DatabaseClient

    @Autowired
    private lateinit var withdrawRepository: WithdrawRepository

    @Autowired
    private lateinit var walletRepository: WalletRepository

    @Autowired
    private lateinit var walletOwnerRepository: WalletOwnerRepository

    @Autowired
    private lateinit var currencyRepository: CurrencyRepository

    private val withdrawPersister by lazy {
        WithdrawPersisterImpl(withdrawRepository)
    }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        databaseClient.executeSql("truncate table withdraws, wallet, wallet_owner, currency restart identity cascade")
    }

    @Test
    fun givenDuplicateDestinationTransactionRef_whenPersistWithdraw_thenThrowDomainError(): Unit = runBlocking {
        val walletId = seedWallet()
        val duplicateRef = "chain-${UUID.randomUUID()}"

        withdrawPersister.persist(withdraw(walletId, "req-1", "final-1", duplicateRef))

        assertThatThrownBy {
            runBlocking {
                withdrawPersister.persist(withdraw(walletId, "req-2", "final-2", duplicateRef))
            }
        }
            .isInstanceOf(OpexException::class.java)
            .extracting("error")
            .isEqualTo(OpexError.DuplicateWithdrawTransactionRef)

        val persisted = withdrawPersister.findByCriteria(null, null, duplicateRef, null, emptyList())
        assertThat(persisted).hasSize(1)
    }

    private suspend fun seedWallet(): Long {
        databaseClient.executeSql("insert into currency(symbol, name, precision) values ('${VALID.CURRENCY.symbol}', '${VALID.CURRENCY.name}', ${VALID.CURRENCY.precision})")
        val owner = walletOwnerRepository.save(VALID.WALLET_OWNER.copy(id = null, uuid = UUID.randomUUID().toString()).toModel()).awaitSingle()
        return walletRepository.save(WalletModel(owner.id!!, WalletType.CASHOUT, VALID.CURRENCY.symbol, BigDecimal.ZERO)).awaitSingle().id!!
    }

    private fun withdraw(walletId: Long, requestTx: String, finalizedTx: String, destinationRef: String) =
        Withdraw(
            withdrawId = null,
            ownerUuid = "owner",
            currency = VALID.CURRENCY.symbol,
            wallet = walletId,
            amount = BigDecimal.ONE,
            requestTransaction = requestTx,
            finalizedTransaction = finalizedTx,
            appliedFee = BigDecimal.ZERO,
            destAmount = BigDecimal.ONE,
            destSymbol = VALID.CURRENCY.symbol,
            destAddress = "address",
            destNetwork = "network",
            destNote = null,
            destTransactionRef = destinationRef,
            statusReason = null,
            status = WithdrawStatus.DONE
        )
}
