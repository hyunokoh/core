package co.nilin.opex.api.app.config

import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.context.annotation.Profile
import org.springframework.security.config.annotation.web.reactive.EnableWebFluxSecurity
import org.springframework.security.config.web.server.ServerHttpSecurity
import org.springframework.security.core.authority.SimpleGrantedAuthority
import org.springframework.security.core.context.ReactiveSecurityContextHolder
import org.springframework.security.oauth2.jwt.Jwt
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken
import org.springframework.security.web.server.SecurityWebFilterChain
import org.springframework.web.server.WebFilter

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
        val user = exchange.request.headers.getFirst("X-Opex-User") ?: "e2e-user"
        val jwt = Jwt.withTokenValue("e2e-token:$user")
            .header("alg", "none")
            .subject(user)
            .claim("scope", "trust")
            .build()
        val authentication = JwtAuthenticationToken(
            jwt,
            listOf(SimpleGrantedAuthority("SCOPE_trust"))
        )
        chain.filter(exchange)
            .contextWrite(ReactiveSecurityContextHolder.withAuthentication(authentication))
    }
}
