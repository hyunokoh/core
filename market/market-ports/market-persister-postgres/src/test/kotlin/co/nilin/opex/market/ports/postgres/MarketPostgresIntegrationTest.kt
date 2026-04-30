package co.nilin.opex.market.ports.postgres

import co.nilin.opex.market.ports.postgres.config.PostgresConfig
import co.nilin.opex.market.ports.postgres.util.RedisCacheHelper
import kotlinx.coroutines.reactor.awaitSingleOrNull
import org.springframework.boot.autoconfigure.EnableAutoConfiguration
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.context.annotation.Import
import org.springframework.data.redis.core.RedisTemplate
import org.springframework.r2dbc.core.DatabaseClient

@SpringBootTest(
    classes = [MarketPostgresIntegrationTestConfig::class],
    properties = [
        "spring.r2dbc.url=r2dbc:tc:postgresql:///market?TC_IMAGE_TAG=9.6.8",
        "spring.r2dbc.username=market",
        "spring.r2dbc.password=market"
    ]
)
abstract class MarketPostgresIntegrationTest {
    protected suspend fun DatabaseClient.executeSql(sql: String) {
        sql(sql).then().awaitSingleOrNull()
    }
}

@Configuration
@EnableAutoConfiguration
@Import(PostgresConfig::class)
class MarketPostgresIntegrationTestConfig {
    @Bean
    fun redisCacheHelper(): RedisCacheHelper = RedisCacheHelper(RedisTemplate())
}
