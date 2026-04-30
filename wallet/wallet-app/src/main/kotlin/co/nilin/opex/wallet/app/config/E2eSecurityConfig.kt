package co.nilin.opex.wallet.app.config

import co.nilin.opex.wallet.core.inout.WithdrawData
import co.nilin.opex.wallet.core.model.PropagateCurrencyChanges
import co.nilin.opex.wallet.core.model.otc.CurrencyImplementationResponse
import co.nilin.opex.wallet.core.model.otc.FetchCurrencyInfo
import co.nilin.opex.wallet.core.spi.BcGatewayProxy
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.context.annotation.Primary
import org.springframework.context.annotation.Profile
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken
import org.springframework.security.config.annotation.web.reactive.EnableWebFluxSecurity
import org.springframework.security.config.web.server.ServerHttpSecurity
import org.springframework.security.core.authority.SimpleGrantedAuthority
import org.springframework.security.core.context.ReactiveSecurityContextHolder
import org.springframework.security.web.server.SecurityWebFilterChain
import org.springframework.web.server.WebFilter
import java.math.BigDecimal

@Configuration
@EnableWebFluxSecurity
@Profile("e2e")
class E2eSecurityConfig {

    @Bean
    fun springSecurityFilterChain(http: ServerHttpSecurity): SecurityWebFilterChain =
        http.csrf().disable()
            .authorizeExchange()
            .anyExchange().permitAll()
            .and()
            .build()

    @Bean
    fun e2ePrincipalWebFilter(): WebFilter = WebFilter { exchange, chain ->
        val user = exchange.request.headers.getFirst("X-Opex-User")
            ?: exchange.request.path.pathWithinApplication().value()
                .takeIf { it.startsWith("/inquiry/") }
                ?.removePrefix("/inquiry/")
                ?.substringBefore("/")
            ?: "e2e-user"
        val authentication = UsernamePasswordAuthenticationToken(
            user,
            "e2e",
            listOf(
                SimpleGrantedAuthority("SCOPE_trust"),
                SimpleGrantedAuthority("ROLE_admin_finance")
            )
        )
        chain.filter(exchange)
            .contextWrite(ReactiveSecurityContextHolder.withAuthentication(authentication))
    }

    @Bean
    @Primary
    fun e2eBcGatewayProxy(): BcGatewayProxy = object : BcGatewayProxy {
        override suspend fun createCurrency(currencyImp: PropagateCurrencyChanges): CurrencyImplementationResponse {
            throw UnsupportedOperationException("E2E wallet flow does not create blockchain currencies")
        }

        override suspend fun updateCurrency(currencyImp: PropagateCurrencyChanges): CurrencyImplementationResponse {
            throw UnsupportedOperationException("E2E wallet flow does not update blockchain currencies")
        }

        override suspend fun getCurrencyInfo(symbol: String): FetchCurrencyInfo? = null

        override suspend fun getWithdrawData(symbol: String, network: String): WithdrawData =
            WithdrawData(
                isEnabled = true,
                fee = BigDecimal("0.1"),
                minimum = BigDecimal("1")
            )
    }
}
