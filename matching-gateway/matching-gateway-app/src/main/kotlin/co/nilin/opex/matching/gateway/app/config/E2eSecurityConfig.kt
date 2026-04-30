package co.nilin.opex.matching.gateway.app.config

import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Profile
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken
import org.springframework.security.config.annotation.web.reactive.EnableWebFluxSecurity
import org.springframework.security.config.web.server.ServerHttpSecurity
import org.springframework.security.core.authority.SimpleGrantedAuthority
import org.springframework.security.core.context.ReactiveSecurityContextHolder
import org.springframework.security.web.server.SecurityWebFilterChain
import org.springframework.web.server.WebFilter

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
        val user = exchange.request.headers.getFirst("X-Opex-User") ?: "e2e-user"
        val authentication = UsernamePasswordAuthenticationToken(
            user,
            "e2e",
            listOf(SimpleGrantedAuthority("SCOPE_trust"))
        )
        chain.filter(exchange)
            .contextWrite(ReactiveSecurityContextHolder.withAuthentication(authentication))
    }
}
