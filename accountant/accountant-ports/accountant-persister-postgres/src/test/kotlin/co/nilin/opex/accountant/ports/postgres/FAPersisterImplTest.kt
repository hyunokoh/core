package co.nilin.opex.accountant.ports.postgres

import co.nilin.opex.accountant.core.model.FinancialAction
import co.nilin.opex.accountant.core.model.FinancialActionStatus
import co.nilin.opex.accountant.ports.postgres.dao.FinancialActionErrorRepository
import co.nilin.opex.accountant.ports.postgres.dao.FinancialActionRepository
import co.nilin.opex.accountant.ports.postgres.dao.FinancialActionRetryRepository
import co.nilin.opex.accountant.ports.postgres.impl.FinancialActionPersisterImpl
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.reactor.awaitSingleOrNull
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired

class FAPersisterImplTest : AccountantPostgresIntegrationTest() {

    @Autowired
    private lateinit var financialActionRepository: FinancialActionRepository

    @Autowired
    private lateinit var faRetryRepository: FinancialActionRetryRepository

    @Autowired
    private lateinit var faErrorRepository: FinancialActionErrorRepository

    private val faPersister by lazy {
        FinancialActionPersisterImpl(
            financialActionRepository,
            faRetryRepository,
            faErrorRepository
        )
    }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        faErrorRepository.deleteAll().awaitSingleOrNull()
        faRetryRepository.deleteAll().awaitSingleOrNull()
        financialActionRepository.deleteAll().awaitSingleOrNull()
    }

    @Test
    fun givenListOfActions_whenSaving_persistsRows(): Unit = runBlocking {
        faPersister.persist(listOf(Valid.fa))

        val persisted = financialActionRepository.findAll().collectList().awaitSingle()

        assertThat(persisted).hasSize(1)
        with(persisted[0]) {
            assertThat(uuid).isEqualTo(Valid.fa.uuid)
            assertThat(pointer).isEqualTo(Valid.fa.pointer)
            assertThat(symbol).isEqualTo(Valid.fa.symbol)
            assertThat(amount.stripTrailingZeros()).isEqualTo(Valid.fa.amount.stripTrailingZeros())
            assertThat(status).isEqualTo(FinancialActionStatus.CREATED)
        }
    }

    @Test
    fun givenFAAndStatus_whenUpdatingStatus_updatesPersistedRow(): Unit = runBlocking {
        val persisted = financialActionRepository.save(Valid.faModel.copy(id = null)).awaitSingle()
        val action = FinancialAction(
            Valid.fa.parent,
            Valid.fa.eventType,
            Valid.fa.pointer,
            Valid.fa.symbol,
            Valid.fa.amount,
            Valid.fa.sender,
            Valid.fa.senderWalletType,
            Valid.fa.receiver,
            Valid.fa.receiverWalletType,
            Valid.fa.createDate,
            Valid.fa.category,
            id = persisted.id,
            uuid = persisted.uuid
        )

        faPersister.updateStatus(action, FinancialActionStatus.PROCESSED)

        val updated = financialActionRepository.findById(persisted.id!!).awaitSingle()
        assertThat(updated.status).isEqualTo(FinancialActionStatus.PROCESSED)
    }

    @Test
    fun givenFAUuidAndWalletError_whenUpdatingWithError_persistsErrorDetails(): Unit = runBlocking {
        val persisted = financialActionRepository.save(Valid.faModel.copy(id = null)).awaitSingle()

        faPersister.updateWithError(persisted.uuid, "6018", "NotEnoughBalance", "wallet rejected transfer")

        val updated = financialActionRepository.findById(persisted.id!!).awaitSingle()
        val errors = faErrorRepository.findAll().collectList().awaitSingle()
        assertThat(updated.status).isEqualTo(FinancialActionStatus.ERROR)
        assertThat(errors).hasSize(1)
        with(errors.single()) {
            assertThat(faId).isEqualTo(persisted.id)
            assertThat(error).isEqualTo("6018")
            assertThat(message).isEqualTo("NotEnoughBalance")
            assertThat(body).isEqualTo("wallet rejected transfer")
            assertThat(retryId).isNull()
        }
    }
}
