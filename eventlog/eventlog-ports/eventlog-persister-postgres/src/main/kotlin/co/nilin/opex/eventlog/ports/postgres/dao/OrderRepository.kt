package co.nilin.opex.eventlog.ports.postgres.dao

import co.nilin.opex.eventlog.ports.postgres.model.OrderModel
import org.springframework.data.r2dbc.repository.Query
import org.springframework.data.repository.reactive.ReactiveCrudRepository
import org.springframework.stereotype.Repository
import reactor.core.publisher.Mono
import java.time.LocalDateTime

@Repository
interface OrderRepository : ReactiveCrudRepository<OrderModel, Long> {

    @Query(
        """
        insert into opex_orders (
            ouid,
            symbol,
            direction,
            match_constraint,
            order_type,
            uuid,
            agent,
            ip,
            order_date,
            create_date
        )
        values (
            :ouid,
            :symbol,
            :direction,
            :matchConstraint,
            :orderType,
            :uuid,
            :agent,
            :ip,
            :orderDate,
            :createDate
        )
        on conflict (ouid) do nothing
        returning *
        """
    )
    fun insertIfAbsent(
        ouid: String,
        symbol: String,
        direction: String,
        matchConstraint: String,
        orderType: String,
        uuid: String,
        agent: String,
        ip: String,
        orderDate: LocalDateTime,
        createDate: LocalDateTime
    ): Mono<OrderModel>
}
