package co.nilin.opex.api.ports.binance.controller

import co.nilin.opex.api.core.inout.*
import co.nilin.opex.api.core.spi.AccountantProxy
import co.nilin.opex.api.core.spi.MarketUserDataProxy
import co.nilin.opex.api.core.spi.MatchingGatewayProxy
import co.nilin.opex.api.core.spi.SymbolMapper
import co.nilin.opex.api.core.spi.WalletProxy
import co.nilin.opex.common.OpexError
import co.nilin.opex.utility.error.data.OpexException
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.Test
import org.springframework.http.HttpHeaders
import org.springframework.security.core.context.SecurityContext
import org.springframework.security.core.context.SecurityContextImpl
import org.springframework.security.oauth2.jwt.Jwt
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.reactive.function.client.WebClientResponseException
import java.math.BigDecimal
import java.security.Principal
import java.time.LocalDateTime
import java.util.*

private class AccountControllerTest {

    @Test
    fun givenBinanceAccountGetRoutes_whenMapped_thenDoNotRequireRequestContentType() {
        val getMethods = listOf(
            "queryOrder",
            "fetchOpenOrders",
            "fetchAllOrders",
            "fetchAllTrades",
            "accountInfo"
        )

        getMethods.forEach { methodName ->
            val mapping = AccountController::class.java.declaredMethods
                .single { it.name == methodName }
                .getAnnotation(GetMapping::class.java)

            assertThat(mapping.consumes).isEmpty()
        }
    }

    @Test
    fun givenNoSymbol_whenOpenOrdersRequested_thenQueryAllOpenOrdersAndReturnAliasSymbols(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)
        val principal = Principal { "user-1" }

        val responses = controller.fetchOpenOrders(principal, null, null, signedTimestamp(), 25)

        assertThat(queryHandler.openOrdersSymbol).isNull()
        assertThat(queryHandler.openOrdersLimit).isEqualTo(25)
        assertThat(responses).hasSize(1)
        assertThat(responses.first().symbol).isEqualTo("ETHUSDT")
    }

    @Test
    fun givenInvalidLimit_whenOpenOrdersRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking { controller.fetchOpenOrders(Principal { "user-1" }, "ETHUSDT", null, signedTimestamp(), 0) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.openOrdersSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenBlankSymbol_whenOpenOrdersRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking { controller.fetchOpenOrders(Principal { "user-1" }, " ", null, signedTimestamp(), null) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.openOrdersSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenNoSymbol_whenAllOrdersRequested_thenQueryAllOrdersAndReturnAliasSymbols(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)
        val principal = Principal { "user-1" }

        val responses = controller.fetchAllOrders(principal, null, null, null, 50, null, signedTimestamp())

        assertThat(queryHandler.allOrdersSymbol).isNull()
        assertThat(queryHandler.allOrdersLimit).isEqualTo(50)
        assertThat(responses).hasSize(1)
        assertThat(responses.first().symbol).isEqualTo("ETHUSDT")
    }

    @Test
    fun givenNoLimit_whenAllOrdersRequested_thenUseBinanceDefaultLimit(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        controller.fetchAllOrders(Principal { "user-1" }, "ETHUSDT", null, null, null, null, signedTimestamp())

        assertThat(queryHandler.allOrdersSymbol).isEqualTo("ETH_USDT")
        assertThat(queryHandler.allOrdersLimit).isEqualTo(500)
    }

    @Test
    fun givenTooLargeLimit_whenAllOrdersRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking { controller.fetchAllOrders(Principal { "user-1" }, "ETHUSDT", null, null, 1001, null, signedTimestamp()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.allOrdersSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenBlankSymbol_whenAllOrdersRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking { controller.fetchAllOrders(Principal { "user-1" }, " ", null, null, null, null, signedTimestamp()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.allOrdersSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenInvertedTimeRange_whenAllOrdersRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking {
                controller.fetchAllOrders(
                    Principal { "user-1" },
                    "ETHUSDT",
                    Date(2000),
                    Date(1000),
                    null,
                    null,
                    signedTimestamp()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.allOrdersSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenInvalidLimit_whenMyTradesRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking { controller.fetchAllTrades(Principal { "user-1" }, "ETHUSDT", null, null, null, 0, null, signedTimestamp()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.allTradesSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenNegativeFromId_whenMyTradesRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking {
                controller.fetchAllTrades(
                    Principal { "user-1" },
                    "ETHUSDT",
                    null,
                    null,
                    -1,
                    null,
                    null,
                    signedTimestamp()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.allTradesSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenInvalidOrderId_whenMyTradesRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking {
                controller.fetchAllTrades(
                    Principal { "user-1" },
                    "ETHUSDT",
                    null,
                    null,
                    null,
                    null,
                    null,
                    signedTimestamp(),
                    orderId = 0
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.allTradesSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenBlankSymbol_whenMyTradesRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking {
                controller.fetchAllTrades(
                    Principal { "user-1" },
                    " ",
                    null,
                    null,
                    null,
                    null,
                    null,
                    signedTimestamp()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.allTradesSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenNegativeStartTime_whenMyTradesRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking {
                controller.fetchAllTrades(
                    Principal { "user-1" },
                    "ETHUSDT",
                    Date(-1),
                    null,
                    null,
                    null,
                    null,
                    signedTimestamp()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.allTradesSymbol).isEqualTo("not-called")
    }

    @Test
    fun givenNoLimit_whenMyTradesRequested_thenUseBinanceDefaultLimit(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        controller.fetchAllTrades(Principal { "user-1" }, "ETHUSDT", null, null, null, null, null, signedTimestamp())

        assertThat(queryHandler.allTradesSymbol).isEqualTo("ETH_USDT")
        assertThat(queryHandler.allTradesLimit).isEqualTo(500)
    }

    @Test
    fun givenOrderId_whenMyTradesRequested_thenPassOrderIdToProxy(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        controller.fetchAllTrades(
            Principal { "user-1" },
            "ETHUSDT",
            null,
            null,
            null,
            null,
            null,
            signedTimestamp(),
            orderId = 123
        )

        assertThat(queryHandler.allTradesSymbol).isEqualTo("ETH_USDT")
        assertThat(queryHandler.allTradesOrderId).isEqualTo(123)
    }

    @Test
    fun givenNoOrderIdentifier_whenQueryOrderRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking { controller.queryOrder(Principal { "user-1" }, "ETHUSDT", null, null, null, signedTimestamp()) }
        }.isOpexError(OpexError.BadRequest)

        assertThat(queryHandler.queryOrderCallCount).isZero()
    }

    @Test
    fun givenExpiredTimestamp_whenAccountInfoRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking { controller.accountInfo(securityContext(), null, signedTimestamp() - 6000) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWalletsCallCount).isZero()
    }

    @Test
    fun givenFeeConfigs_whenAccountInfoRequested_thenReturnBinanceCommissions(): Unit = runBlocking {
        val accountantProxy = RecordingAccountantProxy(
            fees = listOf(
                PairFeeResponse("ETH_USDT", "BID", "*", BigDecimal("0.001"), BigDecimal("0.002")),
                PairFeeResponse("BTC_USDT", "BID", "*", BigDecimal("0.0005"), BigDecimal("0.0015"))
            )
        )
        val controller = controller(accountantProxy = accountantProxy)

        val response = controller.accountInfo(securityContext(), null, signedTimestamp())

        assertThat(response.makerCommission).isEqualTo(10)
        assertThat(response.takerCommission).isEqualTo(20)
        assertThat(response.buyerCommission).isZero()
        assertThat(response.sellerCommission).isZero()
    }

    @Test
    fun givenTooLargeRecvWindow_whenAccountInfoRequested_thenRejectBeforeWalletCall(): Unit = runBlocking {
        val walletProxy = RecordingWalletProxy()
        val controller = controller(walletProxy = walletProxy)

        assertThatThrownBy {
            runBlocking { controller.accountInfo(securityContext(), 60001, signedTimestamp()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(walletProxy.getWalletsCallCount).isZero()
    }

    @Test
    fun givenClientOrderId_whenCancelOrderRequested_thenCancelActualOrderWithAuthenticatedUser(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler, matchingGatewayProxy)

        val response = controller.cancelOrder(
            principal = Principal { "principal-user" },
            symbol = "ETHUSDT",
            orderId = null,
            origClientOrderId = "client-1",
            newClientOrderId = null,
            recvWindow = null,
            timestamp = signedTimestamp(),
            securityContext = securityContext()
        )

        assertThat(queryHandler.queryOrderSymbol).isEqualTo("ETH_USDT")
        assertThat(queryHandler.queryOrderId).isNull()
        assertThat(queryHandler.queryOrigClientOrderId).isEqualTo("client-1")
        assertThat(response.orderId).isEqualTo(100)
        assertThat(response.origClientOrderId).isEqualTo("client-1")
        assertThat(response.clientOrderId).isEqualTo("client-1")
        assertThat(matchingGatewayProxy.cancelOrderCallCount).isEqualTo(1)
        assertThat(matchingGatewayProxy.cancelOrderUuid).isEqualTo("user-1")
        assertThat(matchingGatewayProxy.cancelOrderToken).isEqualTo("token-1")
    }

    @Test
    fun givenBlankSymbol_whenCancelOrderRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler, matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.cancelOrder(
                    principal = Principal { "user-1" },
                    symbol = " ",
                    orderId = 100,
                    origClientOrderId = null,
                    newClientOrderId = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.queryOrderCallCount).isZero()
        assertThat(matchingGatewayProxy.cancelOrderCallCount).isZero()
    }

    @Test
    fun givenInvalidOrderId_whenCancelOrderRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler, matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.cancelOrder(
                    principal = Principal { "user-1" },
                    symbol = "ETHUSDT",
                    orderId = 0,
                    origClientOrderId = null,
                    newClientOrderId = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.queryOrderCallCount).isZero()
        assertThat(matchingGatewayProxy.cancelOrderCallCount).isZero()
    }

    @Test
    fun givenBlankOrigClientOrderId_whenCancelOrderRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler, matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.cancelOrder(
                    principal = Principal { "user-1" },
                    symbol = "ETHUSDT",
                    orderId = null,
                    origClientOrderId = " ",
                    newClientOrderId = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.queryOrderCallCount).isZero()
        assertThat(matchingGatewayProxy.cancelOrderCallCount).isZero()
    }

    @Test
    fun givenNewClientOrderId_whenCancelOrderRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler, matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.cancelOrder(
                    principal = Principal { "user-1" },
                    symbol = "ETHUSDT",
                    orderId = 100,
                    origClientOrderId = null,
                    newClientOrderId = "cancel-1",
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.queryOrderCallCount).isZero()
        assertThat(matchingGatewayProxy.cancelOrderCallCount).isZero()
    }

    @Test
    fun givenBlankSymbol_whenQueryOrderRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val controller = controller(queryHandler)

        assertThatThrownBy {
            runBlocking { controller.queryOrder(Principal { "user-1" }, " ", 100, null, null, signedTimestamp()) }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(queryHandler.queryOrderCallCount).isZero()
    }

    @Test
    fun givenUnsupportedOrderType_whenCreateOrderRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.createNewOrder(
                    symbol = "ETHUSDT",
                    side = OrderSide.BUY,
                    type = OrderType.STOP_LOSS,
                    timeInForce = null,
                    quantity = BigDecimal("0.5"),
                    quoteOrderQty = null,
                    price = null,
                    newClientOrderId = null,
                    stopPrice = BigDecimal("90"),
                    icebergQty = null,
                    newOrderRespType = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenBlankSymbol_whenCreateOrderRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.createNewOrder(
                    symbol = " ",
                    side = OrderSide.BUY,
                    type = OrderType.LIMIT,
                    timeInForce = TimeInForce.GTC,
                    quantity = BigDecimal("0.5"),
                    quoteOrderQty = null,
                    price = BigDecimal("100"),
                    newClientOrderId = null,
                    stopPrice = null,
                    icebergQty = null,
                    newOrderRespType = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenQuoteOrderQuantityMarketOrder_whenCreateOrderRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.createNewOrder(
                    symbol = "ETHUSDT",
                    side = OrderSide.BUY,
                    type = OrderType.MARKET,
                    timeInForce = null,
                    quantity = null,
                    quoteOrderQty = BigDecimal("100"),
                    price = null,
                    newClientOrderId = null,
                    stopPrice = null,
                    icebergQty = null,
                    newOrderRespType = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenTimeInForceMarketOrder_whenCreateOrderRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.createNewOrder(
                    symbol = "ETHUSDT",
                    side = OrderSide.BUY,
                    type = OrderType.MARKET,
                    timeInForce = TimeInForce.GTC,
                    quantity = BigDecimal("0.5"),
                    quoteOrderQty = null,
                    price = null,
                    newClientOrderId = null,
                    stopPrice = null,
                    icebergQty = null,
                    newOrderRespType = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenStopPriceLimitOrder_whenCreateOrderRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.createNewOrder(
                    symbol = "ETHUSDT",
                    side = OrderSide.BUY,
                    type = OrderType.LIMIT,
                    timeInForce = TimeInForce.GTC,
                    quantity = BigDecimal("0.5"),
                    quoteOrderQty = null,
                    price = BigDecimal("100"),
                    newClientOrderId = null,
                    stopPrice = BigDecimal("90"),
                    icebergQty = null,
                    newOrderRespType = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenClientOrderId_whenCreateOrderRequested_thenSubmitExpectedMatchingOrder(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy().apply {
            queryOrderResponse = null
        }
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler = queryHandler, matchingGatewayProxy = matchingGatewayProxy)

        val response = controller.createNewOrder(
            symbol = "ETHUSDT",
            side = OrderSide.BUY,
            type = OrderType.LIMIT,
            timeInForce = TimeInForce.GTC,
            quantity = BigDecimal("0.5"),
            quoteOrderQty = null,
            price = BigDecimal("100"),
            newClientOrderId = "client-1",
            stopPrice = null,
            icebergQty = null,
            newOrderRespType = null,
            recvWindow = null,
            timestamp = signedTimestamp(),
            securityContext = securityContext()
        )

        assertThat(response.clientOrderId).isEqualTo("client-1")
        assertThat(queryHandler.queryOrderCallCount).isEqualTo(1)
        assertThat(queryHandler.queryOrigClientOrderId).isEqualTo("client-1")
        assertThat(matchingGatewayProxy.createOrderCallCount).isEqualTo(1)
        assertThat(matchingGatewayProxy.createOrderClientOrderId).isEqualTo("client-1")
    }

    @Test
    fun givenNoClientOrderId_whenCreateOrderRequested_thenGenerateAndSubmitClientOrderId(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler = queryHandler, matchingGatewayProxy = matchingGatewayProxy)

        val response = controller.createNewOrder(
            symbol = "ETHUSDT",
            side = OrderSide.BUY,
            type = OrderType.LIMIT,
            timeInForce = TimeInForce.GTC,
            quantity = BigDecimal("0.5"),
            quoteOrderQty = null,
            price = BigDecimal("100"),
            newClientOrderId = null,
            stopPrice = null,
            icebergQty = null,
            newOrderRespType = null,
            recvWindow = null,
            timestamp = signedTimestamp(),
            securityContext = securityContext()
        )

        assertThat(response.clientOrderId).isNotBlank()
        assertThat(response.clientOrderId).hasSizeLessThanOrEqualTo(72)
        assertThat(response.clientOrderId).startsWith("x-")
        assertThat(queryHandler.queryOrderCallCount).isZero()
        assertThat(matchingGatewayProxy.createOrderCallCount).isEqualTo(1)
        assertThat(matchingGatewayProxy.createOrderClientOrderId).isEqualTo(response.clientOrderId)
    }

    @Test
    fun givenOpenClientOrderId_whenCreateOrderRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler = queryHandler, matchingGatewayProxy = matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.createNewOrder(
                    symbol = "ETHUSDT",
                    side = OrderSide.BUY,
                    type = OrderType.LIMIT,
                    timeInForce = TimeInForce.GTC,
                    quantity = BigDecimal("0.5"),
                    quoteOrderQty = null,
                    price = BigDecimal("100"),
                    newClientOrderId = "client-1",
                    stopPrice = null,
                    icebergQty = null,
                    newOrderRespType = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.BadRequest)

        assertThat(queryHandler.queryOrderCallCount).isEqualTo(1)
        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenClosedClientOrderId_whenCreateOrderRequested_thenSubmitOrder(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy().apply {
            queryOrderResponse = queryOrderResponse!!.copy(status = OrderStatus.CANCELED)
        }
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler = queryHandler, matchingGatewayProxy = matchingGatewayProxy)

        val response = controller.createNewOrder(
            symbol = "ETHUSDT",
            side = OrderSide.BUY,
            type = OrderType.LIMIT,
            timeInForce = TimeInForce.GTC,
            quantity = BigDecimal("0.5"),
            quoteOrderQty = null,
            price = BigDecimal("100"),
            newClientOrderId = "client-1",
            stopPrice = null,
            icebergQty = null,
            newOrderRespType = null,
            recvWindow = null,
            timestamp = signedTimestamp(),
            securityContext = securityContext()
        )

        assertThat(response.clientOrderId).isEqualTo("client-1")
        assertThat(queryHandler.queryOrderCallCount).isEqualTo(1)
        assertThat(matchingGatewayProxy.createOrderCallCount).isEqualTo(1)
        assertThat(matchingGatewayProxy.createOrderClientOrderId).isEqualTo("client-1")
    }

    @Test
    fun givenMissingClientOrderIdLookup_whenCreateOrderRequested_thenSubmitOrder(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy().apply {
            queryOrderFailure = WebClientResponseException.create(
                404,
                "Not Found",
                HttpHeaders.EMPTY,
                ByteArray(0),
                null
            )
        }
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler = queryHandler, matchingGatewayProxy = matchingGatewayProxy)

        controller.createNewOrder(
            symbol = "ETHUSDT",
            side = OrderSide.BUY,
            type = OrderType.LIMIT,
            timeInForce = TimeInForce.GTC,
            quantity = BigDecimal("0.5"),
            quoteOrderQty = null,
            price = BigDecimal("100"),
            newClientOrderId = "client-missing",
            stopPrice = null,
            icebergQty = null,
            newOrderRespType = null,
            recvWindow = null,
            timestamp = signedTimestamp(),
            securityContext = securityContext()
        )

        assertThat(queryHandler.queryOrderCallCount).isEqualTo(1)
        assertThat(matchingGatewayProxy.createOrderCallCount).isEqualTo(1)
        assertThat(matchingGatewayProxy.createOrderClientOrderId).isEqualTo("client-missing")
    }

    @Test
    fun givenInvalidClientOrderId_whenCreateOrderRequested_thenRejectBeforeProxyCall(): Unit = runBlocking {
        val queryHandler = RecordingMarketUserDataProxy()
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(queryHandler = queryHandler, matchingGatewayProxy = matchingGatewayProxy)
        val tooLongClientOrderId = "x".repeat(73)

        listOf(" ", tooLongClientOrderId).forEach { clientOrderId ->
            assertThatThrownBy {
                runBlocking {
                    controller.createNewOrder(
                        symbol = "ETHUSDT",
                        side = OrderSide.BUY,
                        type = OrderType.LIMIT,
                        timeInForce = TimeInForce.GTC,
                        quantity = BigDecimal("0.5"),
                        quoteOrderQty = null,
                        price = BigDecimal("100"),
                        newClientOrderId = clientOrderId,
                        stopPrice = null,
                        icebergQty = null,
                        newOrderRespType = null,
                        recvWindow = null,
                        timestamp = signedTimestamp(),
                        securityContext = securityContext()
                    )
                }
            }.isOpexError(OpexError.InvalidRequestParam)
        }

        assertThat(queryHandler.queryOrderCallCount).isZero()
        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenIcebergQuantity_whenCreateOrderRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.createNewOrder(
                    symbol = "ETHUSDT",
                    side = OrderSide.SELL,
                    type = OrderType.LIMIT,
                    timeInForce = TimeInForce.GTC,
                    quantity = BigDecimal("0.5"),
                    quoteOrderQty = null,
                    price = BigDecimal("100"),
                    newClientOrderId = null,
                    stopPrice = null,
                    icebergQty = BigDecimal("0.1"),
                    newOrderRespType = null,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenResponseType_whenCreateOrderRequested_thenRejectBeforeGatewayCall(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        assertThatThrownBy {
            runBlocking {
                controller.createNewOrder(
                    symbol = "ETHUSDT",
                    side = OrderSide.SELL,
                    type = OrderType.LIMIT,
                    timeInForce = TimeInForce.GTC,
                    quantity = BigDecimal("0.5"),
                    quoteOrderQty = null,
                    price = BigDecimal("100"),
                    newClientOrderId = null,
                    stopPrice = null,
                    icebergQty = null,
                    newOrderRespType = OrderResponseType.ACK,
                    recvWindow = null,
                    timestamp = signedTimestamp(),
                    securityContext = securityContext()
                )
            }
        }.isOpexError(OpexError.InvalidRequestParam)

        assertThat(matchingGatewayProxy.createOrderCallCount).isZero()
    }

    @Test
    fun givenLimitOrder_whenCreateOrderRequested_thenSubmitExpectedMatchingOrder(): Unit = runBlocking {
        val matchingGatewayProxy = RecordingMatchingGatewayProxy()
        val controller = controller(matchingGatewayProxy = matchingGatewayProxy)

        controller.createNewOrder(
            symbol = "ETHUSDT",
            side = OrderSide.SELL,
            type = OrderType.LIMIT,
            timeInForce = TimeInForce.GTC,
            quantity = BigDecimal("0.5"),
            quoteOrderQty = null,
            price = BigDecimal("100"),
            newClientOrderId = null,
            stopPrice = null,
            icebergQty = null,
            newOrderRespType = null,
            recvWindow = null,
            timestamp = signedTimestamp(),
            securityContext = securityContext()
        )

        assertThat(matchingGatewayProxy.createOrderCallCount).isEqualTo(1)
        assertThat(matchingGatewayProxy.createOrderUuid).isEqualTo("user-1")
        assertThat(matchingGatewayProxy.createOrderPair).isEqualTo("ETH_USDT")
        assertThat(matchingGatewayProxy.createOrderPrice).isEqualByComparingTo("100")
        assertThat(matchingGatewayProxy.createOrderQuantity).isEqualByComparingTo("0.5")
        assertThat(matchingGatewayProxy.createOrderDirection).isEqualTo(OrderDirection.ASK)
        assertThat(matchingGatewayProxy.createOrderConstraint).isEqualTo(MatchConstraint.GTC)
        assertThat(matchingGatewayProxy.createOrderType).isEqualTo(MatchingOrderType.LIMIT_ORDER)
        assertThat(matchingGatewayProxy.createOrderClientOrderId).isNotBlank()
        assertThat(matchingGatewayProxy.createOrderClientOrderId).startsWith("x-")
        assertThat(matchingGatewayProxy.createOrderToken).isEqualTo("token-1")
    }

    private fun controller(
        queryHandler: RecordingMarketUserDataProxy = RecordingMarketUserDataProxy(),
        matchingGatewayProxy: RecordingMatchingGatewayProxy = RecordingMatchingGatewayProxy(),
        walletProxy: RecordingWalletProxy = RecordingWalletProxy(),
        accountantProxy: RecordingAccountantProxy = RecordingAccountantProxy()
    ) = AccountController(
        queryHandler,
        matchingGatewayProxy,
        walletProxy,
        accountantProxy,
        RecordingSymbolMapper()
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

    private class RecordingMarketUserDataProxy : MarketUserDataProxy {
        var openOrdersSymbol: String? = "not-called"
        var openOrdersLimit: Int? = null
        var allOrdersSymbol: String? = "not-called"
        var allOrdersLimit: Int? = null
        var allTradesSymbol: String? = "not-called"
        var allTradesLimit: Int? = null
        var allTradesOrderId: Long? = null
        var queryOrderCallCount = 0
        var queryOrderSymbol: String? = null
        var queryOrderId: Long? = null
        var queryOrigClientOrderId: String? = null
        var queryOrderResponse: Order? = order()
        var queryOrderFailure: RuntimeException? = null

        override suspend fun queryOrder(
            principal: Principal,
            symbol: String,
            orderId: Long?,
            origClientOrderId: String?
        ): Order? {
            queryOrderCallCount += 1
            queryOrderSymbol = symbol
            queryOrderId = orderId
            queryOrigClientOrderId = origClientOrderId
            queryOrderFailure?.let { throw it }
            return queryOrderResponse
        }

        override suspend fun openOrders(principal: Principal, symbol: String?, limit: Int?): List<Order> {
            openOrdersSymbol = symbol
            openOrdersLimit = limit
            return listOf(order())
        }

        override suspend fun allOrders(
            principal: Principal,
            symbol: String?,
            startTime: Date?,
            endTime: Date?,
            limit: Int?
        ): List<Order> {
            allOrdersSymbol = symbol
            allOrdersLimit = limit
            return listOf(order())
        }

        override suspend fun allTrades(
            principal: Principal,
            symbol: String?,
            fromTrade: Long?,
            startTime: Date?,
            endTime: Date?,
            limit: Int?,
            orderId: Long?
        ): List<Trade> {
            allTradesSymbol = symbol
            allTradesLimit = limit
            allTradesOrderId = orderId
            return emptyList()
        }

        private fun order() = Order(
            id = 1,
            ouid = "ouid-1",
            uuid = "user-1",
            clientOrderId = "client-1",
            symbol = "ETH_USDT",
            orderId = 100,
            makerFee = BigDecimal("0.001"),
            takerFee = BigDecimal("0.001"),
            leftSideFraction = BigDecimal.ONE,
            rightSideFraction = BigDecimal.ONE,
            userLevel = "*",
            direction = OrderDirection.ASK,
            constraint = MatchConstraint.GTC,
            type = MatchingOrderType.LIMIT_ORDER,
            price = BigDecimal("100"),
            quantity = BigDecimal("0.5"),
            quoteQuantity = BigDecimal.ZERO,
            executedQuantity = BigDecimal.ZERO,
            accumulativeQuoteQty = BigDecimal.ZERO,
            status = OrderStatus.NEW,
            createDate = LocalDateTime.now(),
            updateDate = LocalDateTime.now()
        )
    }

    private class RecordingSymbolMapper : SymbolMapper {
        override suspend fun fromInternalSymbol(symbol: String?): String? =
            when (symbol) {
                "ETH_USDT" -> "ETHUSDT"
                else -> symbol
            }

        override suspend fun toInternalSymbol(alias: String?): String? =
            when (alias) {
                "ETHUSDT" -> "ETH_USDT"
                else -> null
            }

        override suspend fun symbolToAliasMap(): Map<String, String> = mapOf("ETH_USDT" to "ETHUSDT")
    }

    private class RecordingMatchingGatewayProxy : MatchingGatewayProxy {
        var createOrderCallCount = 0
        var createOrderUuid: String? = null
        var createOrderPair: String? = null
        var createOrderPrice: BigDecimal? = null
        var createOrderQuantity: BigDecimal? = null
        var createOrderDirection: OrderDirection? = null
        var createOrderConstraint: MatchConstraint? = null
        var createOrderType: MatchingOrderType? = null
        var createOrderClientOrderId: String? = null
        var createOrderToken: String? = null
        var cancelOrderCallCount = 0
        var cancelOrderUuid: String? = null
        var cancelOrderToken: String? = null

        override suspend fun createNewOrder(
            uuid: String?,
            pair: String,
            price: BigDecimal,
            quantity: BigDecimal,
            direction: OrderDirection,
            matchConstraint: MatchConstraint?,
            orderType: MatchingOrderType,
            userLevel: String,
            clientOrderId: String?,
            token: String?
        ): OrderSubmitResult? {
            createOrderCallCount += 1
            createOrderUuid = uuid
            createOrderPair = pair
            createOrderPrice = price
            createOrderQuantity = quantity
            createOrderDirection = direction
            createOrderConstraint = matchConstraint
            createOrderType = orderType
            createOrderClientOrderId = clientOrderId
            createOrderToken = token
            return OrderSubmitResult(1)
        }

        override suspend fun cancelOrder(
            ouid: String,
            uuid: String,
            orderId: Long,
            symbol: String,
            token: String?
        ): OrderSubmitResult? {
            cancelOrderCallCount += 1
            cancelOrderUuid = uuid
            cancelOrderToken = token
            return OrderSubmitResult(2)
        }
    }

    private class RecordingWalletProxy : WalletProxy {
        var getWalletsCallCount = 0

        override suspend fun getWallets(uuid: String?, token: String?): List<Wallet> {
            getWalletsCallCount += 1
            return emptyList()
        }

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
        ): List<TransactionHistoryResponse> = emptyList()

        override suspend fun getWithdrawTransactions(
            uuid: String,
            token: String?,
            coin: String?,
            startTime: Long?,
            endTime: Long?,
            limit: Int,
            offset: Int,
            ascendingByTime: Boolean?
        ): List<WithdrawHistoryResponse> = emptyList()
    }

    private class RecordingAccountantProxy(
        private val fees: List<PairFeeResponse> = emptyList()
    ) : AccountantProxy {
        override suspend fun getPairConfigs(): List<PairInfoResponse> = emptyList()

        override suspend fun getFeeConfigs(): List<PairFeeResponse> = fees

        override suspend fun getFeeConfig(symbol: String): PairFeeResponse {
            throw UnsupportedOperationException("Not used by this test")
        }
    }
}
