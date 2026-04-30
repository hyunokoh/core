package co.nilin.opex.market.ports.postgres.impl

import co.nilin.opex.market.core.inout.RateSource
import co.nilin.opex.market.ports.postgres.MarketPostgresIntegrationTest
import co.nilin.opex.market.ports.postgres.dao.CurrencyRateRepository
import co.nilin.opex.market.ports.postgres.dao.TradeRepository
import co.nilin.opex.market.ports.postgres.impl.sample.VALID
import co.nilin.opex.market.ports.postgres.util.RedisCacheHelper
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.r2dbc.core.DatabaseClient

private class TradePersisterTest : MarketPostgresIntegrationTest() {
    @Autowired
    private lateinit var databaseClient: DatabaseClient

    @Autowired
    private lateinit var tradeRepository: TradeRepository

    @Autowired
    private lateinit var currencyRateRepository: CurrencyRateRepository

    @Autowired
    private lateinit var redisCacheHelper: RedisCacheHelper

    private val tradePersister by lazy {
        TradePersisterImpl(tradeRepository, currencyRateRepository, redisCacheHelper)
    }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        databaseClient.executeSql("truncate table trades, currency_rate restart identity cascade")
    }

    @Test
    fun givenRichTrade_whenSave_thenPersistTradeAndUpdateMarketRate(): Unit = runBlocking {
        tradePersister.save(VALID.RICH_TRADE)

        val trade = tradeRepository.findByOuid(VALID.RICH_TRADE.makerOuid).first()
        val rate = currencyRateRepository.findByBaseAndQuoteAndSource("ETH", "USDT", RateSource.MARKET).awaitSingle()

        assertThat(trade.tradeId).isEqualTo(VALID.RICH_TRADE.id)
        assertThat(trade.matchedPrice).isEqualByComparingTo(VALID.RICH_TRADE.matchedPrice)
        assertThat(rate.rate).isEqualByComparingTo(VALID.RICH_TRADE.matchedPrice)
    }

    @Test
    fun givenDuplicateRichTrade_whenSaveAgain_thenPersistOnce(): Unit = runBlocking {
        tradePersister.save(VALID.RICH_TRADE)
        tradePersister.save(VALID.RICH_TRADE)

        val trades = tradeRepository.findByOuid(VALID.RICH_TRADE.makerOuid).toList()

        assertThat(trades).hasSize(1)
        assertThat(trades.single().tradeId).isEqualTo(VALID.RICH_TRADE.id)
    }
}
