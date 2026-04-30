package co.nilin.opex.matching.engine.core.model

class PersistentOrderBook {

    lateinit var pair: Pair
    var lastOrder: PersistentOrder? = null
    var orders: List<PersistentOrder>? = emptyList()
    var tradeCounter: Long = 0
    var processedOrderOuids: Set<String> = emptySet()
    var processedCancelOuids: Set<String> = emptySet()

    constructor() {
    }

    constructor(pair: Pair) {
        this.pair = pair
    }

}
