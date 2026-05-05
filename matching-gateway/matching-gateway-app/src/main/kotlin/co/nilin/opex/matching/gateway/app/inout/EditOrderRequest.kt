package co.nilin.opex.matching.gateway.app.inout

import java.math.BigDecimal

class EditOrderRequest(
    val ouid: String,
    var uuid: String,
    val orderId: Long,
    val symbol: String,
    val price: BigDecimal,
    val quantity: BigDecimal
)
