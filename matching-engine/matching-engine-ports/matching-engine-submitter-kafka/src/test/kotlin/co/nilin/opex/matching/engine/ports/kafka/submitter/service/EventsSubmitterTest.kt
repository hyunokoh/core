package co.nilin.opex.matching.engine.ports.kafka.submitter.service

import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import co.nilin.opex.matching.engine.core.eventh.events.CreateOrderEvent
import co.nilin.opex.matching.engine.core.eventh.events.TradeEvent
import co.nilin.opex.matching.engine.core.model.Pair
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.apache.kafka.clients.admin.AdminClient
import org.apache.kafka.clients.admin.AdminClientConfig
import org.apache.kafka.clients.admin.NewTopic
import org.apache.kafka.clients.consumer.ConsumerConfig
import org.apache.kafka.clients.consumer.KafkaConsumer
import org.apache.kafka.common.TopicPartition
import org.apache.kafka.common.serialization.StringDeserializer
import org.apache.kafka.common.serialization.StringSerializer
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.AfterAll
import org.junit.jupiter.api.BeforeAll
import org.junit.jupiter.api.Test
import org.springframework.kafka.core.DefaultKafkaProducerFactory
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.kafka.support.serializer.JsonSerializer
import org.testcontainers.containers.KafkaContainer
import org.testcontainers.utility.DockerImageName
import java.time.Duration

private class EventsSubmitterTest {

    @Test
    fun givenCreateOrderEvent_whenSubmit_thenPublishToEventsTopicAndComplete(): Unit = runBlocking {
        val event = CreateOrderEvent(ouid = "order-1", uuid = "user-1", pair = createOrderPair)

        withTimeout(Duration.ofSeconds(5).toMillis()) {
            submitter.submit(event)
        }

        consumer(createEventsTopicPartition).use { kafkaConsumer ->
            val recordValue = nextRecordValue(kafkaConsumer, createEventsTopicPartition)
            assertThat(recordValue).contains("\"ouid\":\"order-1\"")
        }
        consumer(createTradesTopicPartition).use { kafkaConsumer ->
            assertThat(nextRecordValueOrNull(kafkaConsumer, createTradesTopicPartition)).isNull()
        }
    }

    @Test
    fun givenTradeEvent_whenSubmit_thenPublishToTradeAndEventTopicsAndComplete(): Unit = runBlocking {
        val event = TradeEvent(tradeId = 7, pair = tradePair, takerOuid = "taker-1", makerOuid = "maker-1")

        withTimeout(Duration.ofSeconds(5).toMillis()) {
            submitter.submit(event)
        }

        consumer(tradeTradesTopicPartition).use { kafkaConsumer ->
            val recordValue = nextRecordValue(kafkaConsumer, tradeTradesTopicPartition)
            assertThat(recordValue).contains("\"tradeId\":7")
        }
        consumer(tradeEventsTopicPartition).use { kafkaConsumer ->
            val recordValue = nextRecordValue(kafkaConsumer, tradeEventsTopicPartition)
            assertThat(recordValue).contains("\"tradeId\":7")
        }
    }

    private fun consumer(topicPartition: TopicPartition): KafkaConsumer<String, String> =
        KafkaConsumer<String, String>(
            mapOf(
                ConsumerConfig.BOOTSTRAP_SERVERS_CONFIG to kafka.bootstrapServers,
                ConsumerConfig.GROUP_ID_CONFIG to "matching-engine-submit-${System.nanoTime()}",
                ConsumerConfig.AUTO_OFFSET_RESET_CONFIG to "earliest",
                ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG to false,
                ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG to StringDeserializer::class.java,
                ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG to StringDeserializer::class.java
            )
        ).apply {
            assign(listOf(topicPartition))
            seekToBeginning(listOf(topicPartition))
        }

    private fun nextRecordValue(kafkaConsumer: KafkaConsumer<String, String>, topicPartition: TopicPartition): String =
        nextRecordValueOrNull(kafkaConsumer, topicPartition)
            ?: error("No records found for ${topicPartition.topic()} at offset ${kafkaConsumer.position(topicPartition)}")

    private fun nextRecordValueOrNull(
        kafkaConsumer: KafkaConsumer<String, String>,
        topicPartition: TopicPartition
    ): String? {
        val deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos()
        while (System.nanoTime() < deadline) {
            val records = kafkaConsumer.poll(Duration.ofMillis(250)).records(topicPartition)
            if (!records.isEmpty())
                return records.first().value()
        }
        return null
    }

    companion object {
        private val createOrderPair = Pair("ETH", "USDT")
        private val tradePair = Pair("BTC", "USDT")
        private const val CREATE_EVENTS_TOPIC = "events_ETH_USDT"
        private const val CREATE_TRADES_TOPIC = "trades_ETH_USDT"
        private const val TRADE_EVENTS_TOPIC = "events_BTC_USDT"
        private const val TRADE_TRADES_TOPIC = "trades_BTC_USDT"
        private val createEventsTopicPartition = TopicPartition(CREATE_EVENTS_TOPIC, 0)
        private val createTradesTopicPartition = TopicPartition(CREATE_TRADES_TOPIC, 0)
        private val tradeEventsTopicPartition = TopicPartition(TRADE_EVENTS_TOPIC, 0)
        private val tradeTradesTopicPartition = TopicPartition(TRADE_TRADES_TOPIC, 0)
        private val kafka = KafkaContainer(DockerImageName.parse("confluentinc/cp-kafka:7.3.3"))
        private lateinit var kafkaTemplate: KafkaTemplate<String, CoreEvent>
        private lateinit var submitter: EventsSubmitter

        @JvmStatic
        @BeforeAll
        fun startKafka() {
            kafka.start()
            AdminClient.create(mapOf(AdminClientConfig.BOOTSTRAP_SERVERS_CONFIG to kafka.bootstrapServers)).use { adminClient ->
                adminClient.createTopics(
                    listOf(
                        NewTopic(CREATE_EVENTS_TOPIC, 1, 1),
                        NewTopic(CREATE_TRADES_TOPIC, 1, 1),
                        NewTopic(TRADE_EVENTS_TOPIC, 1, 1),
                        NewTopic(TRADE_TRADES_TOPIC, 1, 1)
                    )
                ).all().get()
            }
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
            submitter = EventsSubmitter(kafkaTemplate)
        }

        @JvmStatic
        @AfterAll
        fun stopKafka() {
            kafkaTemplate.destroy()
            kafka.stop()
        }
    }
}
