package co.nilin.opex.accountant.ports.walletproxy.proxy

import co.nilin.opex.accountant.core.model.WalletType
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertThrows
import org.springframework.web.reactive.function.client.ExchangeFunction
import org.springframework.web.reactive.function.client.WebClient
import reactor.core.publisher.Mono
import java.math.BigDecimal

class WalletProxyImplTest {

    @Test
    fun givenWalletTransferNeverResponds_whenTransferCalled_thenTimesOut() {
        val proxy = WalletProxyImpl(nonCompletingWebClient(), "http://wallet", 50)

        assertThrows<TimeoutCancellationException> {
            runBlocking {
                proxy.transfer(
                    "USDT",
                    WalletType.MAIN,
                    "sender",
                    WalletType.EXCHANGE,
                    "sender",
                    BigDecimal.ONE,
                    "description",
                    "reference",
                    "ORDER_CREATE"
                )
            }
        }
    }

    @Test
    fun givenWalletInquiryNeverResponds_whenCanFulfilCalled_thenTimesOut() {
        val proxy = WalletProxyImpl(nonCompletingWebClient(), "http://wallet", 50)

        assertThrows<TimeoutCancellationException> {
            runBlocking {
                proxy.canFulfil("USDT", WalletType.MAIN, "owner", BigDecimal.ONE)
            }
        }
    }

    private fun nonCompletingWebClient(): WebClient {
        return WebClient.builder()
            .exchangeFunction(ExchangeFunction { Mono.never() })
            .build()
    }
}
