package co.nilin.opex.api.ports.postgres

import co.nilin.opex.api.ports.postgres.config.PostgresConfig
import kotlinx.coroutines.reactor.awaitSingleOrNull
import org.springframework.boot.autoconfigure.EnableAutoConfiguration
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.context.annotation.Configuration
import org.springframework.context.annotation.Import
import org.springframework.r2dbc.core.DatabaseClient

@SpringBootTest(
    classes = [ApiPostgresIntegrationTestConfig::class],
    properties = [
        "spring.r2dbc.url=r2dbc:tc:postgresql:///api?TC_IMAGE_TAG=9.6.8",
        "spring.r2dbc.username=api",
        "spring.r2dbc.password=api"
    ]
)
abstract class ApiPostgresIntegrationTest {
    protected suspend fun DatabaseClient.executeSql(sql: String) {
        sql(sql).then().awaitSingleOrNull()
    }
}

@Configuration
@EnableAutoConfiguration
@Import(PostgresConfig::class)
class ApiPostgresIntegrationTestConfig
