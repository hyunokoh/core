package co.nilin.opex.matching.engine.core

import co.nilin.opex.matching.engine.core.engine.SimpleOrderBook
import co.nilin.opex.matching.engine.core.eventh.EventDispatcher
import co.nilin.opex.matching.engine.core.inout.OrderCancelCommand
import co.nilin.opex.matching.engine.core.inout.OrderCreateCommand
import co.nilin.opex.matching.engine.core.model.MatchConstraint
import co.nilin.opex.matching.engine.core.model.OrderDirection
import co.nilin.opex.matching.engine.core.model.OrderType
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import java.util.UUID
import kotlin.system.measureNanoTime

/**
 * Throughput / latency probe for SimpleOrderBook.
 *
 * Two scenarios:
 *  1. ``insertResting``: stream of non-crossing limit orders that all rest in the book —
 *     covers the hot path during low-volatility periods (most orders are made, not taken).
 *  2. ``crossingTrades``: stream where every incoming order takes the top of book, producing
 *     a trade — covers the high-volatility path where the engine is doing matching work.
 *
 * The class name does not end in "Test", so surefire's default include pattern (which
 * matches files ending in Test) skips it. Opt in with:
 *
 *     mvn -pl matching-engine/matching-engine-core test \
 *         -Dtest=SimpleOrderBookBench -DfailIfNoTests=false
 *
 * The numbers are local-machine baselines; the asserts only fail on a catastrophic
 * regression (below ~200 ops/s) so a refactor that quietly halves throughput is loud
 * without being noisy. Real production sizing should still be done with JMH against a
 * heated JVM.
 */
class SimpleOrderBookBench {

    private val pair = co.nilin.opex.matching.engine.core.model.Pair("BTC", "USDT")
    private val owner = UUID.randomUUID().toString()

    @BeforeEach
    fun reset() {
        EventDispatcher.clearAll()
    }

    private fun newOrder(
        ouid: String,
        ownerUuid: String,
        price: Long,
        qty: Long,
        side: OrderDirection,
    ) = OrderCreateCommand(
        ouid,
        ownerUuid,
        pair,
        price,
        qty,
        side,
        MatchConstraint.GTC,
        OrderType.LIMIT_ORDER,
    )

    private fun report(label: String, n: Int, totalNanos: Long) {
        val perOp = totalNanos.toDouble() / n
        val opsPerSec = n.toDouble() / (totalNanos / 1e9)
        println(
            "perf %-24s n=%6d  total=%9.3f ms  per-op=%9.0f ns  ops/s=%9.1f"
                .format(label, n, totalNanos / 1e6, perOp, opsPerSec)
        )
        // The floor is intentionally generous: we are catching catastrophic regressions,
        // not setting an SLA. Local-machine measurements include EventDispatcher overhead
        // and a cold JVM, so production numbers under JMH with a hot path will be higher.
        // Real sizing belongs in a dedicated JMH module; this is a smoke probe.
        check(opsPerSec > 200) {
            "$label collapsed to %.1f ops/s — regression?".format(opsPerSec)
        }
    }

    @Test
    fun `bench insert non-crossing limit orders`() {
        val orderBook = SimpleOrderBook(pair, false)
        // Warm up the JIT — a cold first run spends most of its time compiling, not matching.
        repeat(2_000) { i ->
            orderBook.handleNewOrderCommand(
                newOrder(UUID.randomUUID().toString(), owner, 100L + (i % 10), 1, OrderDirection.BID),
            )
        }

        val n = 50_000
        val nanos = measureNanoTime {
            repeat(n) { i ->
                // Spread BID prices so we hit different buckets, exercising the radix-tree path.
                orderBook.handleNewOrderCommand(
                    newOrder(
                        UUID.randomUUID().toString(),
                        owner,
                        50L + (i % 100),
                        1,
                        OrderDirection.BID,
                    ),
                )
            }
        }
        report("insert_bid_resting", n, nanos)
    }

    @Test
    fun `bench crossing trades pop top of book`() {
        val orderBook = SimpleOrderBook(pair, false)

        // Pre-populate the ASK side with one resting unit per price level so each crossing
        // BID consumes exactly one. We use distinct owners so self-trade prevention is not
        // a factor.
        val n = 20_000
        for (i in 0 until n) {
            val asker = UUID.randomUUID().toString()
            orderBook.handleNewOrderCommand(
                newOrder(UUID.randomUUID().toString(), asker, 100L + i, 1, OrderDirection.ASK),
            )
        }
        // Warm up.
        for (i in 0 until 1_000) {
            val taker = UUID.randomUUID().toString()
            orderBook.handleNewOrderCommand(
                newOrder(UUID.randomUUID().toString(), taker, 100L + i, 1, OrderDirection.BID),
            )
        }

        // Pre-populate again because warm-up consumed 1k asks.
        for (i in 0 until n) {
            val asker = UUID.randomUUID().toString()
            orderBook.handleNewOrderCommand(
                newOrder(UUID.randomUUID().toString(), asker, 200_000L + i, 1, OrderDirection.ASK),
            )
        }

        val nanos = measureNanoTime {
            for (i in 0 until n) {
                val taker = UUID.randomUUID().toString()
                orderBook.handleNewOrderCommand(
                    newOrder(UUID.randomUUID().toString(), taker, 200_000L + i, 1, OrderDirection.BID),
                )
            }
        }
        report("crossing_trade", n, nanos)
    }

    @Test
    fun `bench cancel resting orders`() {
        val orderBook = SimpleOrderBook(pair, false)
        val orderIds = mutableListOf<Long>()
        val n = 20_000
        for (i in 0 until n) {
            val resting = orderBook.handleNewOrderCommand(
                newOrder(UUID.randomUUID().toString(), owner, 50L + (i % 100), 1, OrderDirection.BID),
            )
            orderIds.add(resting!!.id()!!)
        }

        val nanos = measureNanoTime {
            orderIds.forEach { orderId ->
                orderBook.handleCancelCommand(
                    OrderCancelCommand(UUID.randomUUID().toString(), owner, orderId, pair),
                )
            }
        }
        report("cancel_resting", n, nanos)
    }
}
