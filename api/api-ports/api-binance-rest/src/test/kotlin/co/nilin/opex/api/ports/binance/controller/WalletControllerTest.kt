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
    fun givenExpiredTimestamp_whenDepositHistoryRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val blockchainGatewayProxy = RecordingBlockchainGatewayProxy()
        val controller = controller(walletProxy = walletProxy, blockchainGatewayProxy = blockchainGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.getDepositTransactions(
                    coin = "USDT",
                    status = null,
                    startTime = null,
                    endTime = null,
                    offset = null,
                    limit = null,
                    recvWindow = null,
                    timestamp = signedTimestamp() - 6000,
                    ascendingByTime = null,
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getDepositTransactionsCallCount).isZero()
        assertThat(blockchainGatewayProxy.getDepositDetailsCallCount).isZero()
    }

    @Test
    fun givenExpiredTimestamp_whenWithdrawHistoryRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
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
                    recvWindow = null,
                    timestamp = signedTimestamp() - 6000,
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
    fun givenInvalidStatus_whenDepositHistoryRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking {
                controller.getDepositTransactions(
                    coin = "USDT",
                    status = 2,
                    startTime = null,
                    endTime = null,
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
    fun givenInvalidStatus_whenWithdrawHistoryRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking {
                controller.getWithdrawTransactions(
                    coin = "USDT",
                    withdrawOrderId = null,
                    withdrawStatus = 3,
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
    fun givenInvalidStatus_whenWithdrawHistoryV2Requested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)
        val request = WithDrawRequest(
            coin = "USDT",
            withdrawOrderId = null,
            withdrawStatus = -2,
            offset = null,
            limit = null,
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
                withdrawHistory(withdrawId = 11, status = "REJECTED"),
                withdrawHistory(withdrawId = 12, status = "CANCELED")
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
    fun givenCanceledStatus_whenWithdrawHistoryRequested_thenFiltersReturnedRows(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            withdraws = listOf(
                withdrawHistory(withdrawId = 10, status = "DONE"),
                withdrawHistory(withdrawId = 11, status = "CANCELED")
            )
        )
        val controller = controller(walletProxy = walletProxy)

        val response = controller.getWithdrawTransactions(
            coin = "USDT",
            withdrawOrderId = null,
            withdrawStatus = -1,
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
        assertThat(response[0].id).isEqualTo("11")
        assertThat(response[0].status).isEqualTo(-1)
    }

    @Test
    fun givenNoCoin_whenWithdrawHistoryRequested_thenPassesNullCoinToWalletProxy(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        controller.getWithdrawTransactions(
            coin = null,
            withdrawOrderId = null,
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

        assertThat(walletProxy.getWithdrawTransactionsCallCount).isEqualTo(1)
        assertThat(walletProxy.lastWithdrawCoin).isNull()
    }

    @Test
    fun givenStatus_whenWithdrawHistoryV2Requested_thenFiltersReturnedRows(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            withdraws = listOf(
                withdrawHistory(withdrawId = 20, status = "CREATED"),
                withdrawHistory(withdrawId = 21, status = "DONE"),
                withdrawHistory(withdrawId = 22, status = "CANCELED")
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
    fun givenCanceledStatus_whenWithdrawHistoryV2Requested_thenFiltersReturnedRows(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            withdraws = listOf(
                withdrawHistory(withdrawId = 20, status = "DONE"),
                withdrawHistory(withdrawId = 21, status = "CANCELED")
            )
        )
        val controller = controller(walletProxy = walletProxy)
        val request = WithDrawRequest(
            coin = "USDT",
            withdrawOrderId = null,
            withdrawStatus = -1,
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
        assertThat(response[0].status).isEqualTo(-1)
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

    @Test
    fun givenBlankSymbol_whenUserAssetsRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking { controller.getUserAssets(securityContext(), " ", null, null, null, signedTimestamp()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWalletCallCount).isZero()
        assertThat(walletProxy.getWalletsCallCount).isZero()
    }

    @Test
    fun givenBlankQuoteAsset_whenUserAssetsRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking { controller.getUserAssets(securityContext(), null, " ", null, null, signedTimestamp()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWalletsCallCount).isZero()
    }

    @Test
    fun givenBlankQuoteAsset_whenEstimatedValueRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking { controller.assetsEstimatedValue(securityContext(), " ", null, signedTimestamp()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWalletsCallCount).isZero()
    }

    @Test
    fun givenExpiredTimestamp_whenUserAssetsRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking {
                controller.getUserAssets(securityContext(), null, "USDT", true, null, signedTimestamp() - 6000)
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWalletCallCount).isZero()
        assertThat(walletProxy.getWalletsCallCount).isZero()
    }

    @Test
    fun givenExpiredTimestamp_whenEstimatedValueRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking { controller.assetsEstimatedValue(securityContext(), "USDT", null, signedTimestamp() - 6000) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWalletsCallCount).isZero()
    }

    @Test
    fun givenSymbol_whenTradeFeeRequested_thenReturnMappedAccountantFee(): Unit = runBlocking {
        val accountantProxy = RecordingAccountantProxy(
            feeConfig = co.nilin.opex.api.core.inout.PairFeeResponse(
                pair = "ETH_USDT",
                direction = "*",
                userLevel = "*",
                makerFee = BigDecimal("0.001"),
                takerFee = BigDecimal("0.002")
            )
        )
        val controller = controller(accountantProxy = accountantProxy)

        val fees = controller.getPairFees("ETH_USDT", null, signedTimestamp())

        assertThat(fees).hasSize(1)
        assertThat(fees[0].symbol).isEqualTo("ETH_USDT")
        assertThat(fees[0].makerCommission).isEqualTo(0.001)
        assertThat(fees[0].takerCommission).isEqualTo(0.002)
        assertThat(accountantProxy.getFeeConfigSymbol).isEqualTo("ETH_USDT")
        assertThat(accountantProxy.getFeeConfigsCallCount).isZero()
    }

    @Test
    fun givenNoSymbol_whenTradeFeeRequested_thenReturnDistinctMappedAccountantFees(): Unit = runBlocking {
        val accountantProxy = RecordingAccountantProxy(
            feeConfigs = listOf(
                co.nilin.opex.api.core.inout.PairFeeResponse(
                    pair = "ETH_USDT",
                    direction = "BID",
                    userLevel = "*",
                    makerFee = BigDecimal("0.001"),
                    takerFee = BigDecimal("0.002")
                ),
                co.nilin.opex.api.core.inout.PairFeeResponse(
                    pair = "ETH_USDT",
                    direction = "ASK",
                    userLevel = "*",
                    makerFee = BigDecimal("0.001"),
                    takerFee = BigDecimal("0.002")
                ),
                co.nilin.opex.api.core.inout.PairFeeResponse(
                    pair = "BTC_USDT",
                    direction = "*",
                    userLevel = "*",
                    makerFee = BigDecimal("0.003"),
                    takerFee = BigDecimal("0.004")
                )
            )
        )
        val controller = controller(accountantProxy = accountantProxy)

        val fees = controller.getPairFees(null, null, signedTimestamp())

        assertThat(fees.map { it.symbol }).containsExactly("ETH_USDT", "BTC_USDT")
        assertThat(fees.map { it.makerCommission }).containsExactly(0.001, 0.003)
        assertThat(fees.map { it.takerCommission }).containsExactly(0.002, 0.004)
        assertThat(accountantProxy.getFeeConfigsCallCount).isEqualTo(1)
        assertThat(accountantProxy.getFeeConfigSymbol).isNull()
    }

    @Test
    fun givenExpiredTimestamp_whenTradeFeeRequested_thenRejectBeforeAccountantCall(): Unit = runBlocking {
        val accountantProxy = RecordingAccountantProxy()
        val controller = controller(accountantProxy = accountantProxy)

        assertThatThrownBy {
            runBlocking { controller.getPairFees("ETH_USDT", null, signedTimestamp() - 6000) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(accountantProxy.getFeeConfigSymbol).isNull()
        assertThat(accountantProxy.getFeeConfigsCallCount).isZero()
    }

    @Test
    fun givenLowercaseAsset_whenUserAssetsEvaluated_thenMatchesUppercaseBestPrice(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            wallets = listOf(Wallet("eth", BigDecimal("2"), BigDecimal("0.5"), BigDecimal("0.25")))
        )
        val marketDataProxy = RecordingMarketDataProxy(
            bestPrices = listOf(BestPrice("ETH_USDT", BigDecimal("100"), BigDecimal("101")))
        )
        val controller = controller(walletProxy = walletProxy, marketDataProxy = marketDataProxy)

        val assets = controller.getUserAssets(securityContext(), null, "USDT", true, null, signedTimestamp())

        assertThat(assets).hasSize(1)
        assertThat(assets[0].valuation).isEqualByComparingTo("100")
        assertThat(assets[0].free).isEqualByComparingTo("200")
        assertThat(assets[0].locked).isEqualByComparingTo("50")
        assertThat(assets[0].withdrawing).isEqualByComparingTo("25")
    }

    @Test
    fun givenNoBestBidAndLastPrice_whenUserAssetsEvaluated_thenUsesLastPrice(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            wallets = listOf(Wallet("ETH", BigDecimal("2"), BigDecimal("0.5"), BigDecimal("0.25")))
        )
        val marketDataProxy = RecordingMarketDataProxy(
            bestPrices = listOf(BestPrice("ETH_USDT", null, null)),
            lastPrices = listOf(PriceTicker("ETH_USDT", "100"))
        )
        val controller = controller(walletProxy = walletProxy, marketDataProxy = marketDataProxy)

        val assets = controller.getUserAssets(securityContext(), null, "USDT", true, null, signedTimestamp())

        assertThat(assets).hasSize(1)
        assertThat(assets[0].valuation).isEqualByComparingTo("100")
        assertThat(assets[0].free).isEqualByComparingTo("200")
        assertThat(assets[0].locked).isEqualByComparingTo("50")
        assertThat(assets[0].withdrawing).isEqualByComparingTo("25")
    }

    @Test
    fun givenOnlyQuoteAsset_whenUserAssetsEvaluated_thenDoesNotQueryMarketBestPrices(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            wallets = listOf(Wallet("USDT", BigDecimal("9.4"), BigDecimal("30.6"), BigDecimal.ZERO))
        )
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(walletProxy = walletProxy, marketDataProxy = marketDataProxy)

        val assets = controller.getUserAssets(securityContext(), null, "USDT", true, null, signedTimestamp())

        assertThat(assets).hasSize(1)
        assertThat(assets[0].asset).isEqualTo("USDT")
        assertThat(assets[0].valuation).isEqualByComparingTo("1")
        assertThat(assets[0].free).isEqualByComparingTo("9.4")
        assertThat(assets[0].locked).isEqualByComparingTo("30.6")
        assertThat(assets[0].withdrawing).isEqualByComparingTo("0")
        assertThat(marketDataProxy.getBestPriceForSymbolsCallCount).isZero()
    }

    @Test
    fun givenLowercaseAsset_whenEstimatedValueRequested_thenMatchesUppercaseBestPrice(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            wallets = listOf(
                Wallet("eth", BigDecimal("2"), BigDecimal("0.5"), BigDecimal("0.25")),
                Wallet("usdt", BigDecimal("10"), BigDecimal("1"), BigDecimal("2"))
            )
        )
        val marketDataProxy = RecordingMarketDataProxy(
            bestPrices = listOf(BestPrice("ETH_USDT", BigDecimal("100"), BigDecimal("101")))
        )
        val controller = controller(walletProxy = walletProxy, marketDataProxy = marketDataProxy)

        val estimatedValue = controller.assetsEstimatedValue(securityContext(), "USDT", null, signedTimestamp())

        assertThat(estimatedValue.value).isEqualByComparingTo("288")
        assertThat(estimatedValue.zeroValueAssets).isEmpty()
    }

    @Test
    fun givenOnlyQuoteAsset_whenEstimatedValueRequested_thenDoesNotQueryMarketBestPrices(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            wallets = listOf(Wallet("USDT", BigDecimal("9.4"), BigDecimal("30.6"), BigDecimal.ZERO))
        )
        val marketDataProxy = RecordingMarketDataProxy()
        val controller = controller(walletProxy = walletProxy, marketDataProxy = marketDataProxy)

        val estimatedValue = controller.assetsEstimatedValue(securityContext(), "USDT", null, signedTimestamp())

        assertThat(estimatedValue.value).isEqualByComparingTo("40")
        assertThat(estimatedValue.evaluatedWith).isEqualTo("USDT")
        assertThat(estimatedValue.zeroValueAssets).isEmpty()
        assertThat(marketDataProxy.getBestPriceForSymbolsCallCount).isZero()
    }

    @Test
    fun givenNoBestBidAndLastPrice_whenEstimatedValueRequested_thenUsesLastPrice(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy(
            wallets = listOf(
                Wallet("ETH", BigDecimal("2"), BigDecimal("0.5"), BigDecimal("0.25")),
                Wallet("USDT", BigDecimal("10"), BigDecimal("1"), BigDecimal("2"))
            )
        )
        val marketDataProxy = RecordingMarketDataProxy(
            bestPrices = listOf(BestPrice("ETH_USDT", BigDecimal.ZERO, null)),
            lastPrices = listOf(PriceTicker("ETH_USDT", "100"))
        )
        val controller = controller(walletProxy = walletProxy, marketDataProxy = marketDataProxy)

        val estimatedValue = controller.assetsEstimatedValue(securityContext(), "USDT", null, signedTimestamp())

        assertThat(estimatedValue.value).isEqualByComparingTo("288")
        assertThat(estimatedValue.zeroValueAssets).isEmpty()
    }

    private fun controller(
        walletProxy: RecordingWalletProxy = RecordingWalletProxy(),
        blockchainGatewayProxy: RecordingBlockchainGatewayProxy = RecordingBlockchainGatewayProxy(),
        marketDataProxy: RecordingMarketDataProxy = RecordingMarketDataProxy(),
        accountantProxy: RecordingAccountantProxy = RecordingAccountantProxy()
    ) = WalletController(
        walletProxy,
        RecordingSymbolMapper(),
        marketDataProxy,
        accountantProxy,
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
        createDate = LocalDateTime.of(2026, 1, 1, 0, 0, 1),
        acceptDate = LocalDateTime.of(2026, 1, 1, 0, 0, 2)
    )

    private class RecordingWalletProxy(
        private val deposits: List<TransactionHistoryResponse> = emptyList(),
        private val withdraws: List<WithdrawHistoryResponse> = emptyList(),
        private val wallets: List<Wallet> = emptyList()
    ) : WalletProxy {
        var getDepositTransactionsCallCount = 0
        var getWithdrawTransactionsCallCount = 0
        var getWalletCallCount = 0
        var getWalletsCallCount = 0
        var lastWithdrawCoin: String? = null

        override suspend fun getWallets(uuid: String?, token: String?): List<Wallet> {
            getWalletsCallCount += 1
            return wallets
        }

        override suspend fun getWallet(uuid: String?, token: String?, symbol: String): Wallet {
            getWalletCallCount += 1
            return Wallet(symbol, BigDecimal.ZERO, BigDecimal.ZERO, BigDecimal.ZERO)
        }

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
            lastWithdrawCoin = coin
            return withdraws
        }
    }

    private class RecordingSymbolMapper : SymbolMapper {
        override suspend fun fromInternalSymbol(symbol: String?): String? = symbol

        override suspend fun toInternalSymbol(alias: String?): String? = alias

        override suspend fun symbolToAliasMap(): Map<String, String> = emptyMap()
    }

    private class RecordingMarketDataProxy(
        private val bestPrices: List<BestPrice> = emptyList(),
        private val lastPrices: List<PriceTicker> = emptyList()
    ) : MarketDataProxy {
        var getBestPriceForSymbolsCallCount = 0

        override suspend fun getTradeTickerData(interval: Interval): List<PriceChange> = emptyList()

        override suspend fun getTradeTickerDataBySymbol(symbol: String, interval: Interval): PriceChange =
            throw UnsupportedOperationException()

        override suspend fun openBidOrders(symbol: String, limit: Int): List<OrderBook> = emptyList()

        override suspend fun openAskOrders(symbol: String, limit: Int): List<OrderBook> = emptyList()

        override suspend fun lastOrder(symbol: String): Order? = null

        override suspend fun recentTrades(symbol: String, limit: Int): List<MarketTrade> = emptyList()

        override suspend fun lastPrice(symbol: String?): List<PriceTicker> =
            lastPrices.filter { symbol == null || it.symbol.equals(symbol, true) }

        override suspend fun getBestPriceForSymbols(symbols: List<String>): List<BestPrice> {
            getBestPriceForSymbolsCallCount += 1
            return bestPrices
        }

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

    private class RecordingAccountantProxy(
        private val feeConfigs: List<co.nilin.opex.api.core.inout.PairFeeResponse> = emptyList(),
        private val feeConfig: co.nilin.opex.api.core.inout.PairFeeResponse =
            co.nilin.opex.api.core.inout.PairFeeResponse(
                pair = "ETH_USDT",
                direction = "*",
                userLevel = "*",
                makerFee = BigDecimal.ZERO,
                takerFee = BigDecimal.ZERO
            )
    ) : AccountantProxy {
        var getFeeConfigsCallCount = 0
        var getFeeConfigSymbol: String? = null

        override suspend fun getPairConfigs(): List<PairInfoResponse> = emptyList()

        override suspend fun getFeeConfigs(): List<co.nilin.opex.api.core.inout.PairFeeResponse> {
            getFeeConfigsCallCount += 1
            return feeConfigs
        }

        override suspend fun getFeeConfig(symbol: String): co.nilin.opex.api.core.inout.PairFeeResponse {
            getFeeConfigSymbol = symbol
            return feeConfig
        }
    }

    private class RecordingBlockchainGatewayProxy(
        private val depositDetails: List<DepositDetails> = emptyList()
    ) : BlockchainGatewayProxy {
        var assignAddressCallCount = 0
        var getDepositDetailsCallCount = 0

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

        override suspend fun getDepositDetails(refs: List<String>): List<DepositDetails> {
            getDepositDetailsCallCount += 1
            return depositDetails
        }

        override suspend fun getCurrencyImplementations(currency: String?): List<CurrencyImplementation> = emptyList()
    }
}
