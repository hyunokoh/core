package co.nilin.opex.api.ports.proxy.impl

import co.nilin.opex.api.core.inout.MatchConstraint
import co.nilin.opex.api.core.inout.MatchingOrderType
import co.nilin.opex.api.core.inout.OrderDirection
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import org.springframework.http.HttpHeaders
import org.springframework.http.HttpMethod
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.test.util.ReflectionTestUtils
import org.springframework.web.reactive.function.client.ClientRequest
import org.springframework.web.reactive.function.client.ClientResponse
import org.springframework.web.reactive.function.client.ExchangeFunction
import org.springframework.web.reactive.function.client.WebClient
import reactor.core.publisher.Mono
import java.math.BigDecimal

class MatchingGatewayProxyImplTest {

    @Test
    fun givenOwnerAndToken_whenCreatingOrder_thenForwardsOwnerAndAuthorizationHeaders(): Unit = runBlocking {
        val exchange = RecordingExchangeFunction()
        val proxy = proxy(exchange)

        val result = proxy.createNewOrder(
            uuid = "user-1",
            pair = "ETH_USDT",
            price = BigDecimal("100"),
            quantity = BigDecimal("0.5"),
            direction = OrderDirection.ASK,
            matchConstraint = MatchConstraint.GTC,
            orderType = MatchingOrderType.LIMIT_ORDER,
            userLevel = "*",
            clientOrderId = "client-1",
            token = "token-1"
        )

        assertThat(result?.offset).isEqualTo(42)
        assertThat(exchange.request.method()).isEqualTo(HttpMethod.POST)
        assertThat(exchange.request.url().toString()).isEqualTo("http://matching-gateway/order")
        assertThat(exchange.request.headers().getFirst(HttpHeaders.AUTHORIZATION)).isEqualTo("Bearer token-1")
        assertThat(exchange.request.headers().getFirst("X-Opex-User")).isEqualTo("user-1")
    }

    @Test
    fun givenOwnerAndToken_whenCancelingOrder_thenForwardsOwnerAndAuthorizationHeaders(): Unit = runBlocking {
        val exchange = RecordingExchangeFunction()
        val proxy = proxy(exchange)

        val result = proxy.cancelOrder(
            ouid = "ouid-1",
            uuid = "user-2",
            orderId = 123,
            symbol = "ETH_USDT",
            token = "token-2"
        )

        assertThat(result?.offset).isEqualTo(42)
        assertThat(exchange.request.method()).isEqualTo(HttpMethod.POST)
        assertThat(exchange.request.url().toString()).isEqualTo("http://matching-gateway/order/cancel")
        assertThat(exchange.request.headers().getFirst(HttpHeaders.AUTHORIZATION)).isEqualTo("Bearer token-2")
        assertThat(exchange.request.headers().getFirst("X-Opex-User")).isEqualTo("user-2")
    }

    private fun proxy(exchange: RecordingExchangeFunction): MatchingGatewayProxyImpl {
        val proxy = MatchingGatewayProxyImpl(WebClient.builder().exchangeFunction(exchange).build())
        ReflectionTestUtils.setField(proxy, "baseUrl", "http://matching-gateway")
        return proxy
    }

    private class RecordingExchangeFunction : ExchangeFunction {
        lateinit var request: ClientRequest

        override fun exchange(request: ClientRequest): Mono<ClientResponse> {
            this.request = request
            return Mono.just(
                ClientResponse.create(HttpStatus.OK)
                    .header(HttpHeaders.CONTENT_TYPE, MediaType.APPLICATION_JSON_VALUE)
                    .body("""{"offset":42}""")
                    .build()
            )
        }
    }
}
