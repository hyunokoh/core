package co.nilin.opex.matching.engine.app.bl

import co.nilin.opex.matching.engine.app.config.AppSchedulers
import co.nilin.opex.matching.engine.core.spi.OrderBookPersister
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import org.slf4j.LoggerFactory

class OrderBookBootstrapper(
    private val symbols: List<String>,
    private val orderBookPersister: OrderBookPersister
) {
    private val logger = LoggerFactory.getLogger(OrderBookBootstrapper::class.java)
    private val scope = CoroutineScope(SupervisorJob() + AppSchedulers.generalExecutor)

    fun bootstrap(): List<Job> {
        return symbols.map { symbol ->
            scope.launch {
                try {
                    val lastOrderBook = orderBookPersister.loadLastState(symbol)
                    if (lastOrderBook != null) {
                        OrderBooks.reloadOrderBook(lastOrderBook)
                    } else {
                        OrderBooks.createOrderBook(symbol)
                    }
                    logger.info("Order book bootstrap completed: symbol={}", symbol)
                } catch (e: Exception) {
                    logger.error("Order book bootstrap failed: symbol=$symbol", e)
                }
            }
        }
    }
}
