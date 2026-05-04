package co.nilin.opex.api.ports.binance.controller

import co.nilin.opex.api.core.inout.*
import co.nilin.opex.api.core.spi.*
import co.nilin.opex.api.ports.binance.data.WithDrawRequest
import co.nilin.opex.common.OpexError
import co.nilin.opex.common.utils.Interval
import co.nilin.opex.utility.error.data.OpexException
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.Test
import org.springframework.security.core.context.SecurityContext
import org.springframework.security.core.context.SecurityContextImpl
import org.springframework.security.oauth2.jwt.Jwt
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken
import java.math.BigDecimal
import java.time.LocalDateTime
import java.util.Date

private class WalletControllerTest {

    @Test
    fun givenExpiredTimestamp_whenAssignAddressRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val blockchainGatewayProxy = RecordingBlockchainGatewayProxy()
        val controller = controller(blockchainGatewayProxy = blockchainGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.assignAddress("USDT", "ETH", null, signedTimestamp() - 6000, securityContext())
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(blockchainGatewayProxy.assignAddressCallCount).isZero()
    }

    @Test
    fun givenTooLargeRecvWindow_whenWithdrawHistoryRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking {
                controller.getWithdrawTransactions(
                    coin = "USDT",
                    withdrawOrderId = null,
                    withdrawStatus = null,
                    offset = null,
                    limit = null,
                    startTime = null,
                    endTime = null,
                    ascendingByTime = null,
                    recvWindow = 60001,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWithdrawTransactionsCallCount).isZero()
    }

    @Test
    fun givenExpiredTimestamp_whenWithdrawHistoryV2Requested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)
        val request = WithDrawRequest(
            coin = "USDT",
            withdrawOrderId = null,
            withdrawStatus = null,
            offset = null,
            limit = null,
            startTime = null,
            endTime = null,
            ascendingByTime = null,
            recvWindow = null,
            timestamp = signedTimestamp() - 6000
        )

        assertThatThrownBy {
            runBlocking { controller.getWithdrawTransactionsV2(request, securityContext()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWithdrawTransactionsCallCount).isZero()
    }

    @Test
    fun givenInvalidLimit_whenDepositHistoryRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking {
                controller.getDepositTransactions(
                    coin = "USDT",
                    status = null,
                    startTime = null,
                    endTime = null,
                    offset = null,
                    limit = 0,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    ascendingByTime = null,
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getDepositTransactionsCallCount).isZero()
    }

    @Test
    fun givenInvalidLimit_whenWithdrawHistoryV2Requested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)
        val request = WithDrawRequest(
            coin = "USDT",
            withdrawOrderId = null,
            withdrawStatus = null,
            offset = null,
            limit = 1001,
            startTime = null,
            endTime = null,
            ascendingByTime = null,
            recvWindow = null,
            timestamp = signedTimestamp()
        )

        assertThatThrownBy {
            runBlocking { controller.getWithdrawTransactionsV2(request, securityContext()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWithdrawTransactionsCallCount).isZero()
    }

    @Test
    fun givenNegativeOffset_whenWithdrawHistoryRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking {
                controller.getWithdrawTransactions(
                    coin = "USDT",
                    withdrawOrderId = null,
                    withdrawStatus = null,
                    offset = -1,
                    limit = null,
                    startTime = null,
                    endTime = null,
                    ascendingByTime = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWithdrawTransactionsCallCount).isZero()
    }

    @Test
    fun givenInvertedTimeRange_whenDepositHistoryRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking {
                controller.getDepositTransactions(
                    coin = "USDT",
                    status = null,
                    startTime = 2000,
                    endTime = 1000,
                    offset = null,
                    limit = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    ascendingByTime = null,
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getDepositTransactionsCallCount).isZero()
    }

    @Test
    fun givenNegativeStartTime_whenWithdrawHistoryV2Requested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)
        val request = WithDrawRequest(
            coin = "USDT",
            withdrawOrderId = null,
            withdrawStatus = null,
            offset = null,
            limit = null,
            startTime = -1,
            endTime = null,
            ascendingByTime = null,
            recvWindow = null,
            timestamp = signedTimestamp()
        )

        assertThatThrownBy {
            runBlocking { controller.getWithdrawTransactionsV2(request, securityContext()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWithdrawTransactionsCallCount).isZero()
    }

    @Test
    fun givenStatus_whenWithdrawHistoryRequested_thenFiltersReturnedRows(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            withdraws = listOf(
                withdrawHistory(withdrawId = 10, status = "DONE"),
                withdrawHistory(withdrawId = 11, status = "REJECTED")
            )
        )
        val controller = controller(walletProxy = walletProxy)

        val response = controller.getWithdrawTransactions(
            coin = "USDT",
            withdrawOrderId = null,
            withdrawStatus = 1,
            offset = null,
            limit = null,
            startTime = null,
            endTime = null,
            ascendingByTime = null,
            recvWindow = null,
            timestamp = signedTimestamp(),
            securityContext = securityContext()
        )

        assertThat(response).hasSize(1)
        assertThat(response[0].id).isEqualTo("10")
        assertThat(response[0].status).isEqualTo(1)
    }

    @Test
    fun givenStatus_whenWithdrawHistoryV2Requested_thenFiltersReturnedRows(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            withdraws = listOf(
                withdrawHistory(withdrawId = 20, status = "CREATED"),
                withdrawHistory(withdrawId = 21, status = "DONE")
            )
        )
        val controller = controller(walletProxy = walletProxy)
        val request = WithDrawRequest(
            coin = "USDT",
            withdrawOrderId = null,
            withdrawStatus = 1,
            offset = null,
            limit = null,
            startTime = null,
            endTime = null,
            ascendingByTime = null,
            recvWindow = null,
            timestamp = signedTimestamp()
        )

        val response = controller.getWithdrawTransactionsV2(request, securityContext())

        assertThat(response).hasSize(1)
        assertThat(response[0].id).isEqualTo("21")
        assertThat(response[0].status).isEqualTo(1)
    }

    @Test
    fun givenWithdrawOrderId_whenWithdrawHistoryRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking {
                controller.getWithdrawTransactions(
                    coin = "USDT",
                    withdrawOrderId = "client-1",
                    withdrawStatus = null,
                    offset = null,
                    limit = null,
                    startTime = null,
                    endTime = null,
                    ascendingByTime = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWithdrawTransactionsCallCount).isZero()
    }

    @Test
    fun givenDepositStatus_whenDepositHistoryRequested_thenFiltersReturnedRows(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            deposits = listOf(
                TransactionHistoryResponse(
                    id = 1,
                    currency = "USDT",
                    amount = BigDecimal.TEN,
                    description = null,
                    ref = "tx-1",
                    date = 1000
                )
            )
        )
        val blockchainGatewayProxy = RecordingBlockchainGatewayProxy(
            depositDetails = listOf(
                DepositDetails(
                    hash = "tx-1",
                    address = "0x1",
                    memo = null,
                    amount = BigDecimal.TEN,
                    chain = "ETH",
                    isToken = true,
                    tokenAddress = null
                )
            )
        )
        val controller = controller(walletProxy = walletProxy, blockchainGatewayProxy = blockchainGatewayProxy)

        val completedDeposits = controller.getDepositTransactions(
            coin = "USDT",
            status = 1,
            startTime = null,
            endTime = null,
            offset = null,
            limit = null,
            recvWindow = null,
            timestamp = signedTimestamp(),
            ascendingByTime = null,
            securityContext = securityContext()
        )
        val pendingDeposits = controller.getDepositTransactions(
            coin = "USDT",
            status = 0,
            startTime = null,
            endTime = null,
            offset = null,
            limit = null,
            recvWindow = null,
            timestamp = signedTimestamp(),
            ascendingByTime = null,
            securityContext = securityContext()
        )

        assertThat(completedDeposits).hasSize(1)
        assertThat(pendingDeposits).isEmpty()
    }

    private fun controller(
        walletProxy: RecordingWalletProxy = RecordingWalletProxy(),
        blockchainGatewayProxy: RecordingBlockchainGatewayProxy = RecordingBlockchainGatewayProxy()
    ) = WalletController(
        walletProxy,
        RecordingSymbolMapper(),
        RecordingMarketDataProxy(),
        RecordingAccountantProxy(),
        blockchainGatewayProxy
    )

    private fun securityContext(): SecurityContext {
        val jwt = Jwt.withTokenValue("token-1")
            .header("alg", "none")
            .subject("user-1")
            .build()
        return SecurityContextImpl(JwtAuthenticationToken(jwt))
    }

    private fun signedTimestamp(): Long = Date().time

    private fun org.assertj.core.api.AbstractThrowableAssert<*, out Throwable>.isOpexError(error: OpexError) {
        isInstanceOf(OpexException::class.java)
            .extracting("error")
            .isEqualTo(error)
    }

    private fun withdrawHistory(withdrawId: Long, status: String) = WithdrawHistoryResponse(
        withdrawId = withdrawId,
        uuid = "user-1",
        amount = BigDecimal.TEN,
        currency = "USDT",
        acceptedFee = BigDecimal.ZERO,
        appliedFee = BigDecimal.ZERO,
        destAmount = BigDecimal.TEN,
        destSymbol = "USDT",
        destAddress = "0x1",
        destNetwork = "ETH",
        destNote = null,
        destTransactionRef = "tx-$withdrawId",
        statusReason = null,
        status = status,
        createDate = 1000,
        acceptDate = 2000
    )

    private class RecordingWalletProxy(
        private val deposits: List<TransactionHistoryResponse> = emptyList(),
        private val withdraws: List<WithdrawHistoryResponse> = emptyList()
    ) : WalletProxy {
        var getDepositTransactionsCallCount = 0
        var getWithdrawTransactionsCallCount = 0

        override suspend fun getWallets(uuid: String?, token: String?): List<Wallet> = emptyList()

        override suspend fun getWallet(uuid: String?, token: String?, symbol: String): Wallet =
            Wallet(symbol, BigDecimal.ZERO, BigDecimal.ZERO, BigDecimal.ZERO)

        override suspend fun getOwnerLimits(uuid: String?, token: String?): OwnerLimitsResponse =
            OwnerLimitsResponse(canTrade = true, canWithdraw = true, canDeposit = true)

        override suspend fun getDepositTransactions(
            uuid: String,
            token: String?,
            coin: String?,
            startTime: Long?,
            endTime: Long?,
            limit: Int,
            offset: Int,
            ascendingByTime: Boolean?
        ): List<TransactionHistoryResponse> {
            getDepositTransactionsCallCount += 1
            return deposits
        }

        override suspend fun getWithdrawTransactions(
            uuid: String,
            token: String?,
            coin: String?,
            startTime: Long?,
            endTime: Long?,
            limit: Int,
            offset: Int,
            ascendingByTime: Boolean?
        ): List<WithdrawHistoryResponse> {
            getWithdrawTransactionsCallCount += 1
            return withdraws
        }
    }

    private class RecordingSymbolMapper : SymbolMapper {
        override suspend fun fromInternalSymbol(symbol: String?): String? = symbol

        override suspend fun toInternalSymbol(alias: String?): String? = alias

        override suspend fun symbolToAliasMap(): Map<String, String> = emptyMap()
    }

    private class RecordingMarketDataProxy : MarketDataProxy {
        override suspend fun getTradeTickerData(interval: Interval): List<PriceChange> = emptyList()

        override suspend fun getTradeTickerDataBySymbol(symbol: String, interval: Interval): PriceChange =
            throw UnsupportedOperationException()

        override suspend fun openBidOrders(symbol: String, limit: Int): List<OrderBook> = emptyList()

        override suspend fun openAskOrders(symbol: String, limit: Int): List<OrderBook> = emptyList()

        override suspend fun lastOrder(symbol: String): Order? = null

        override suspend fun recentTrades(symbol: String, limit: Int): List<MarketTrade> = emptyList()

        override suspend fun lastPrice(symbol: String?): List<PriceTicker> = emptyList()

        override suspend fun getBestPriceForSymbols(symbols: List<String>): List<BestPrice> = emptyList()

        override suspend fun getCandleInfo(
            symbol: String,
            interval: String,
            startTime: Long?,
            endTime: Long?,
            limit: Int
        ): List<CandleData> = emptyList()

        override suspend fun getMarketCurrencyRates(quote: String, base: String?): List<CurrencyRate> = emptyList()

        override suspend fun getExternalCurrencyRates(quote: String, base: String?): List<CurrencyRate> = emptyList()

        override suspend fun countActiveUsers(interval: Interval): Long = 0

        override suspend fun countTotalOrders(interval: Interval): Long = 0

        override suspend fun countTotalTrades(interval: Interval): Long = 0
    }

    private class RecordingAccountantProxy : AccountantProxy {
        override suspend fun getPairConfigs(): List<PairInfoResponse> = emptyList()

        override suspend fun getFeeConfigs(): List<co.nilin.opex.api.core.inout.PairFeeResponse> = emptyList()

        override suspend fun getFeeConfig(symbol: String): co.nilin.opex.api.core.inout.PairFeeResponse =
            co.nilin.opex.api.core.inout.PairFeeResponse(
                pair = symbol,
                direction = "*",
                userLevel = "*",
                makerFee = BigDecimal.ZERO,
                takerFee = BigDecimal.ZERO
            )
    }

    private class RecordingBlockchainGatewayProxy(
        private val depositDetails: List<DepositDetails> = emptyList()
    ) : BlockchainGatewayProxy {
        var assignAddressCallCount = 0

        override suspend fun assignAddress(uuid: String, currency: String, chain: String): AssignResponse? {
            assignAddressCallCount += 1
            return AssignResponse(
                listOf(
                    AssignedAddress(
                        uuid = uuid,
                        address = "0x1",
                        memo = null,
                        type = AddressType(1, "address", ".*", null),
                        chains = mutableListOf()
                    )
                )
            )
        }

        override suspend fun getDepositDetails(refs: List<String>): List<DepositDetails> = depositDetails

        override suspend fun getCurrencyImplementations(currency: String?): List<CurrencyImplementation> = emptyList()
    }
}
