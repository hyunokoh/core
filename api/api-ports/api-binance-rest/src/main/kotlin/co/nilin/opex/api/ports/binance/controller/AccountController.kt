package co.nilin.opex.api.ports.binance.controller

import co.nilin.opex.api.core.inout.*
import co.nilin.opex.api.core.spi.AccountantProxy
import co.nilin.opex.api.core.spi.MarketUserDataProxy
import co.nilin.opex.api.core.spi.MatchingGatewayProxy
import co.nilin.opex.api.core.spi.SymbolMapper
import co.nilin.opex.api.core.spi.WalletProxy
import co.nilin.opex.api.ports.binance.data.*
import co.nilin.opex.api.ports.binance.util.*
import co.nilin.opex.common.OpexError
import io.swagger.annotations.ApiParam
import io.swagger.annotations.ApiResponse
import io.swagger.annotations.Example
import io.swagger.annotations.ExampleProperty
import org.springframework.http.MediaType
import org.springframework.http.HttpStatus
import org.springframework.security.core.annotation.CurrentSecurityContext
import org.springframework.security.core.context.SecurityContext
import org.springframework.web.bind.annotation.*
import org.springframework.web.reactive.function.client.WebClientResponseException
import java.math.BigDecimal
import java.security.Principal
import java.time.ZoneId
import java.util.*

@RestController
class AccountController(
    val queryHandler: MarketUserDataProxy,
    val matchingGatewayProxy: MatchingGatewayProxy,
    val walletProxy: WalletProxy,
    val accountantProxy: AccountantProxy,
    val symbolMapper: SymbolMapper
) {

    private val defaultAccountQueryLimit = 500
    private val maxAccountQueryLimit = 1000
    private val maxClientOrderIdLength = 72

    /*
    Send in a new order.
    Weight: 1
    Data Source: Matching Engine
    */
    @PostMapping(
        "/v3/order",
        consumes = [MediaType.APPLICATION_FORM_URLENCODED_VALUE],
        produces = [MediaType.APPLICATION_JSON_VALUE]
    )
    @ApiResponse(
        message = "OK",
        code = 200,
        examples = Example(
            ExampleProperty(
                value = "{ \"symbol\": \"btc_usdt\", \"orderId\": -1, \"orderListId\": -1, \"transactTime\": \"2021-08-03T11:09:23.190+00:00\" }",
                mediaType = "application/json"
            )
        )
    )
    suspend fun createNewOrder(
        @RequestParam
        symbol: String,
        @RequestParam
        side: OrderSide,
        @RequestParam
        type: OrderType,
        @RequestParam(required = false)
        timeInForce: TimeInForce?,
        @RequestParam(required = false)
        quantity: BigDecimal?,
        @RequestParam(required = false)
        quoteOrderQty: BigDecimal?,
        @RequestParam(required = false)
        price: BigDecimal?,
        @ApiParam(
            value = "A unique id among open orders. Automatically generated if not sent.\n" +
                    "Orders with the same newClientOrderID can be accepted only when the previous one is filled, otherwise the order will be rejected."
        )
        @RequestParam(required = false)
        newClientOrderId: String?,    /* A unique id among open orders. Automatically generated if not sent.
    Orders with the same newClientOrderID can be accepted only when the previous one is filled, otherwise the order will be rejected.
    */
        @ApiParam(value = "Used with STOP_LOSS, STOP_LOSS_LIMIT, TAKE_PROFIT, and TAKE_PROFIT_LIMIT orders.")
        @RequestParam(required = false)
        stopPrice: BigDecimal?, //Used with STOP_LOSS, STOP_LOSS_LIMIT, TAKE_PROFIT, and TAKE_PROFIT_LIMIT orders.
        @RequestParam(required = false)
        @ApiParam(value = "Used with LIMIT, STOP_LOSS_LIMIT, and TAKE_PROFIT_LIMIT to create an iceberg order.")
        icebergQty: BigDecimal?, //Used with LIMIT, STOP_LOSS_LIMIT, and TAKE_PROFIT_LIMIT to create an iceberg order.
        @RequestParam(required = false)
        @ApiParam(value = "Set the response JSON. ACK, RESULT, or FULL; MARKET and LIMIT order types default to FULL, all other orders default to ACK.")
        newOrderRespType: OrderResponseType?,  //Set the response JSON. ACK, RESULT, or FULL; MARKET and LIMIT order types default to FULL, all other orders default to ACK.
        @ApiParam(value = "The value cannot be greater than 60000")
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long,
        @CurrentSecurityContext securityContext: SecurityContext
    ): NewOrderResponse {
        validateSignedRequest(recvWindow, timestamp)
        validateRequiredSymbol(symbol)
        val internalSymbol = symbolMapper.toInternalSymbol(symbol) ?: throw OpexError.SymbolNotFound.exception()
        validateNewOrderParams(type, side, price, quantity, timeInForce, stopPrice, quoteOrderQty)
        validateNewClientOrderId(newClientOrderId)
        validateUnsupportedNewOrderParams(icebergQty, newOrderRespType)
        val authentication = securityContext.jwtAuthentication()
        val effectiveClientOrderId = newClientOrderId ?: generateClientOrderId()
        rejectDuplicateOpenClientOrderId(Principal { authentication.name }, internalSymbol, newClientOrderId)
        val effectiveMatchConstraint = when (type) {
            OrderType.MARKET -> MatchConstraint.IOC
            else -> timeInForce?.asMatchConstraint()
        }

        matchingGatewayProxy.createNewOrder(
            authentication.name,
            internalSymbol,
            price ?: BigDecimal.ZERO, // Maybe make this nullable as well?
            quantity ?: BigDecimal.ZERO,
            side.asOrderDirection(),
            effectiveMatchConstraint,
            type.asMatchingOrderType(),
            "*",
            effectiveClientOrderId,
            authentication.tokenValue()
        )
        return NewOrderResponse(
            symbol,
            -1,
            -1,
            effectiveClientOrderId,
            Date().time,
            null,
            null,
            null,
            null,
            null,
            null,
            null,
            null,
            null
        )
    }

    @DeleteMapping(
        "/v3/order",
        consumes = [MediaType.APPLICATION_FORM_URLENCODED_VALUE],
        produces = [MediaType.APPLICATION_JSON_VALUE]
    )
    suspend fun cancelOrder(
        principal: Principal,
        @RequestParam
        symbol: String,
        @RequestParam(required = false)
        orderId: Long?, //Either orderId or origClientOrderId must be sent.
        @RequestParam(required = false)
        origClientOrderId: String?,
        @RequestParam(required = false)
        newClientOrderId: String?,
        @ApiParam(value = "The value cannot be greater than 60000")
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long,
        @CurrentSecurityContext securityContext: SecurityContext
    ): CancelOrderResponse {
        validateSignedRequest(recvWindow, timestamp)
        validateRequiredSymbol(symbol)
        val localSymbol = symbolMapper.toInternalSymbol(symbol) ?: throw OpexError.SymbolNotFound.exception()
        validateOrderLookupParams(orderId, origClientOrderId)
        validateNewClientOrderId(newClientOrderId)

        val order = queryHandler.queryOrder(principal, localSymbol, orderId, origClientOrderId)
            ?: throw OpexError.OrderNotFound.exception()

        val response = CancelOrderResponse(
            symbol,
            order.clientOrderId,
            order.orderId,
            -1,
            newClientOrderId ?: order.clientOrderId,
            order.price,
            order.quantity,
            order.executedQuantity,
            order.accumulativeQuoteQty,
            OrderStatus.CANCELED,
            order.constraint.asTimeInForce(),
            order.type.asOrderType(),
            order.direction.asOrderSide()
        )

        if (order.status == OrderStatus.CANCELED)
            return response

        if (order.status.equalsAny(OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.FILLED))
            throw OpexError.CancelOrderNotAllowed.exception()


        val auth = securityContext.jwtAuthentication()
        matchingGatewayProxy.cancelOrder(
            order.ouid,
            auth.name,
            order.orderId ?: 0,
            localSymbol,
            auth.tokenValue()
        )
        return response
    }

    /*
  Check an order's status.

  Weight: 2
  Data Source: Database
  */
    @GetMapping(
        "/v3/order",
        produces = [MediaType.APPLICATION_JSON_VALUE]
    )
    @ApiResponse(
        message = "OK",
        code = 200,
        examples = Example(
            ExampleProperty(
                value = "{ \"symbol\": \"btc_usdt\", \"orderId\": 12, \"orderListId\": -1, \"clientOrderId\": \"\", \"price\": 1, \"origQty\": 10, \"executedQty\": 0, \"cummulativeQuoteQty\": 0, \"status\": \"NEW\", \"timeInForce\": \"GTC\", \"type\": \"LIMIT\", \"side\": \"SELL\", \"time\": \"2021-08-04T12:10:13.488+00:00\", \"updateTime\": \"2021-08-04T12:10:13.488+00:00\", \"isWorking\": true, \"origQuoteOrderQty\": 10 }",
                mediaType = "application/json"
            )
        )
    )
    suspend fun queryOrder(
        principal: Principal,
        @RequestParam
        symbol: String,
        @RequestParam(required = false)
        orderId: Long?,
        @RequestParam(required = false)
        origClientOrderId: String?,
        @ApiParam(value = "The value cannot be greater than 60000")
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long
    ): QueryOrderResponse {
        validateSignedRequest(recvWindow, timestamp)
        validateRequiredSymbol(symbol)
        val internalSymbol = symbolMapper.toInternalSymbol(symbol) ?: throw OpexError.SymbolNotFound.exception()
        validateOrderLookupParams(orderId, origClientOrderId)
        return queryHandler.queryOrder(principal, internalSymbol, orderId, origClientOrderId)
            ?.asQueryOrderResponse()
            ?.apply { this.symbol = symbol }
            ?: throw OpexError.OrderNotFound.exception()
    }

    /*
      Get all open orders on a symbol. Careful when accessing this with no symbol.

      Weight: 3 for a single symbol; 40 when the symbol parameter is omitted

      Data Source: Database
    */
    @GetMapping(
        "/v3/openOrders",
        produces = [MediaType.APPLICATION_JSON_VALUE]
    )
    @ApiResponse(
        message = "OK",
        code = 200,
        examples = Example(
            ExampleProperty(
                value = "[ { \"symbol\": \"btc_usdt\", \"orderId\": 12, \"orderListId\": -1, \"clientOrderId\": \"\", \"price\": 1, \"origQty\": 10, \"executedQty\": 0, \"cummulativeQuoteQty\": 0, \"status\": \"NEW\", \"timeInForce\": \"GTC\", \"type\": \"LIMIT\", \"side\": \"SELL\", \"time\": \"2021-08-04T12:10:13.488+00:00\", \"updateTime\": \"2021-08-04T12:10:13.488+00:00\", \"isWorking\": true, \"origQuoteOrderQty\": 10 } ]",
                mediaType = "application/json"
            )
        )
    )
    suspend fun fetchOpenOrders(
        principal: Principal,
        @RequestParam(required = false)
        symbol: String?,
        @ApiParam(value = "The value cannot be greater than 60000")
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long,
        @RequestParam(required = false)
        limit: Int?
    ): List<QueryOrderResponse> {
        validateSignedRequest(recvWindow, timestamp)
        validateOptionalSymbol(symbol)
        val internalSymbol = symbol?.let { symbolMapper.toInternalSymbol(it) ?: throw OpexError.SymbolNotFound.exception() }
        val validLimit = validOptionalAccountQueryLimit(limit)
        return queryHandler.openOrders(principal, internalSymbol, validLimit).map {
            it.asQueryOrderResponse().apply { this.symbol = responseSymbol(symbol, it.symbol) }
        }
    }

    /*
   Get all account orders; active, canceled, or filled.
   Weight: 10 with symbol
   Data Source: Database
   */
    @GetMapping(
        "/v3/allOrders",
        produces = [MediaType.APPLICATION_JSON_VALUE]
    )
    @ApiResponse(
        message = "OK",
        code = 200,
        examples = Example(
            ExampleProperty(
                value = "{ }",
                mediaType = "application/json"
            )
        )
    )
    suspend fun fetchAllOrders(
        principal: Principal,
        @RequestParam(required = false)
        symbol: String?,
        @RequestParam(required = false)
        startTime: Date?,
        @RequestParam(required = false)
        endTime: Date?,
        @ApiParam(value = "Default 500; max 1000.")
        @RequestParam(required = false)
        limit: Int?, //Default 500; max 1000.
        @ApiParam(value = "The value cannot be greater than 60000")
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long
    ): List<QueryOrderResponse> {
        validateSignedRequest(recvWindow, timestamp)
        validateOptionalSymbol(symbol)
        validateAccountTimeRange(startTime, endTime)
        val internalSymbol = symbol?.let { symbolMapper.toInternalSymbol(it) ?: throw OpexError.SymbolNotFound.exception() }
        val validLimit = validAccountQueryLimit(limit)
        return queryHandler.allOrders(principal, internalSymbol, startTime, endTime, validLimit).map {
            it.asQueryOrderResponse().apply { this.symbol = responseSymbol(symbol, it.symbol) }
        }
    }

    /*
    Get trades for a specific account and symbol.
    If fromId is set, it will get trades >= that fromId. Otherwise, most recent trades are returned.
    Weight: 10 with symbol
    Data Source: Database
    */
    @GetMapping(
        "/v3/myTrades",
        produces = [MediaType.APPLICATION_JSON_VALUE]
    )
    @ApiResponse(
        message = "OK",
        code = 200,
        examples = Example(
            ExampleProperty(
                value = "{ }",
                mediaType = "application/json"
            )
        )
    )
    suspend fun fetchAllTrades(
        principal: Principal,
        @RequestParam
        symbol: String?,
        @RequestParam(required = false)
        startTime: Date?,
        @RequestParam(required = false)
        endTime: Date?,
        @ApiParam(value = "TradeId to fetch from. Default gets most recent trades.")
        @RequestParam(required = false)
        fromId: Long?,//TradeId to fetch from. Default gets most recent trades.
        @ApiParam(value = "Default 500; max 1000.")
        @RequestParam(required = false)
        limit: Int?, //Default 500; max 1000.
        @ApiParam(value = "The value cannot be greater than 60000")
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long,
        @RequestParam(required = false)
        orderId: Long? = null
    ): List<TradeResponse> {
        validateSignedRequest(recvWindow, timestamp)
        validateRequiredSymbol(symbol)
        validateAccountTimeRange(startTime, endTime)
        validateFromId(fromId)
        validateOrderId(orderId)
        val internalSymbol = symbolMapper.toInternalSymbol(symbol) ?: throw OpexError.SymbolNotFound.exception()
        val validLimit = validAccountQueryLimit(limit)

        return queryHandler.allTrades(principal, internalSymbol, fromId, startTime, endTime, validLimit, orderId)
            .map {
                TradeResponse(
                    symbol ?: "",
                    it.id,
                    it.orderId,
                    -1,
                    it.price,
                    it.quantity,
                    it.quoteQuantity,
                    it.commission,
                    it.commissionAsset,
                    it.time,
                    it.isBuyer,
                    it.isMaker,
                    it.isBestMatch
                )
            }
    }

    @GetMapping(
        "/v3/account",
        produces = [MediaType.APPLICATION_JSON_VALUE]
    )
    @ApiResponse(
        message = "OK",
        code = 200,
        examples = Example(
            ExampleProperty(
                value = "{ \"makerCommission\": 0, \"takerCommission\": 0, \"buyerCommission\": 0, \"sellerCommission\": 0, \"canTrade\": true, \"canWithdraw\": true, \"canDeposit\": true, \"updateTime\": 1628420513843, \"accountType\": \"SPOT\", \"balances\": [ { \"asset\": \"usdt\", \"free\": 1000, \"locked\": 0 } ], \"permissions\": [ \"SPOT\" ] }",
                mediaType = "application/json"
            )
        )
    )
    suspend fun accountInfo(
        @CurrentSecurityContext securityContext: SecurityContext,
        @ApiParam(value = "The value cannot be greater than 60000")
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long
    ): AccountInfoResponse {
        validateSignedRequest(recvWindow, timestamp)
        val auth = securityContext.jwtAuthentication()
        val wallets = walletProxy.getWallets(auth.name, auth.tokenValue())
        val limits = walletProxy.getOwnerLimits(auth.name, auth.tokenValue())
        val feeConfigs = accountantProxy.getFeeConfigs()
        val makerCommission = feeConfigs.maxOfOrNull { it.makerFee.toBinanceCommission() } ?: 0
        val takerCommission = feeConfigs.maxOfOrNull { it.takerFee.toBinanceCommission() } ?: 0
        val accountType = "SPOT"

        return AccountInfoResponse(
            makerCommission,
            takerCommission,
            0,
            0,
            limits.canTrade,
            limits.canWithdraw,
            limits.canDeposit,
            Date().time,
            accountType,
            wallets.map { BalanceResponse(it.asset, it.balance, it.locked, it.withdraw) },
            listOf(accountType)
        )
    }

    private fun BigDecimal.toBinanceCommission(): Long = multiply(BigDecimal("10000")).toLong()

    private fun validateNewOrderParams(
        type: OrderType,
        side: OrderSide,
        price: BigDecimal?,
        quantity: BigDecimal?,
        timeInForce: TimeInForce?,
        stopPrice: BigDecimal?,
        quoteOrderQty: BigDecimal?,
    ) {
        if (!OrderType.activeTypes().contains(type))
            throw OpexError.InvalidRequestParam.exception("Parameter 'type' is either missing or invalid")
        if (stopPrice != null)
            throw OpexError.InvalidRequestParam.exception("Parameter 'stopPrice' is either missing or invalid")

        when (type) {
            OrderType.LIMIT -> {
                checkDecimal(price, "price")
                checkDecimal(quantity, "quantity")
                checkNull(timeInForce, "timeInForce")
            }

            OrderType.MARKET -> {
                if (timeInForce != null)
                    throw OpexError.InvalidRequestParam.exception("Parameter 'timeInForce' is either missing or invalid")
                if (quoteOrderQty != null)
                    throw OpexError.InvalidRequestParam.exception("Parameter 'quoteOrderQty' is either missing or invalid")
                if (side == OrderSide.BUY)
                    checkDecimal(price, "price")
                checkDecimal(quantity, "quantity")
            }

            else -> throw OpexError.InvalidRequestParam.exception("Parameter 'type' is either missing or invalid")
        }
    }

    private fun validateUnsupportedNewOrderParams(
        icebergQty: BigDecimal?,
        newOrderRespType: OrderResponseType?
    ) {
        if (icebergQty != null)
            throw OpexError.InvalidRequestParam.exception("Parameter 'icebergQty' is either missing or invalid")
        if (newOrderRespType != null && newOrderRespType != OrderResponseType.ACK)
            throw OpexError.InvalidRequestParam.exception("Parameter 'newOrderRespType' is either missing or invalid")
    }

    private fun validateNewClientOrderId(newClientOrderId: String?) {
        if (newClientOrderId != null && (newClientOrderId.isBlank() || newClientOrderId.length > maxClientOrderIdLength))
            throw OpexError.InvalidRequestParam.exception("Parameter 'newClientOrderId' is either missing or invalid")
    }

    private fun generateClientOrderId(): String =
        "x-${UUID.randomUUID().toString().replace("-", "")}"

    private suspend fun rejectDuplicateOpenClientOrderId(
        principal: Principal,
        symbol: String,
        newClientOrderId: String?
    ) {
        if (newClientOrderId == null)
            return
        val existingOrder = try {
            queryHandler.queryOrder(principal, symbol, null, newClientOrderId)
        } catch (ex: WebClientResponseException) {
            if (ex.statusCode == HttpStatus.NOT_FOUND)
                null
            else
                throw ex
        }
        if (existingOrder?.status?.isWorking() == true)
            throw OpexError.BadRequest.exception("newClientOrderId is already in use by an open order")
    }

    private fun checkDecimal(decimal: BigDecimal?, paramName: String) {
        if (decimal == null || decimal <= BigDecimal.ZERO)
            throw OpexError.InvalidRequestParam.exception("Parameter '$paramName' is either missing or invalid")
    }

    private fun checkNull(obj: Any?, paramName: String) {
        if (obj == null)
            throw OpexError.InvalidRequestParam.exception("Parameter '$paramName' is either missing or invalid")
    }

    private fun validateOrderLookupParams(orderId: Long?, origClientOrderId: String?) {
        if (orderId == null && origClientOrderId == null)
            throw OpexError.BadRequest.exception("'orderId' or 'origClientOrderId' must be sent")
        if (orderId != null && orderId <= 0)
            throw OpexError.InvalidRequestParam.exception("Parameter 'orderId' is either missing or invalid")
        if (origClientOrderId != null && origClientOrderId.isBlank())
            throw OpexError.InvalidRequestParam.exception("Parameter 'origClientOrderId' is either missing or invalid")
    }

    private fun validAccountQueryLimit(limit: Int?): Int {
        val validLimit = limit ?: defaultAccountQueryLimit
        if (validLimit !in 1..maxAccountQueryLimit)
            throw OpexError.InvalidRequestParam.exception("Parameter 'limit' is either missing or invalid")
        return validLimit
    }

    private fun validOptionalAccountQueryLimit(limit: Int?): Int? {
        if (limit != null && limit !in 1..maxAccountQueryLimit)
            throw OpexError.InvalidRequestParam.exception("Parameter 'limit' is either missing or invalid")
        return limit
    }

    private fun validateAccountTimeRange(startTime: Date?, endTime: Date?) {
        if (startTime != null && startTime.time <= 0)
            throw OpexError.InvalidRequestParam.exception("Parameter 'startTime' is either missing or invalid")
        if (endTime != null && endTime.time <= 0)
            throw OpexError.InvalidRequestParam.exception("Parameter 'endTime' is either missing or invalid")
        if (startTime != null && endTime != null && startTime.after(endTime))
            throw OpexError.InvalidRequestParam.exception("Parameter 'startTime' is either missing or invalid")
    }

    private fun validateFromId(fromId: Long?) {
        if (fromId != null && fromId < 0)
            throw OpexError.InvalidRequestParam.exception("Parameter 'fromId' is either missing or invalid")
    }

    private fun validateOrderId(orderId: Long?) {
        if (orderId != null && orderId <= 0)
            throw OpexError.InvalidRequestParam.exception("Parameter 'orderId' is either missing or invalid")
    }

    private fun validateOptionalSymbol(symbol: String?) {
        if (symbol != null && symbol.isBlank())
            throw OpexError.InvalidRequestParam.exception("Parameter 'symbol' is either missing or invalid")
    }

    private fun validateRequiredSymbol(symbol: String?) {
        if (symbol.isNullOrBlank())
            throw OpexError.InvalidRequestParam.exception("Parameter 'symbol' is either missing or invalid")
    }

    private suspend fun responseSymbol(requestSymbol: String?, internalSymbol: String): String {
        return requestSymbol ?: symbolMapper.fromInternalSymbol(internalSymbol) ?: internalSymbol
    }

    private fun Order.asQueryOrderResponse() = QueryOrderResponse(
        symbol,
        ouid,
        orderId ?: 0,
        -1,
        clientOrderId ?: "",
        price,
        quantity,
        executedQuantity,
        accumulativeQuoteQty,
        status,
        constraint.asTimeInForce(),
        type.asOrderType(),
        direction.asOrderSide(),
        null,
        null,
        Date.from(createDate.atZone(ZoneId.systemDefault()).toInstant()),
        Date.from(updateDate.atZone(ZoneId.systemDefault()).toInstant()),
        status.isWorking(),
        quoteQuantity
    )

}
