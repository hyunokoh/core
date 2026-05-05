package co.nilin.opex.api.app.interceptor

import co.nilin.opex.api.core.inout.APIKey
import co.nilin.opex.api.core.spi.APIKeyFilter
import co.nilin.opex.api.core.spi.APIKeyService
import kotlinx.coroutines.runBlocking
import org.springframework.stereotype.Component
import org.springframework.web.server.ServerWebExchange
import org.springframework.web.server.WebFilter
import org.springframework.web.server.WebFilterChain
import reactor.core.publisher.Mono

@Component
class APIKeyFilterImpl(private val apiKeyService: APIKeyService) : APIKeyFilter, WebFilter {

    override fun filter(exchange: ServerWebExchange, chain: WebFilterChain): Mono<Void> {
        val request = exchange.request
        val key = request.headers["X-API-KEY"]
        if (!key.isNullOrEmpty()) {
            val secret = request.headers["X-API-SECRET"]
            if (secret.isNullOrEmpty())
                return chain.filter(exchange)

            val apiKey = runBlocking { apiKeyService.getAPIKey(key[0], secret[0]) }
            if (apiKey != null && apiKey.isEnabled && apiKey.accessToken != null && !apiKey.isExpired &&
                isAllowedClientIp(apiKey, exchange)
            ) {
                val req = exchange.request.mutate()
                    .header("Authorization", "Bearer ${apiKey.accessToken}")
                    .build()
                return chain.filter(exchange.mutate().request(req).build())
            }
        }
        return chain.filter(exchange)
    }

    private fun isAllowedClientIp(apiKey: APIKey, exchange: ServerWebExchange): Boolean {
        val allowedIps = apiKey.allowedIPs
            ?.split(',', ';', ' ', '\n', '\t')
            ?.map { it.trim() }
            ?.filter { it.isNotEmpty() }
            ?: return true
        if (allowedIps.isEmpty())
            return true

        val remoteAddress = exchange.request.remoteAddress?.address?.hostAddress
            ?: exchange.request.remoteAddress?.hostString
            ?: return false
        return allowedIps.contains(remoteAddress)
    }

}
