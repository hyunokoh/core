package co.nilin.opex.api.ports.binance.data

import co.nilin.opex.api.core.inout.OrderSide
import co.nilin.opex.api.core.inout.OrderStatus
import co.nilin.opex.api.core.inout.OrderType
import co.nilin.opex.api.core.inout.TimeInForce
import com.fasterxml.jackson.annotation.JsonInclude
import java.math.BigDecimal

@JsonInclude(JsonInclude.Include.NON_NULL)
data class QueryOrderResponse(
    var symbol: String,
    val ouid: String,
    val orderId: Long,
    val orderListId: Long, //Unless part of an OCO, the value will always be -1.
    val clientOrderId: String,
    val price: BigDecimal,
    val origQty: BigDecimal,
    val executedQty: BigDecimal,
    val cummulativeQuoteQty: BigDecimal,
    val status: OrderStatus,
    val timeInForce: TimeInForce,
    val type: OrderType,
    val side: OrderSide,
    val stopPrice: BigDecimal?,
    val icebergQty: BigDecimal?,
    val time: Long,
    val updateTime: Long,
    val isWorking: Boolean,
    val origQuoteOrderQty: BigDecimal
)