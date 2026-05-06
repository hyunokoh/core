package co.nilin.opex.market.ports.postgres.dao

import co.nilin.opex.market.ports.postgres.model.OrderStatusModel
import org.springframework.data.r2dbc.repository.Query
import org.springframework.data.repository.reactive.ReactiveCrudRepository
import org.springframework.stereotype.Repository
import reactor.core.publisher.Mono
import java.math.BigDecimal
import java.time.LocalDateTime

@Repository
interface OrderStatusRepository : ReactiveCrudRepository<OrderStatusModel, Long> {

    @Query(
        """
        insert into order_status (ouid, executed_quantity, accumulative_quote_qty, status, appearance, date) 
        values (:ouid, :executedQuantity, :accumulativeQuoteQuantity, :status, :appearance, :date)
        on conflict do nothing
    """
    )
    fun insert(
        ouid: String,
        executedQuantity: BigDecimal,
        accumulativeQuoteQuantity: BigDecimal,
        status: Int,
        appearance: Int,
        date: LocalDateTime = LocalDateTime.now()
    ): Mono<Void>

    @Query(
        """
        WITH ranked_order_status AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY ouid ORDER BY appearance DESC, executed_quantity DESC) AS rnk
            FROM order_status
            WHERE ouid = :ouid
        ),
        status_totals AS (
            SELECT
                ouid,
                COALESCE(
                    MAX(CASE WHEN appearance > 1 THEN executed_quantity END),
                    MAX(executed_quantity)
                ) AS executed_quantity,
                COALESCE(
                    MAX(CASE WHEN appearance > 1 THEN accumulative_quote_qty END),
                    MAX(accumulative_quote_qty)
                ) AS accumulative_quote_qty
            FROM order_status
            WHERE ouid = :ouid
            GROUP BY ouid
        )
        SELECT
            ranked_order_status.ouid,
            status_totals.executed_quantity,
            status_totals.accumulative_quote_qty,
            ranked_order_status.status,
            ranked_order_status.appearance,
            ranked_order_status.date,
            ranked_order_status.id
        FROM ranked_order_status
        JOIN status_totals ON status_totals.ouid = ranked_order_status.ouid
        WHERE ranked_order_status.rnk = 1;
        """
    )
    fun findMostRecentByOUID(ouid: String): Mono<OrderStatusModel>

}
