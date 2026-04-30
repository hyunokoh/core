package co.nilin.opex.wallet.ports.postgres.impl

import co.nilin.opex.utility.error.data.OpexException
import co.nilin.opex.wallet.ports.postgres.WalletPostgresIntegrationTest
import co.nilin.opex.wallet.ports.postgres.dao.CurrencyRepository
import co.nilin.opex.wallet.ports.postgres.impl.sample.VALID
import kotlinx.coroutines.reactor.awaitSingleOrNull
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.r2dbc.core.DatabaseClient

private class CurrencyServiceTest : WalletPostgresIntegrationTest() {
    @Autowired
    private lateinit var currencyRepository: CurrencyRepository

    @Autowired
    private lateinit var databaseClient: DatabaseClient

    private val currencyService by lazy { CurrencyServiceImpl(currencyRepository) }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        databaseClient.executeSql("truncate table transaction, wallet_limits, wallet_config, wallet, wallet_owner, currency restart identity cascade")
    }

    @Test
    fun givenCurrency_whenGetCurrency_thenReturnCurrency(): Unit = runBlocking {
        databaseClient.executeSql("insert into currency(symbol, name, precision) values ('${VALID.CURRENCY.symbol}', '${VALID.CURRENCY.name}', ${VALID.CURRENCY.precision})")

        val currency = currencyService.getCurrency(VALID.CURRENCY.symbol)

        assertThat(currency).isNotNull
        assertThat(currency!!.symbol).isEqualTo(VALID.CURRENCY.symbol)
        assertThat(currency.name).isEqualTo(VALID.CURRENCY.name)
        assertThat(currency.precision).isEqualTo(VALID.CURRENCY.precision)
    }

    @Test
    fun givenNoCurrency_whenGetCurrency_thenThrowException(): Unit = runBlocking {
        assertThrows(OpexException::class.java) {
            runBlocking { currencyService.getCurrency(VALID.CURRENCY.symbol) }
        }
    }

    @Test
    fun givenNoCurrency_whenGetCurrencyWithEmptySymbol_thenThrowException(): Unit = runBlocking {
        assertThrows(OpexException::class.java) {
            runBlocking { currencyService.getCurrency("") }
        }
    }
}
