package co.nilin.opex.accountant.app

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.WebApplicationType
import org.springframework.boot.builder.SpringApplicationBuilder
import org.springframework.context.annotation.ComponentScan

@SpringBootApplication
@ComponentScan("co.nilin.opex")
class AccountantApp

fun main(args: Array<String>) {
    SpringApplicationBuilder(AccountantApp::class.java)
        .web(WebApplicationType.REACTIVE)
        .run(*args)
}
