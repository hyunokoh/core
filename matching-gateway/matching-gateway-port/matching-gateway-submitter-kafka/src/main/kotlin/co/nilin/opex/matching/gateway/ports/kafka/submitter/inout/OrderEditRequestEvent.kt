package co.nilin.opex.matching.gateway.ports.kafka.submitter.inout

import co.nilin.opex.matching.engine.core.model.Pair

class OrderEditRequestEvent(
    ouid: String,
    uuid: String,
    pair: Pair,
    val orderId: Long,
    val price: Long,
    val quantity: Long
) : OrderRequestEvent(ouid, uuid, pair)
