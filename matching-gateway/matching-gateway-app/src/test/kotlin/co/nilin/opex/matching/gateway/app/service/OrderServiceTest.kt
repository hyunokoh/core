package co.nilin.opex.matching.gateway.app.service

import co.nilin.opex.common.OpexError
import co.nilin.opex.matching.engine.core.model.MatchConstraint
import co.nilin.opex.matching.engine.core.model.OrderDirection
import co.nilin.opex.matching.engine.core.model.OrderType
import co.nilin.opex.matching.gateway.app.inout.CancelOrderRequest
import co.nilin.opex.matching.gateway.app.inout.PairConfig
import co.nilin.opex.matching.gateway.app.service.sample.VALID
import co.nilin.opex.matching.gateway.app.spi.AccountantApiProxy
import co.nilin.opex.matching.gateway.app.spi.PairConfigLoader
import co.nilin.opex.matching.gateway.ports.kafka.submitter.inout.OrderRequestEvent
import co.nilin.opex.matching.gateway.ports.kafka.submitter.service.KafkaHealthIndicator
import co.nilin.opex.matching.gateway.ports.kafka.submitter.service.OrderRequestEventSubmitter
import co.nilin.opex.utility.error.data.OpexException
import kotlinx.coroutines.runBlocking
import org.apache.kafka.clients.admin.AdminClient
import org.apache.kafka.clients.admin.AdminClientConfig
import org.apache.kafka.clients.admin.NewTopic
import org.apache.kafka.clients.consumer.KafkaConsumer
import org.apache.kafka.clients.consumer.ConsumerConfig
import org.apache.kafka.common.TopicPartition
import org.apache.kafka.common.serialization.StringDeserializer
import org.apache.kafka.common.serialization.StringSerializer
import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.AfterAll
import org.junit.jupiter.api.BeforeAll
import org.junit.jupiter.api.Test
import org.springframework.kafka.core.DefaultKafkaProducerFactory
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.kafka.support.serializer.JsonSerializer
import org.testcontainers.containers.KafkaContainer
import org.testcontainers.utility.DockerImageName
import java.math.BigDecimal
import java.time.Duration

private class OrderServiceTest {

    @Test
    fun givenPair_whenSubmitNewAskOrder_thenPublishesOrderToKafka(): Unit = runBlocking {
        val service = orderService()

        val result = consumer().use { kafkaConsumer ->
            val result = service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK)
            val recordValue = nextRecordValue(kafkaConsumer)

            assertThat(recordValue).contains("\"direction\":\"ASK\"")
            assertThat(recordValue).contains("\"price\":10000000")
            assertThat(recordValue).contains("\"quantity\":10")
            result
        }

        assertThat(result).isNotNull
    }

    @Test
    fun givenMarketAskWithZeroPrice_whenSubmitNewOrder_thenPublishesOrderToKafka(): Unit = runBlocking {
        val service = orderService()

        consumer().use { kafkaConsumer ->
            service.submitNewOrder(
                VALID.CREATE_ORDER_REQUEST_ASK.copy(
                    price = BigDecimal.ZERO,
                    matchConstraint = MatchConstraint.IOC,
                    orderType = OrderType.MARKET_ORDER
                )
            )
            val recordValue = nextRecordValue(kafkaConsumer)

            assertThat(recordValue).contains("\"orderType\":\"MARKET_ORDER\"")
            assertThat(recordValue).contains("\"price\":0")
        }
    }

    @Test
    fun givenMarketBidWithZeroPrice_whenSubmitNewOrder_thenThrowBadRequestBeforeAccountantCheck(): Unit = runBlocking {
        val accountant = RecordingAccountantApiProxy()
        val service = orderService(accountant)

        assertThatThrownBy {
            runBlocking {
                service.submitNewOrder(
                    VALID.CREATE_ORDER_REQUEST_BID.copy(
                        price = BigDecimal.ZERO,
                        orderType = OrderType.MARKET_ORDER
                    )
                )
            }
        }.isBadRequest()

        assertThat(accountant.lastSymbol).isNull()
        assertThat(accountant.lastValue).isNull()
    }

    @Test
    fun givenMarketOrderWithGtcConstraint_whenSubmitNewOrder_thenThrowBadRequestBeforeAccountantCheck(): Unit = runBlocking {
        val accountant = RecordingAccountantApiProxy()
        val service = orderService(accountant)

        assertThatThrownBy {
            runBlocking {
                service.submitNewOrder(
                    VALID.CREATE_ORDER_REQUEST_ASK.copy(
                        price = BigDecimal.ZERO,
                        matchConstraint = MatchConstraint.GTC,
                        orderType = OrderType.MARKET_ORDER
                    )
                )
            }
        }.isBadRequest()

        assertThat(accountant.lastSymbol).isNull()
        assertThat(accountant.lastValue).isNull()
    }

    @Test
    fun givenUnsupportedMatchConstraint_whenSubmitNewOrder_thenThrowBadRequestBeforeAccountantCheck(): Unit = runBlocking {
        val accountant = RecordingAccountantApiProxy()
        val service = orderService(accountant)

        assertThatThrownBy {
            runBlocking {
                service.submitNewOrder(
                    VALID.CREATE_ORDER_REQUEST_ASK.copy(matchConstraint = MatchConstraint.FOK)
                )
            }
        }.isBadRequest()

        assertThat(accountant.lastSymbol).isNull()
        assertThat(accountant.lastValue).isNull()
    }

    @Test
    fun givenPair_whenSubmitNewBidOrder_thenChecksRightSideAmountAndPublishesOrderToKafka(): Unit = runBlocking {
        val accountant = RecordingAccountantApiProxy()
        val service = orderService(accountant)

        consumer().use { kafkaConsumer ->
            service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_BID)
            val recordValue = nextRecordValue(kafkaConsumer)

            assertThat(recordValue).contains("\"direction\":\"BID\"")
        }

        assertThat(accountant.lastSymbol).isEqualTo(VALID.USDT)
        assertThat(accountant.lastValue).isEqualByComparingTo(BigDecimal("100.000"))
    }

    @Test
    fun givenPair_whenSubmitNewOrderByInvalidSymbol_thenThrow(): Unit = runBlocking {
        val service = orderService(pairConfigLoader = RecordingPairConfigLoader(allowPair = false))

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK.copy(pair = "BTC_ETH")) }
        }.isInstanceOf(IllegalStateException::class.java)
    }

    @Test
    fun givenPair_whenSubmitNewOrderByASKAndInvalidPrice_thenThrow(): Unit = runBlocking {
        val service = orderService()

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK.copy(price = BigDecimal.valueOf(-100000))) }
        }.isBadRequest()

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK.copy(price = BigDecimal.ZERO)) }
        }.isBadRequest()
    }

    @Test
    fun givenPair_whenSubmitNewOrderByASKAndInvalidQuantity_thenThrow(): Unit = runBlocking {
        val service = orderService()

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK.copy(quantity = BigDecimal.valueOf(-0.001))) }
        }.isBadRequest()
    }

    @Test
    fun givenPair_whenOrderPrecisionDoesNotMatchPairConfig_thenThrowBadRequestBeforeKafkaPublish(): Unit = runBlocking {
        val service = orderService()

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK.copy(price = BigDecimal("100000.00001"))) }
        }.isBadRequest()

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK.copy(quantity = BigDecimal("0.00101"))) }
        }.isBadRequest()
    }

    @Test
    fun givenPair_whenSubmitNewOrderByBIDAndNotAllowed_thenThrow(): Unit = runBlocking {
        val service = orderService(RecordingAccountantApiProxy(allowed = false))

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_BID) }
        }.isOpexError(OpexError.SubmitOrderForbiddenByAccountant)
    }

    @Test
    fun givenAccountantUnavailable_whenSubmitNewOrder_thenThrowServiceUnavailable(): Unit = runBlocking {
        val service = orderService(RecordingAccountantApiProxy(error = RuntimeException("accountant unavailable")))

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_BID) }
        }.isOpexError(OpexError.ServiceUnavailable)
    }

    @Test
    fun givenMalformedPairOrMissingUser_whenSubmitNewOrder_thenThrowBeforeKafkaPublish(): Unit = runBlocking {
        val service = orderService()

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK.copy(pair = "ETHUSDT")) }
        }.isBadRequest()

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK.copy(pair = "_USDT")) }
        }.isBadRequest()

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK.copy(uuid = " ")) }
        }.isBadRequest()
    }

    @Test
    fun givenOrder_whenCancelOrder_thenPublishesCancelToKafka(): Unit = runBlocking {
        val service = orderService()

        consumer().use { kafkaConsumer ->
            service.cancelOrder(VALID.CANCEL_ORDER_REQUEST)
            val recordValue = nextRecordValue(kafkaConsumer)

            assertThat(recordValue).contains("\"orderId\":1")
            assertThat(recordValue).contains(VALID.OUID)
        }
    }

    @Test
    fun givenInconsistentPairConfig_whenSubmitNewOrder_thenThrowServiceUnavailableBeforeAccountantCheck(): Unit = runBlocking {
        val accountant = RecordingAccountantApiProxy()
        val mismatchedConfig = PairConfig(
            "BTC_USDT",
            VALID.ETH,
            VALID.USDT,
            BigDecimal.valueOf(0.01),
            BigDecimal.valueOf(0.0001)
        )
        val service = orderService(accountant, RecordingPairConfigLoader(pairConfig = mismatchedConfig))

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK) }
        }.isOpexError(OpexError.ServiceUnavailable)

        assertThat(accountant.lastSymbol).isNull()
        assertThat(accountant.lastValue).isNull()
    }

    @Test
    fun givenInvalidPairFractions_whenSubmitNewOrder_thenThrowServiceUnavailableBeforeAccountantCheck(): Unit = runBlocking {
        val accountant = RecordingAccountantApiProxy()
        val invalidConfig = PairConfig(
            VALID.ETH_USDT,
            VALID.ETH,
            VALID.USDT,
            BigDecimal.ZERO,
            BigDecimal.valueOf(0.0001)
        )
        val service = orderService(accountant, RecordingPairConfigLoader(pairConfig = invalidConfig))

        assertThatThrownBy {
            runBlocking { service.submitNewOrder(VALID.CREATE_ORDER_REQUEST_ASK) }
        }.isOpexError(OpexError.ServiceUnavailable)

        assertThat(accountant.lastSymbol).isNull()
        assertThat(accountant.lastValue).isNull()
    }

    @Test
    fun givenKafkaUnhealthy_whenCancelOrder_thenThrowServiceUnavailable(): Unit = runBlocking {
        val unhealthyIndicator = KafkaHealthIndicator(adminClient, healthyNodeSize = 2)
        unhealthyIndicator.check()
        val service = orderService(healthIndicator = unhealthyIndicator)

        assertThatThrownBy {
            runBlocking { service.cancelOrder(VALID.CANCEL_ORDER_REQUEST) }
        }.isInstanceOf(OpexException::class.java)
            .extracting("error")
            .isEqualTo(OpexError.ServiceUnavailable)
    }

    @Test
    fun givenInvalidCancelRequest_whenCancelOrder_thenThrowBeforeKafkaPublish(): Unit = runBlocking {
        val service = orderService()

        assertThatThrownBy {
            runBlocking { service.cancelOrder(CancelOrderRequest(VALID.OUID, VALID.UUID, 1, "ETHUSDT")) }
        }.isBadRequest()

        assertThatThrownBy {
            runBlocking { service.cancelOrder(CancelOrderRequest(VALID.OUID, VALID.UUID, -1, VALID.ETH_USDT)) }
        }.isBadRequest()

        assertThatThrownBy {
            runBlocking { service.cancelOrder(CancelOrderRequest("", VALID.UUID, 1, VALID.ETH_USDT)) }
        }.isBadRequest()

        assertThatThrownBy {
            runBlocking { service.cancelOrder(CancelOrderRequest(VALID.OUID, " ", 1, VALID.ETH_USDT)) }
        }.isBadRequest()
    }

    private fun orderService(
        accountantApiProxy: RecordingAccountantApiProxy = RecordingAccountantApiProxy(),
        pairConfigLoader: PairConfigLoader = RecordingPairConfigLoader(),
        healthIndicator: KafkaHealthIndicator = Companion.healthIndicator
    ): OrderService {
        healthIndicator.check()
        return OrderService(accountantApiProxy, orderSubmitter, pairConfigLoader, healthIndicator)
    }

    private fun consumer(): KafkaConsumer<String, String> =
        KafkaConsumer<String, String>(
            mapOf(
                ConsumerConfig.BOOTSTRAP_SERVERS_CONFIG to kafka.bootstrapServers,
                ConsumerConfig.GROUP_ID_CONFIG to "matching-gateway-test-${System.nanoTime()}",
                ConsumerConfig.AUTO_OFFSET_RESET_CONFIG to "latest",
                ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG to false,
                ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG to StringDeserializer::class.java,
                ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG to StringDeserializer::class.java
            )
        ).apply {
            assign(listOf(ordersTopicPartition))
            seekToEnd(listOf(ordersTopicPartition))
            position(ordersTopicPartition)
        }

    private fun nextRecordValue(kafkaConsumer: KafkaConsumer<String, String>): String {
        val deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos()
        while (System.nanoTime() < deadline) {
            val records = kafkaConsumer.poll(Duration.ofMillis(250)).records(ordersTopicPartition)
            if (!records.isEmpty())
                return records.first().value()
        }
        error("No records found for $ORDERS_TOPIC at offset ${kafkaConsumer.position(ordersTopicPartition)}")
    }

    private fun org.assertj.core.api.AbstractThrowableAssert<*, out Throwable>.isBadRequest() {
        isOpexError(OpexError.BadRequest)
    }

    private fun org.assertj.core.api.AbstractThrowableAssert<*, out Throwable>.isOpexError(error: OpexError) {
        isInstanceOf(OpexException::class.java)
            .extracting("error")
            .isEqualTo(error)
    }

    private class RecordingAccountantApiProxy(
        private val allowed: Boolean = true,
        private val error: RuntimeException? = null
    ) : AccountantApiProxy {
        var lastSymbol: String? = null
        var lastValue: BigDecimal? = null

        override suspend fun canCreateOrder(uuid: String, symbol: String, value: BigDecimal): Boolean {
            error?.let { throw it }
            lastSymbol = symbol
            lastValue = value
            return allowed
        }

        override suspend fun fetchPairConfig(pair: String, direction: OrderDirection): PairConfig = VALID.PAIR_CONFIG
    }

    private class RecordingPairConfigLoader(
        private val allowPair: Boolean = true,
        private val pairConfig: PairConfig = VALID.PAIR_CONFIG
    ) : PairConfigLoader {
        override suspend fun load(pair: String, direction: OrderDirection): PairConfig {
            check(allowPair && pair == VALID.ETH_USDT) { "Unknown pair: $pair" }
            return pairConfig
        }
    }

    companion object {
        private val kafka = KafkaContainer(DockerImageName.parse("confluentinc/cp-kafka:7.3.3"))
        private lateinit var kafkaTemplate: KafkaTemplate<String, OrderRequestEvent>
        private lateinit var adminClient: AdminClient
        private lateinit var orderSubmitter: OrderRequestEventSubmitter
        private lateinit var healthIndicator: KafkaHealthIndicator

        @JvmStatic
        @BeforeAll
        fun startKafka() {
            kafka.start()
            kafkaTemplate = KafkaTemplate(
                DefaultKafkaProducerFactory(
                    mapOf(
                        org.apache.kafka.clients.producer.ProducerConfig.BOOTSTRAP_SERVERS_CONFIG to kafka.bootstrapServers,
                        org.apache.kafka.clients.producer.ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG to StringSerializer::class.java,
                        org.apache.kafka.clients.producer.ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG to JsonSerializer::class.java,
                        org.apache.kafka.clients.producer.ProducerConfig.ACKS_CONFIG to "all",
                    )
                )
            )
            adminClient = AdminClient.create(mapOf(AdminClientConfig.BOOTSTRAP_SERVERS_CONFIG to kafka.bootstrapServers))
            adminClient.createTopics(listOf(NewTopic(ORDERS_TOPIC, 1, 1))).all().get()
            orderSubmitter = OrderRequestEventSubmitter(kafkaTemplate)
            healthIndicator = KafkaHealthIndicator(adminClient, healthyNodeSize = 1)
        }

        @JvmStatic
        @AfterAll
        fun stopKafka() {
            kafkaTemplate.destroy()
            adminClient.close()
            kafka.stop()
        }

        private const val ORDERS_TOPIC = "orders_ETH_USDT"
        private val ordersTopicPartition = TopicPartition(ORDERS_TOPIC, 0)
    }
}
