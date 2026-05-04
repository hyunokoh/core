package co.nilin.opex.matching.engine.app.bl

import co.nilin.opex.matching.engine.core.factory.OrderBookFactory
import co.nilin.opex.matching.engine.core.model.OrderBook
import co.nilin.opex.matching.engine.core.model.Pair
import co.nilin.opex.matching.engine.core.model.PersistentOrderBook
import org.slf4j.LoggerFactory
import java.util.concurrent.ConcurrentHashMap

object OrderBooks {
    private val logger = LoggerFactory.getLogger(OrderBooks::class.java)
    private val orderBooks = ConcurrentHashMap<String, OrderBook>()

    fun createOrderBook(pair: String) {
        val pairKey = normalizePairKey(pair)
        val symbols = pairKey.split("_")
        val created = OrderBookFactory.createOrderBook(Pair(symbols[0], symbols[1]))
        if (orderBooks.putIfAbsent(pairKey, created) != null)
            throw IllegalArgumentException("$pairKey has an order book right now!")
        logger.info("Order book created: pair={}, currentOrderBooks={}", pairKey, orderBooks.size)
    }

    fun reloadOrderBook(orderBook: PersistentOrderBook) {
        val pairKey = normalizePairKey("${orderBook.pair.leftSideName}_${orderBook.pair.rightSideName}")
        orderBooks[pairKey] = OrderBookFactory.createOrderBook(orderBook)
        logger.info("Order book reloaded: pair={}, currentOrderBooks={}", pairKey, orderBooks.size)
    }

    fun lookupOrderBook(pair: String): OrderBook {
        val pairKey = normalizePairKey(pair)
        return orderBooks[pairKey] ?: throw IllegalArgumentException("No orderbook for $pairKey")
    }

    private fun normalizePairKey(pair: String): String {
        val pairKey = pair.trim().uppercase()
        val symbols = pairKey.split("_")
        if (symbols.size != 2 || symbols.any { it.isBlank() })
            throw IllegalArgumentException("pair must be formatted as BASE_QUOTE")
        return pairKey
    }

    internal fun clearForTest() {
        orderBooks.clear()
    }
}
