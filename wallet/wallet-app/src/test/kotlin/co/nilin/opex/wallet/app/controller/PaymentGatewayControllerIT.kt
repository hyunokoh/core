package co.nilin.opex.wallet.app.controller

import co.nilin.opex.wallet.app.KafkaEnabledTest
import co.nilin.opex.wallet.app.dto.PaymentCurrency
import co.nilin.opex.wallet.app.dto.PaymentDepositRequest
import co.nilin.opex.wallet.app.dto.PaymentDepositResponse
import co.nilin.opex.wallet.core.model.Amount
import co.nilin.opex.wallet.core.model.WalletType
import co.nilin.opex.wallet.core.spi.CurrencyService
import co.nilin.opex.wallet.core.spi.WalletManager
import co.nilin.opex.wallet.core.spi.WalletOwnerManager
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.autoconfigure.web.reactive.AutoConfigureWebTestClient
import org.springframework.http.MediaType
import org.springframework.test.web.reactive.server.WebTestClient
import java.math.BigDecimal
import java.util.UUID

@AutoConfigureWebTestClient
class PaymentGatewayControllerIT : KafkaEnabledTest() {

    @Autowired
    private lateinit var webClient: WebTestClient

    @Autowired
    private lateinit var currencyService: CurrencyService

    @Autowired
    private lateinit var walletManager: WalletManager

    @Autowired
    private lateinit var walletOwnerManager: WalletOwnerManager

    @BeforeEach
    fun setup() = runBlocking {
        currencyService.addCurrency("IRT", "IRT", BigDecimal.ZERO)
    }

    @Test
    fun givenFundedSystemWallet_whenPaymentDepositRequested_thenCreditsReceiverWallet() = runBlocking {
        val userId = UUID.randomUUID().toString()
        val currency = currencyService.getCurrency("IRT")!!
        val systemOwner = walletOwnerManager.findWalletOwner(walletOwnerManager.systemUuid)!!
        val initialSystemWallet = walletManager.findWalletByOwnerAndCurrencyAndType(systemOwner, WalletType.MAIN, currency)
            ?: walletManager.createWallet(systemOwner, Amount(currency, BigDecimal.valueOf(1_000)), currency, WalletType.MAIN)
        val systemWallet = if (initialSystemWallet.balance.amount < BigDecimal.valueOf(1_000)) {
            walletManager.increaseBalance(initialSystemWallet, BigDecimal.valueOf(1_000) - initialSystemWallet.balance.amount)
            walletManager.findWalletByOwnerAndCurrencyAndType(systemOwner, WalletType.MAIN, currency)!!
        } else {
            initialSystemWallet
        }

        val response = webClient.post().uri("/payment/internal/deposit")
            .accept(MediaType.APPLICATION_JSON)
            .bodyValue(PaymentDepositRequest(userId, BigDecimal.valueOf(125), PaymentCurrency.TOMAN, "pg-${UUID.randomUUID()}", "payment deposit"))
            .exchange()
            .expectStatus().isOk
            .expectBody(PaymentDepositResponse::class.java)
            .returnResult().responseBody!!

        val receiverOwner = walletOwnerManager.findWalletOwner(userId)!!
        val refreshedSystemWallet = walletManager.findWalletByOwnerAndCurrencyAndType(systemOwner, WalletType.MAIN, currency)!!
        val receiverWallet = walletManager.findWalletByOwnerAndCurrencyAndType(receiverOwner, WalletType.MAIN, currency)!!

        Assertions.assertTrue(response.success)
        Assertions.assertEquals(systemWallet.balance.amount - BigDecimal.valueOf(125), refreshedSystemWallet.balance.amount)
        Assertions.assertEquals(BigDecimal.valueOf(125), receiverWallet.balance.amount)
    }

    @Test
    fun givenNegativeAmount_whenPaymentDepositRequested_thenBadRequestAndNoWalletOwnerCreated() = runBlocking {
        val userId = UUID.randomUUID().toString()
        val currency = currencyService.getCurrency("IRT")!!
        val systemOwner = walletOwnerManager.findWalletOwner(walletOwnerManager.systemUuid)!!
        val systemWallet = walletManager.findWalletByOwnerAndCurrencyAndType(systemOwner, WalletType.MAIN, currency)
            ?: walletManager.createWallet(systemOwner, Amount(currency, BigDecimal.valueOf(500)), currency, WalletType.MAIN)

        webClient.post().uri("/payment/internal/deposit")
            .accept(MediaType.APPLICATION_JSON)
            .bodyValue(PaymentDepositRequest(userId, BigDecimal.valueOf(-1), PaymentCurrency.TOMAN, "pg-${UUID.randomUUID()}", "negative deposit"))
            .exchange()
            .expectStatus().isBadRequest

        val refreshedSystemWallet = walletManager.findWalletByOwnerAndCurrencyAndType(systemOwner, WalletType.MAIN, currency)!!

        Assertions.assertNull(walletOwnerManager.findWalletOwner(userId))
        Assertions.assertEquals(systemWallet.balance.amount, refreshedSystemWallet.balance.amount)
    }
}
