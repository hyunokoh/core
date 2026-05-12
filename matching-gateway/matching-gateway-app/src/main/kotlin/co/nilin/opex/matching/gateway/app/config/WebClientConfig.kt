package co.nilin.opex.matching.gateway.app.config

import org.springframework.cloud.client.ServiceInstance
import org.springframework.cloud.client.loadbalancer.reactive.ReactiveLoadBalancer
import org.springframework.cloud.client.loadbalancer.reactive.ReactorLoadBalancerExchangeFilterFunction
import org.springframework.beans.factory.annotation.Value
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.http.client.reactive.ReactorClientHttpConnector
import org.springframework.web.reactive.function.client.WebClient
import org.zalando.logbook.Logbook
import org.zalando.logbook.netty.LogbookClientHandler
import io.netty.channel.ChannelOption
import reactor.netty.http.client.HttpClient
import java.time.Duration

@Configuration
class WebClientConfig {

    @Bean
    fun webClient(
        loadBalancerFactory: ReactiveLoadBalancer.Factory<ServiceInstance>,
        logbook: Logbook,
        @Value("\${app.http-client.connect-timeout-ms:3000}") connectTimeoutMs: Int,
        @Value("\${app.http-client.response-timeout-ms:10000}") responseTimeoutMs: Long
    ): WebClient {
        val client = HttpClient.create()
            .option(ChannelOption.CONNECT_TIMEOUT_MILLIS, connectTimeoutMs)
            .responseTimeout(Duration.ofMillis(responseTimeoutMs))
            .doOnConnected { it.addHandlerLast(LogbookClientHandler(logbook)) }
        return WebClient.builder()
            .clientConnector(ReactorClientHttpConnector(client))
            .filter(ReactorLoadBalancerExchangeFilterFunction(loadBalancerFactory, emptyList()))
            .build()
    }

}
