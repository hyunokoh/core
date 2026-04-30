package co.nilin.opex.wallet.app.integration

import co.nilin.opex.wallet.app.KafkaEnabledTest
import co.nilin.opex.wallet.core.model.Amount
import co.nilin.opex.wallet.core.model.TransferCategory
import co.nilin.opex.wallet.core.model.WalletType
import co.nilin.opex.wallet.core.spi.CurrencyService
import co.nilin.opex.wallet.core.spi.WalletManager
import co.nilin.opex.wallet.core.spi.WalletOwnerManager
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.web.server.LocalServerPort
import org.springframework.http.MediaType
import org.springframework.web.reactive.function.client.WebClient
import org.springframework.web.reactive.function.client.bodyToMono
import kotlinx.coroutines.reactive.awaitFirst
import java.math.BigDecimal
import java.util.UUID

class AccountantWalletProxyIT : KafkaEnabledTest() {

    @LocalServerPort
    private var port: Int = 0

    @Autowired
    private lateinit var currencyService: CurrencyService

    @Autowired
    private lateinit var walletOwnerManager: WalletOwnerManager

    @Autowired
    private lateinit var walletManager: WalletManager

    @Test
    fun givenRealWalletApp_whenAccountantWalletProxyContractTransfers_thenBalancesMove() {
        val symbol = "PX${System.nanoTime().toString().takeLast(8)}"
        val senderUuid = UUID.randomUUID().toString()
        val receiverUuid = UUID.randomUUID().toString()
        val transferAmount = BigDecimal("3")
        val webClient = WebClient.builder()
            .baseUrl("http://localhost:$port")
            .build()

        runBlocking {
            currencyService.addCurrency(symbol, symbol, BigDecimal.ONE)
            val currency = currencyService.getCurrency(symbol)!!
            val sender = walletOwnerManager.createWalletOwner(senderUuid, "sender", "")
            val receiver = walletOwnerManager.createWalletOwner(receiverUuid, "receiver", "")
            walletManager.createWallet(sender, Amount(currency, BigDecimal.TEN), currency, WalletType.MAIN)
            walletManager.createWallet(receiver, Amount(currency, BigDecimal.ZERO), currency, WalletType.EXCHANGE)

            val canFulfil = webClient.get()
                .uri("/inquiry/$senderUuid/wallet_type/${WalletType.MAIN}/can_withdraw/${transferAmount}_$symbol")
                .retrieve()
                .bodyToMono<CanFulfilResponse>()
                .awaitFirst()
            assertTrue(canFulfil.result)

            webClient.post()
                .uri("/v2/transfer/${transferAmount}_$symbol/from/${senderUuid}_${WalletType.MAIN}/to/${receiverUuid}_${WalletType.EXCHANGE}")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(
                    TransferBody(
                        "accountant integration transfer",
                        "accountant-wallet-proxy-${UUID.randomUUID()}",
                        TransferCategory.ORDER_CREATE.name
                    )
                )
                .retrieve()
                .bodyToMono<String>()
                .awaitFirst()

            val sourceWallet = walletManager.findWalletByOwnerAndCurrencyAndType(sender, WalletType.MAIN, currency)!!
            val receiverWallet = walletManager.findWalletByOwnerAndCurrencyAndType(receiver, WalletType.EXCHANGE, currency)!!
            assertEquals(0, sourceWallet.balance.amount.compareTo(BigDecimal("7")))
            assertEquals(0, receiverWallet.balance.amount.compareTo(BigDecimal("3")))
        }
    }

    data class CanFulfilResponse(val result: Boolean)

    data class TransferBody(
        val description: String?,
        val transferRef: String?,
        val transferCategory: String
    )
}
