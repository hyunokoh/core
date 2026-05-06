package co.nilin.opex.market.ports.postgres.dao

import co.nilin.opex.market.core.inout.AggregatedOrderPriceModel
import co.nilin.opex.market.core.inout.MatchConstraint
import co.nilin.opex.market.core.inout.MatchingOrderType
import co.nilin.opex.market.core.inout.OrderDirection
import co.nilin.opex.market.ports.postgres.model.OrderModel
import kotlinx.coroutines.flow.Flow
import org.springframework.data.r2dbc.repository.Query
import org.springframework.data.repository.query.Param
import org.springframework.data.repository.reactive.ReactiveCrudRepository
import org.springframework.stereotype.Repository
import reactor.core.publisher.Flux
import reactor.core.publisher.Mono
import java.math.BigDecimal
import java.time.LocalDateTime
import java.util.*

@Repository
interface OrderRepository : ReactiveCrudRepository<OrderModel, Long> {

    @Query(
        """
        insert into orders (
            ouid,
            uuid,
            client_order_id,
            symbol,
            order_id,
            maker_fee,
            taker_fee,
            left_side_fraction,
            right_side_fraction,
            user_level,
            side,
            match_constraint,
            order_type,
            price,
            quantity,
            quote_quantity,
            create_date,
            update_date
        )
        values (
            :ouid,
            :uuid,
            :clientOrderId,
            :symbol,
            :orderId,
            :makerFee,
            :takerFee,
            :leftSideFraction,
            :rightSideFraction,
            :userLevel,
            :side,
            :matchConstraint,
            :orderType,
            :price,
            :quantity,
            :quoteQuantity,
            :createDate,
            :updateDate
        )
        on conflict (ouid) do nothing
        returning *
        """
    )
    fun insertIfAbsent(
        ouid: String,
        uuid: String,
        clientOrderId: String?,
        symbol: String,
        orderId: Long?,
        makerFee: java.math.BigDecimal?,
        takerFee: java.math.BigDecimal?,
        leftSideFraction: java.math.BigDecimal?,
        rightSideFraction: java.math.BigDecimal?,
        userLevel: String?,
        side: OrderDirection,
        matchConstraint: MatchConstraint?,
        orderType: MatchingOrderType?,
        price: java.math.BigDecimal?,
        quantity: java.math.BigDecimal?,
        quoteQuantity: java.math.BigDecimal?,
        createDate: LocalDateTime,
        updateDate: LocalDateTime
    ): Mono<OrderModel>

    @Query("select * from orders where ouid = :ouid")
    fun findByOuid(@Param("ouid") ouid: String): Mono<OrderModel>

    @Query(
        """
        update orders
        set price = :price,
            quantity = :quantity,
            quote_quantity = :quoteQuantity,
            update_date = :updateDate
        where ouid = :ouid
        """
    )
    fun updateOrderDetails(
        @Param("ouid")
        ouid: String,
        @Param("price")
        price: BigDecimal,
        @Param("quantity")
        quantity: BigDecimal,
        @Param("quoteQuantity")
        quoteQuantity: BigDecimal,
        @Param("updateDate")
        updateDate: LocalDateTime
    ): Mono<Int>

    @Query("select * from orders where uuid = :uuid and ouid = :ouid")
    fun findByUUIDAndOUID(@Param("uuid") uuid: String, @Param("ouid") ouid: String): Mono<OrderModel>

    @Query("select * from orders where symbol = :symbol and order_id = :orderId")
    fun findBySymbolAndOrderId(
        @Param("symbol")
        symbol: String, @Param("orderId")
        orderId: Long
    ): Mono<OrderModel>

    @Query(
        """
        with latest_status as (
            select distinct on (ouid)
                ouid,
                status,
                date
            from order_status
            order by ouid, date desc, id desc
        )
        select orders.*
        from orders
        left join latest_status on latest_status.ouid = orders.ouid
        where uuid = :uuid
          and symbol = :symbol
          and client_order_id = :origClientOrderId
        order by
          case when latest_status.status in (1, 4) then 0 else 1 end,
          orders.create_date desc,
          orders.id desc
        limit 1
        """
    )
    fun findByUuidAndSymbolAndClientOrderId(
        @Param("uuid")
        uuid: String,
        @Param("symbol")
        symbol: String,
        @Param("origClientOrderId")
        origClientOrderId: String
    ): Mono<OrderModel>

    @Query(
        """
        select * from orders
        join open_orders oo on orders.ouid = oo.ouid
        where uuid = :uuid and (:symbol is null or symbol = :symbol) and status in (:statuses)
        order by create_date desc
        limit :limit
    """
    )
    fun findByUuidAndSymbolAndStatus(
        @Param("uuid")
        uuid: String,
        @Param("symbol")
        symbol: String?,
        @Param("statuses")
        status: Collection<Int>,
        limit: Int
    ): Flow<OrderModel>

    @Query(
        """
        select * from orders where uuid = :uuid
            and (:symbol is null or symbol = :symbol)
            and (:startTime is null or update_date >= :startTime)
            and (:endTime is null or update_date < :endTime)
        order by update_date DESC 
        limit :limit
        """
    )
    fun findByUuidAndSymbolAndTimeBetween(
        @Param("uuid")
        uuid: String,
        @Param("symbol")
        symbol: String?,
        @Param("startTime")
        startTime: Date?,
        @Param("endTime")
        endTime: Date?,
        limit: Int
    ): Flow<OrderModel>

    @Query(
        """
        select price, (sum(quantity) - sum(oo.executed_quantity)) as quantity from orders 
        join open_orders oo on orders.ouid = oo.ouid
        where symbol = :symbol and side = :direction and status in (:statuses) 
        group by price 
        order by price asc
        limit :limit
    """
    )
    fun findBySymbolAndDirectionAndStatusSortAscendingByPrice(
        @Param("symbol")
        symbol: String,
        @Param("direction")
        direction: OrderDirection,
        @Param("limit")
        limit: Int,
        @Param("statuses")
        status: Collection<Int>
    ): Flux<AggregatedOrderPriceModel>

    @Query(
        """
        select price, (sum(quantity) - sum(oo.executed_quantity)) as quantity from orders 
        join open_orders oo on orders.ouid = oo.ouid
        where symbol = :symbol and side = :direction and status in (:statuses) 
        group by price 
        order by price desc
        limit :limit
    """
    )
    fun findBySymbolAndDirectionAndStatusSortDescendingByPrice(
        @Param("symbol")
        symbol: String,
        @Param("direction")
        direction: OrderDirection,
        @Param("limit")
        limit: Int,
        @Param("statuses")
        status: Collection<Int>
    ): Flux<AggregatedOrderPriceModel>

    @Query("select * from orders where symbol = :symbol order by create_date desc limit 1")
    fun findLastOrderBySymbol(@Param("symbol") symbol: String): Mono<OrderModel>

    @Query("select count(distinct uuid) from orders where create_date >= :interval")
    fun countUsersWhoMadeOrder(interval: LocalDateTime): Flow<Long>

    @Query("select count(*) from orders where create_date >= :interval")
    fun countNewerThan(interval: LocalDateTime): Flow<Long>

    @Query("select count(*) from orders where symbol = :symbol and create_date >= :interval")
    fun countBySymbolNewerThan(interval: LocalDateTime, symbol: String): Flow<Long>
}
