package co.nilin.opex.matching.gateway.app.service

import co.nilin.opex.common.OpexError
import co.nilin.opex.matching.engine.core.model.MatchConstraint
import co.nilin.opex.matching.engine.core.model.OrderDirection
import co.nilin.opex.matching.engine.core.model.OrderType
import co.nilin.opex.matching.engine.core.model.Pair
import co.nilin.opex.matching.gateway.app.inout.CancelOrderRequest
import co.nilin.opex.matching.gateway.app.inout.CreateOrderRequest
import co.nilin.opex.matching.gateway.app.spi.AccountantApiProxy
import co.nilin.opex.matching.gateway.app.spi.PairConfigLoader
import co.nilin.opex.matching.gateway.ports.kafka.submitter.inout.OrderCancelRequestEvent
import co.nilin.opex.matching.gateway.ports.kafka.submitter.inout.OrderSubmitRequestEvent
import co.nilin.opex.matching.gateway.ports.kafka.submitter.inout.OrderSubmitResult
import co.nilin.opex.matching.gateway.ports.kafka.submitter.service.KafkaHealthIndicator
import co.nilin.opex.matching.gateway.ports.kafka.submitter.service.OrderRequestEventSubmitter
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.math.BigDecimal

@Service
class OrderService(
    val accountantApiProxy: AccountantApiProxy,
    val orderRequestEventSubmitter: OrderRequestEventSubmitter,
    val pairConfigLoader: PairConfigLoader,
    private val kafkaHealthIndicator: KafkaHealthIndicator,
) {

    private val logger = LoggerFactory.getLogger(OrderService::class.java)

    suspend fun submitNewOrder(createOrderRequest: CreateOrderRequest): OrderSubmitResult {
        val uuid = createOrderRequest.uuid ?: badRequest("uuid is required")
        if (uuid.isBlank())
            badRequest("uuid is required")
        if (createOrderRequest.matchConstraint !in setOf(MatchConstraint.GTC, MatchConstraint.IOC))
            badRequest("match constraint is not supported")
        if (createOrderRequest.orderType == OrderType.LIMIT_ORDER) {
            if (createOrderRequest.price <= BigDecimal.ZERO)
                badRequest("limit order price must be greater than zero")
        } else {
            if (createOrderRequest.matchConstraint == MatchConstraint.GTC)
                badRequest("market order cannot be GTC")
            if (createOrderRequest.price < BigDecimal.ZERO)
                badRequest("market order price must be zero or greater")
            if (createOrderRequest.direction == OrderDirection.BID && createOrderRequest.price <= BigDecimal.ZERO)
                badRequest("market bid price must be greater than zero")
        }
        if (createOrderRequest.quantity <= BigDecimal.ZERO)
            badRequest("quantity must be greater than zero")
        val symbolSides = parsePair(createOrderRequest.pair)
        val symbol = if (createOrderRequest.direction == OrderDirection.ASK)
            symbolSides[0]
        else
            symbolSides[1]

        //TODO cache
        val pairConfig = pairConfigLoader.load(createOrderRequest.pair, createOrderRequest.direction)

        val canCreateOrder = runCatching {
            accountantApiProxy.canCreateOrder(
                uuid,
                symbol,
                if (createOrderRequest.direction == OrderDirection.ASK)
                    createOrderRequest.quantity
                else
                    createOrderRequest.quantity.multiply(createOrderRequest.price)
            )
        }.onFailure {
            logger.error("Failed to check whether order can be created", it)
        }.getOrElse {
            throw OpexError.ServiceUnavailable.exception("accountant service is unavailable")
        }

        if (!canCreateOrder)
            throw OpexError.SubmitOrderForbiddenByAccountant.exception()

        if (!kafkaHealthIndicator.isHealthy)
            throw OpexError.ServiceUnavailable.exception()

        val orderSubmitRequest = OrderSubmitRequestEvent(
            uuid, //get from auth2
            Pair(symbolSides[0], symbolSides[1]),
            toOrderUnits(createOrderRequest.price, pairConfig.rightSideFraction, "price"),
            toOrderUnits(createOrderRequest.quantity, pairConfig.leftSideFraction, "quantity"),
            createOrderRequest.direction,
            createOrderRequest.matchConstraint,
            createOrderRequest.orderType,
            createOrderRequest.userLevel
        )
        return orderRequestEventSubmitter.submit(orderSubmitRequest)
    }

    suspend fun cancelOrder(request: CancelOrderRequest): OrderSubmitResult {
        if (!kafkaHealthIndicator.isHealthy)
            throw OpexError.ServiceUnavailable.exception()

        if (request.uuid.isBlank())
            badRequest("uuid is required")
        if (request.ouid.isBlank())
            badRequest("ouid is required")
        val symbols = parsePair(request.symbol)
        if (request.orderId < 0)
            badRequest("orderId must be zero or greater")

        val event = OrderCancelRequestEvent(request.ouid, request.uuid, Pair(symbols[0], symbols[1]), request.orderId)
        return orderRequestEventSubmitter.submit(event)
    }

    private fun parsePair(pair: String): List<String> {
        val symbols = pair.split("_")
        if (symbols.size != 2 || symbols[0].isBlank() || symbols[1].isBlank())
            badRequest("pair must be formatted as BASE_QUOTE")
        return symbols
    }

    private fun toOrderUnits(value: BigDecimal, fraction: BigDecimal, field: String): Long {
        return try {
            value.divide(fraction).longValueExact()
        } catch (ex: ArithmeticException) {
            badRequest("$field does not match pair precision")
        }
    }

    private fun badRequest(message: String): Nothing {
        throw OpexError.BadRequest.exception(message)
    }
}
