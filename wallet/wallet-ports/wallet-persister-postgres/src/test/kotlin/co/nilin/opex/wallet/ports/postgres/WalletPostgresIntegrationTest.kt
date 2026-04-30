package co.nilin.opex.wallet.ports.postgres

import co.nilin.opex.wallet.ports.postgres.config.PostgresConfig
import kotlinx.coroutines.reactor.awaitSingleOrNull
import org.springframework.boot.autoconfigure.EnableAutoConfiguration
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.context.annotation.Configuration
import org.springframework.context.annotation.Import
import org.springframework.r2dbc.core.DatabaseClient

@SpringBootTest(
    classes = [WalletPostgresIntegrationTestConfig::class],
    properties = [
        "spring.r2dbc.url=r2dbc:tc:postgresql:///wallet?TC_IMAGE_TAG=9.6.8",
        "spring.r2dbc.username=wallet",
        "spring.r2dbc.password=wallet"
    ]
)
abstract class WalletPostgresIntegrationTest {
    protected suspend fun DatabaseClient.executeSql(sql: String) {
        sql(sql).then().awaitSingleOrNull()
    }
}

@Configuration
@EnableAutoConfiguration
@Import(PostgresConfig::class)
class WalletPostgresIntegrationTestConfig
