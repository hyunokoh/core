package co.nilin.opex.api.ports.binance.controller

import com.fasterxml.jackson.databind.ObjectMapper
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import org.springframework.http.MediaType
import org.springframework.web.reactive.function.client.ClientRequest
import org.springframework.web.reactive.function.client.ClientResponse
import org.springframework.web.reactive.function.client.ExchangeFunction
import org.springframework.web.reactive.function.client.WebClient
import reactor.core.publisher.Mono
import java.security.Principal

private class PolControllerTest {

    private val mapper = ObjectMapper()

    @Test
    fun `inclusion-proof forwards authenticated user_id and target epoch`(): Unit = runBlocking {
        val captured = mutableListOf<String>()
        val controller = controllerWith(captured) { _ ->
            jsonResponse(
                mapOf(
                    "epoch" to 7,
                    "user_id" to "alice",
                    "user_nonce" to "nonce",
                    "inclusion" to mapOf("leaf_balance" to 1000),
                ),
            )
        }

        val resp = controller.inclusionProof(Principal { "alice" }, epoch = 7)

        assertThat(captured).containsExactly("/api/v1/audit/user-proof?epoch=7&user_id=alice")
        assertThat(resp.path("epoch").asInt()).isEqualTo(7)
        assertThat(resp.path("user_id").asText()).isEqualTo("alice")
    }

    @Test
    fun `inclusion-proof falls back to latest epoch when none specified`(): Unit = runBlocking {
        val captured = mutableListOf<String>()
        val controller = controllerWith(captured) { req ->
            val path = req.url().path
            val query = req.url().query.orEmpty()
            when {
                path == "/api/v1/certificate/latest" ->
                    jsonResponse(mapOf("certificate" to mapOf("epoch" to 42)))
                path == "/api/v1/audit/user-proof" && query.contains("epoch=42") ->
                    jsonResponse(mapOf("epoch" to 42, "user_id" to "bob"))
                else -> jsonResponse(mapOf("error" to "unexpected"))
            }
        }

        val resp = controller.inclusionProof(Principal { "bob" }, epoch = null)

        assertThat(captured).containsExactly(
            "/api/v1/certificate/latest",
            "/api/v1/audit/user-proof?epoch=42&user_id=bob",
        )
        assertThat(resp.path("epoch").asInt()).isEqualTo(42)
    }

    @Test
    fun `latestCertificate returns the snapshot server's response unchanged`(): Unit = runBlocking {
        val captured = mutableListOf<String>()
        val controller = controllerWith(captured) {
            jsonResponse(
                mapOf(
                    "certificate" to mapOf("epoch" to 99, "total_liability" to 1234),
                    "signature" to "sig",
                    "pubkey" to "pk",
                ),
            )
        }

        val resp = controller.latestCertificate()

        assertThat(captured).containsExactly("/api/v1/certificate/latest")
        assertThat(resp.path("certificate").path("total_liability").asLong()).isEqualTo(1234)
        assertThat(resp.path("signature").asText()).isEqualTo("sig")
    }

    @Test
    fun `signingPubkey returns the pubkey envelope`(): Unit = runBlocking {
        val captured = mutableListOf<String>()
        val controller = controllerWith(captured) {
            jsonResponse(mapOf("algorithm" to "ed25519", "pubkey" to "BASE64KEY"))
        }

        val resp = controller.signingPubkey()

        assertThat(captured).containsExactly("/api/v1/certificate/pubkey")
        assertThat(resp.path("pubkey").asText()).isEqualTo("BASE64KEY")
    }

    private fun controllerWith(
        captured: MutableList<String>,
        handler: (ClientRequest) -> Mono<ClientResponse>,
    ): PolController {
        val exchangeFn = ExchangeFunction { req ->
            captured.add(req.url().path + (req.url().query?.let { "?$it" } ?: ""))
            handler(req)
        }
        val builder = WebClient.builder().exchangeFunction(exchangeFn)
        val controller = PolController(builder)
        // Set the @Value-injected baseUrl manually for tests.
        val field = PolController::class.java.getDeclaredField("baseUrl")
        field.isAccessible = true
        field.set(controller, "http://snapshot")
        return controller
    }

    private fun jsonResponse(payload: Any): Mono<ClientResponse> {
        val body = mapper.writeValueAsString(payload)
        val resp = ClientResponse.create(org.springframework.http.HttpStatus.OK)
            .header("Content-Type", MediaType.APPLICATION_JSON_VALUE)
            .body(body)
            .build()
        return Mono.just(resp)
    }
}
