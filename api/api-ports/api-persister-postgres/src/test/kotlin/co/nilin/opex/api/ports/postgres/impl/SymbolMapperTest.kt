package co.nilin.opex.api.ports.postgres.impl

import co.nilin.opex.api.ports.postgres.ApiPostgresIntegrationTest
import co.nilin.opex.api.ports.postgres.dao.SymbolMapRepository
import co.nilin.opex.api.ports.postgres.impl.sample.VALID
import co.nilin.opex.api.ports.postgres.model.SymbolMapModel
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.r2dbc.core.DatabaseClient

private class SymbolMapperTest : ApiPostgresIntegrationTest() {
    @Autowired
    private lateinit var databaseClient: DatabaseClient

    @Autowired
    private lateinit var symbolMapRepository: SymbolMapRepository

    private val symbolMapper by lazy {
        SymbolMapperImpl(symbolMapRepository)
    }

    @BeforeEach
    fun seedDb(): Unit = runBlocking {
        databaseClient.executeSql("truncate table symbol_maps restart identity cascade")
        symbolMapRepository.save(SymbolMapModel(null, VALID.ETH_USDT, "binance", "ETHUSDT")).awaitSingle()
    }

    @Test
    fun givenSymbolAlias_whenMapSymbol_thenReturnAlias(): Unit = runBlocking {
        val alias = symbolMapper.fromInternalSymbol(VALID.ETH_USDT)

        assertThat(alias).isEqualTo("ETHUSDT")
    }

    @Test
    fun givenSymbolAlias_whenUnmapAlias_thenReturnSymbol(): Unit = runBlocking {
        val symbol = symbolMapper.toInternalSymbol("ETHUSDT")

        assertThat(symbol).isEqualTo(VALID.ETH_USDT)
    }

    @Test
    fun givenSymbolAlias_whenSymbolToAliasMap_thenReturnMap(): Unit = runBlocking {
        val map = symbolMapper.symbolToAliasMap()

        assertThat(map).hasSize(1)
        assertThat(map[VALID.ETH_USDT]).isEqualTo("ETHUSDT")
    }
}
