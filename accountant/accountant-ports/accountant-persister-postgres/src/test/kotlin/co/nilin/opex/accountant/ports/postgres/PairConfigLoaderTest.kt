package co.nilin.opex.accountant.ports.postgres

import co.nilin.opex.accountant.ports.postgres.dao.PairConfigRepository
import co.nilin.opex.accountant.ports.postgres.dao.PairFeeConfigRepository
import co.nilin.opex.accountant.ports.postgres.impl.PairConfigLoaderImpl
import co.nilin.opex.accountant.ports.postgres.model.PairFeeConfigModel
import co.nilin.opex.matching.engine.core.model.OrderDirection
import co.nilin.opex.utility.error.data.OpexException
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.reactor.awaitSingleOrNull
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.r2dbc.core.DatabaseClient

class PairConfigLoaderTest : AccountantPostgresIntegrationTest() {

    @Autowired
    private lateinit var databaseClient: DatabaseClient

    @Autowired
    private lateinit var pairConfigRepository: PairConfigRepository

    @Autowired
    private lateinit var pairFeeConfigRepository: PairFeeConfigRepository

    private val pairConfigLoader by lazy { PairConfigLoaderImpl(pairConfigRepository, pairFeeConfigRepository) }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        pairFeeConfigRepository.deleteAll().awaitSingleOrNull()
        pairConfigRepository.deleteAll().awaitSingleOrNull()
        databaseClient.executeSql("delete from user_level")
    }

    @Test
    fun givenPairConfigs_whenListNotEmpty_resultIsNotEmptyAndValid(): Unit = runBlocking {
        seedPairConfig()
        seedPairConfig("ETH_USDT", "ETH", "USDT")

        val configs = pairConfigLoader.loadPairConfigs()

        assertThat(configs).hasSize(2)
        assertThat(configs.map { it.pair }).contains("BTC_USDT", "ETH_USDT")
    }

    @Test
    fun givenPairFeeConfigs_whenListNotEmpty_resultIsNotEmptyAndValid(): Unit = runBlocking {
        seedPairConfig()
        seedUserLevel("*")
        seedUserLevel("1")
        seedFeeConfig("*")
        seedFeeConfig("1")

        val configs = pairConfigLoader.loadPairFeeConfigs()

        assertThat(configs).hasSize(2)
        assertThat(configs.map { it.userLevel }).contains("*", "1")
        assertThat(configs.map { it.pairConfig.pair }).containsOnly("BTC_USDT")
    }

    @Test
    fun givenPairDirectionUserLevel_whenPairConfigNotFound_throwsException(): Unit = runBlocking {
        assertThatThrownBy {
            runBlocking { pairConfigLoader.load("BTC_USDT", OrderDirection.BID, "*") }
        }.isInstanceOf(OpexException::class.java)
    }

    @Test
    fun givenPairDirection_whenUserLevelEmpty_loadWithDefaultUserLevel(): Unit = runBlocking {
        seedPairConfig()
        seedUserLevel("*")
        seedFeeConfig("*")

        val pair = pairConfigLoader.load("BTC_USDT", OrderDirection.BID, "")

        assertThat(pair).isNotNull
        assertThat(pair.userLevel).isEqualTo("*")
    }

    @Test
    fun givenPairDirection_whenPairFeeConfigNotFound_throwsException(): Unit = runBlocking {
        seedPairConfig()
        seedUserLevel("*")

        assertThatThrownBy {
            runBlocking { pairConfigLoader.load("BTC_USDT", OrderDirection.BID, "") }
        }.isInstanceOf(OpexException::class.java)
    }

    @Test
    fun givenPairDirectionUserLevel_whenPairFeeConfigNotFound_loadWithDefaultUserLevel(): Unit = runBlocking {
        seedPairConfig()
        seedUserLevel("*")
        seedUserLevel("1")
        seedFeeConfig("*")

        val pair = pairConfigLoader.load("BTC_USDT", OrderDirection.BID, "1")

        assertThat(pair).isNotNull
        assertThat(pair.userLevel).isEqualTo("*")
    }

    @Test
    fun givenPairDirectionUserLevel_whenPairFeeConfigNotFoundWithActualAndDefaultUserLevel_throwsException(): Unit =
        runBlocking {
            seedPairConfig()
            seedUserLevel("*")
            seedUserLevel("1")

            assertThatThrownBy {
                runBlocking { pairConfigLoader.load("BTC_USDT", OrderDirection.BID, "1") }
            }.isInstanceOf(OpexException::class.java)
        }

    @Test
    fun givenPairDirection_whenPairConfigNotFound_throwException(): Unit = runBlocking {
        assertThatThrownBy {
            runBlocking { pairConfigLoader.load("BTC_USDT", OrderDirection.BID) }
        }.isInstanceOf(OpexException::class.java)
    }

    @Test
    fun givenPairDirection_whenConfigLoaded_returnValidPairConfig(): Unit = runBlocking {
        seedPairConfig()

        with(pairConfigLoader.load("BTC_USDT", OrderDirection.BID)) {
            assertThat(pair).isEqualTo(Valid.pairConfigModel.pair)
            assertThat(leftSideWalletSymbol).isEqualTo(Valid.pairConfigModel.leftSideWalletSymbol)
            assertThat(rightSideWalletSymbol).isEqualTo(Valid.pairConfigModel.rightSideWalletSymbol)
            assertThat(rightSideFraction.stripTrailingZeros())
                .isEqualTo(Valid.pairConfigModel.rightSideFraction.stripTrailingZeros())
            assertThat(leftSideFraction.stripTrailingZeros())
                .isEqualTo(Valid.pairConfigModel.leftSideFraction.stripTrailingZeros())
        }
    }

    private suspend fun seedPairConfig(
        pair: String = "BTC_USDT",
        leftSide: String = "BTC",
        rightSide: String = "USDT"
    ) {
        pairConfigRepository.insert(
            pair,
            leftSide,
            rightSide,
            Valid.pairConfigModel.leftSideFraction,
            Valid.pairConfigModel.rightSideFraction
        ).awaitSingleOrNull()
    }

    private suspend fun seedUserLevel(level: String) {
        databaseClient.executeSql("insert into user_level(level) values ('$level') on conflict do nothing")
    }

    private suspend fun seedFeeConfig(userLevel: String) {
        pairFeeConfigRepository.save(
            PairFeeConfigModel(
                null,
                "BTC_USDT",
                OrderDirection.BID.toString(),
                userLevel,
                Valid.pairFeeConfigModel.makerFee,
                Valid.pairFeeConfigModel.takerFee
            )
        ).awaitSingle()
    }
}
