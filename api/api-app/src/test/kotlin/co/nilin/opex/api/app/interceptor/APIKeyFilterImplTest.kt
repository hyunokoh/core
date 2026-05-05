package co.nilin.opex.api.app.interceptor

import co.nilin.opex.api.core.inout.APIKey
import co.nilin.opex.api.core.spi.APIKeyService
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import org.springframework.mock.http.server.reactive.MockServerHttpRequest
import org.springframework.mock.web.server.MockServerWebExchange
import org.springframework.web.server.ServerWebExchange
import org.springframework.web.server.WebFilterChain
import reactor.core.publisher.Mono
import java.net.InetSocketAddress
import java.time.LocalDateTime

class APIKeyFilterImplTest {

    @Test
    fun givenAllowedIpMatchesRemoteAddress_whenApiKeyIsValid_thenInjectBearerToken() {
        val apiKeyService = RecordingAPIKeyService(apiKey(allowedIPs = "10.0.0.7"))
        val chain = RecordingWebFilterChain()
        val exchange = exchange(remoteIp = "10.0.0.7")

        APIKeyFilterImpl(apiKeyService).filter(exchange, chain).block()

        assertThat(chain.authorizationHeader).isEqualTo("Bearer access-token")
        assertThat(apiKeyService.calls).isEqualTo(1)
    }

    @Test
    fun givenAllowedIpDoesNotMatchRemoteAddress_whenApiKeyIsValid_thenDoNotInjectBearerToken() {
        val apiKeyService = RecordingAPIKeyService(apiKey(allowedIPs = "10.0.0.8"))
        val chain = RecordingWebFilterChain()
        val exchange = exchange(remoteIp = "10.0.0.7")

        APIKeyFilterImpl(apiKeyService).filter(exchange, chain).block()

        assertThat(chain.authorizationHeader).isNull()
        assertThat(apiKeyService.calls).isEqualTo(1)
    }

    @Test
    fun givenNoAllowedIpRestriction_whenApiKeyIsValid_thenInjectBearerToken() {
        val apiKeyService = RecordingAPIKeyService(apiKey(allowedIPs = null))
        val chain = RecordingWebFilterChain()
        val exchange = exchange(remoteIp = "10.0.0.7")

        APIKeyFilterImpl(apiKeyService).filter(exchange, chain).block()

        assertThat(chain.authorizationHeader).isEqualTo("Bearer access-token")
    }

    @Test
    fun givenApiSecretMissing_whenFiltering_thenDoNotLookupApiKey() {
        val apiKeyService = RecordingAPIKeyService(apiKey(allowedIPs = "10.0.0.7"))
        val chain = RecordingWebFilterChain()
        val request = MockServerHttpRequest.get("/v3/account")
            .remoteAddress(InetSocketAddress("10.0.0.7", 12345))
            .header("X-API-KEY", "api-key")
            .build()

        APIKeyFilterImpl(apiKeyService).filter(MockServerWebExchange.from(request), chain).block()

        assertThat(chain.authorizationHeader).isNull()
        assertThat(apiKeyService.calls).isZero()
    }

    private fun exchange(remoteIp: String): ServerWebExchange {
        val request = MockServerHttpRequest.get("/v3/account")
            .remoteAddress(InetSocketAddress(remoteIp, 12345))
            .header("X-API-KEY", "api-key")
            .header("X-API-SECRET", "api-secret")
            .build()
        return MockServerWebExchange.from(request)
    }

    private fun apiKey(allowedIPs: String?): APIKey {
        return APIKey(
            "user",
            "label",
            "access-token",
            null,
            allowedIPs,
            "api-key",
            isEnabled = true,
            isExpired = false
        )
    }

    private class RecordingAPIKeyService(private val apiKey: APIKey?) : APIKeyService {
        var calls = 0

        override suspend fun createAPIKey(
            userId: String,
            label: String,
            expirationTime: LocalDateTime?,
            allowedIPs: String?,
            currentToken: String
        ): Pair<String, APIKey> {
            error("not used")
        }

        override suspend fun getAPIKey(key: String, secret: String): APIKey? {
            calls += 1
            return apiKey
        }

        override suspend fun getKeysByUserId(userId: String): List<APIKey> {
            error("not used")
        }

        override suspend fun changeKeyState(userId: String, key: String, isEnabled: Boolean) {
            error("not used")
        }

        override suspend fun deleteKey(userId: String, key: String) {
            error("not used")
        }
    }

    private class RecordingWebFilterChain : WebFilterChain {
        var authorizationHeader: String? = null

        override fun filter(exchange: ServerWebExchange): Mono<Void> {
            authorizationHeader = exchange.request.headers.getFirst("Authorization")
            return Mono.empty()
        }
    }
}
