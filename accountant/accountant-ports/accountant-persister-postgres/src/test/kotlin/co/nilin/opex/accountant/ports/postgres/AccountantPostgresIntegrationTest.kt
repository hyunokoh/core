package co.nilin.opex.accountant.ports.postgres

import co.nilin.opex.accountant.ports.postgres.config.PostgresConfig
import com.fasterxml.jackson.databind.DeserializationFeature
import com.fasterxml.jackson.databind.ObjectMapper
import kotlinx.coroutines.reactor.awaitSingleOrNull
import org.springframework.boot.autoconfigure.EnableAutoConfiguration
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.context.annotation.Import
import org.springframework.r2dbc.core.DatabaseClient

@SpringBootTest(
    classes = [AccountantPostgresIntegrationTestConfig::class],
    properties = [
        "spring.r2dbc.url=r2dbc:tc:postgresql:///accountant?TC_IMAGE_TAG=9.6.8",
        "spring.r2dbc.username=accountant",
        "spring.r2dbc.password=accountant",
        "app.fi-action.retry.count=5",
        "app.fi-action.retry.delay-seconds=4",
        "app.fi-action.retry.delay-multiplier=3"
    ]
)
abstract class AccountantPostgresIntegrationTest {

    protected suspend fun DatabaseClient.executeSql(sql: String) {
        sql(sql).then().awaitSingleOrNull()
    }
}

@Configuration
@EnableAutoConfiguration
@Import(PostgresConfig::class)
class AccountantPostgresIntegrationTestConfig {

    @Bean
    fun objectMapper(): ObjectMapper {
        return ObjectMapper().apply {
            findAndRegisterModules()
            configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
        }
    }
}
