package co.nilin.opex.accountant.ports.walletproxy.config

import org.springframework.beans.factory.annotation.Value
import org.springframework.cloud.client.ServiceInstance
import org.springframework.cloud.client.loadbalancer.reactive.ReactiveLoadBalancer
import org.springframework.cloud.client.loadbalancer.reactive.ReactorLoadBalancerExchangeFilterFunction
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.http.client.reactive.ReactorClientHttpConnector
import org.springframework.web.reactive.function.client.WebClient
import org.zalando.logbook.Logbook
import org.zalando.logbook.netty.LogbookClientHandler
import reactor.netty.http.client.HttpClient
import java.time.Duration

@Configuration
class WebClientConfig {

    @Bean
    fun webClient(
        loadBalancerFactory: ReactiveLoadBalancer.Factory<ServiceInstance>,
        logbook: Logbook,
        @Value("\${app.wallet.response-timeout-ms:10000}")
        responseTimeoutMs: Long
    ): WebClient {
        val client = HttpClient.create()
            .responseTimeout(Duration.ofMillis(responseTimeoutMs))
            .doOnConnected { it.addHandlerLast(LogbookClientHandler(logbook)) }
        return WebClient.builder()
            .clientConnector(ReactorClientHttpConnector(client))
            .filter(ReactorLoadBalancerExchangeFilterFunction(loadBalancerFactory, emptyList()))
            .build()
    }

}
