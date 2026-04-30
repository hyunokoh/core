package co.nilin.opex.wallet.app.controller

import co.nilin.opex.wallet.app.KafkaEnabledTest
import co.nilin.opex.wallet.app.dto.TransactionRequest
import co.nilin.opex.wallet.core.inout.TransferCommand
import co.nilin.opex.wallet.core.model.Amount
import co.nilin.opex.wallet.core.model.TransactionWithDetailHistory
import co.nilin.opex.wallet.core.model.TransferCategory
import co.nilin.opex.wallet.core.model.WalletType
import co.nilin.opex.wallet.core.spi.CurrencyService
import co.nilin.opex.wallet.core.spi.TransferManager
import co.nilin.opex.wallet.core.spi.WalletManager
import co.nilin.opex.wallet.core.spi.WalletOwnerManager
import kotlinx.coroutines.runBlocking
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.autoconfigure.web.reactive.AutoConfigureWebTestClient
import org.springframework.http.MediaType
import org.springframework.test.web.reactive.server.WebTestClient
import java.math.BigDecimal
import java.time.LocalDateTime
import java.time.ZoneId
import java.util.UUID

@AutoConfigureWebTestClient
class TransactionControllerIT : KafkaEnabledTest() {

    @Autowired
    private lateinit var webClient: WebTestClient

    @Autowired
    private lateinit var currencyService: CurrencyService

    @Autowired
    private lateinit var walletOwnerManager: WalletOwnerManager

    @Autowired
    private lateinit var walletManager: WalletManager

    @Autowired
    private lateinit var transferManager: TransferManager

    @Test
    fun whenGetTransactionsForUser_thenReturnsHistoryFromRealManagers() {
        val sourceUuid = UUID.randomUUID().toString()
        val receiverUuid = UUID.randomUUID().toString()
        val currencySymbol = "TC${System.nanoTime().toString().takeLast(8)}"
        val transferRef = "ref-${UUID.randomUUID()}"
        val now = LocalDateTime.now()

        runBlocking {
            currencyService.addCurrency(currencySymbol, currencySymbol, BigDecimal.ONE)
            val currency = currencyService.getCurrency(currencySymbol)!!

            val sender = walletOwnerManager.createWalletOwner(sourceUuid, "sender", "")
            val receiver = walletOwnerManager.createWalletOwner(receiverUuid, "receiver", "")

            val sourceWallet = walletManager.createWallet(
                sender,
                Amount(currency, BigDecimal.TEN),
                currency,
                WalletType.MAIN
            )
            val receiverWallet = walletManager.createWallet(
                receiver,
                Amount(currency, BigDecimal.ZERO),
                currency,
                WalletType.EXCHANGE
            )

            transferManager.transfer(
                TransferCommand(
                    sourceWallet,
                    receiverWallet,
                    Amount(currency, BigDecimal.ONE),
                    "controller integration transfer",
                    transferRef,
                    TransferCategory.NORMAL
                )
            )
        }

        val startMillis = now.minusMinutes(1).atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
        val endMillis = now.plusMinutes(1).atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()

        val response = webClient.post().uri("/transaction/$sourceUuid")
            .accept(MediaType.APPLICATION_JSON)
            .bodyValue(TransactionRequest(currencySymbol, TransferCategory.NORMAL, startMillis, endMillis, 10, 0, true))
            .exchange()
            .expectStatus().isOk
            .expectBodyList(TransactionWithDetailHistory::class.java)
            .returnResult()
            .responseBody ?: emptyList()

        assertEquals(1, response.size)
        assertEquals(sourceUuid, response.first().senderUuid)
        assertEquals(receiverUuid, response.first().receiverUuid)
        assertEquals(transferRef, response.first().ref)
        assertEquals(WalletType.MAIN, response.first().srcWalletType)
        assertEquals(WalletType.EXCHANGE, response.first().destWalletType)
    }
}
