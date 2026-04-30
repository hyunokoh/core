package co.nilin.opex.accountant.ports.kafka.submitter

import co.nilin.opex.accountant.core.inout.RichOrderEvent
import co.nilin.opex.accountant.core.inout.RichTrade
import co.nilin.opex.accountant.ports.kafka.submitter.service.RichOrderSubmitter
import co.nilin.opex.accountant.ports.kafka.submitter.service.RichTradeSubmitter
import co.nilin.opex.accountant.ports.kafka.submitter.service.TempEventSubmitter
import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import kotlinx.coroutines.runBlocking
import org.apache.kafka.clients.consumer.Consumer
import org.apache.kafka.clients.consumer.ConsumerConfig
import org.apache.kafka.clients.producer.ProducerConfig
import org.apache.kafka.common.serialization.ByteArrayDeserializer
import org.apache.kafka.common.serialization.StringDeserializer
import org.apache.kafka.common.serialization.StringSerializer
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.kafka.core.DefaultKafkaProducerFactory
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.kafka.support.serializer.JsonSerializer
import org.springframework.kafka.test.EmbeddedKafkaBroker
import org.springframework.kafka.test.utils.KafkaTestUtils
import java.util.UUID

class EventPublishersTest {

    private val embeddedKafka = EmbeddedKafkaBroker(1, true, 1, "richOrder", "richTrade", "tempevents")
    private val templates = mutableListOf<KafkaTemplate<*, *>>()

    @BeforeEach
    fun startKafka() {
        embeddedKafka.afterPropertiesSet()
    }

    @AfterEach
    fun stopKafka() {
        templates.forEach { it.destroy() }
        templates.clear()
        embeddedKafka.destroy()
    }

    @Test
    fun givenSubmitters_validateTopics() {
        assertThat(RichOrderSubmitter(kafkaTemplate<RichOrderEvent>()).topic).isEqualTo("richOrder")
        assertThat(RichTradeSubmitter(kafkaTemplate<RichTrade>()).topic).isEqualTo("richTrade")
        assertThat(TempEventSubmitter(kafkaTemplate<CoreEvent>()).topic).isEqualTo("tempevents")
    }

    @Test
    fun givenRichOrderSubmitter_whenPublish_writesToKafkaTopic(): Unit = runBlocking {
        val consumer = byteConsumer("richOrder")
        val submitter = RichOrderSubmitter(kafkaTemplate())

        submitter.publish(Valid.testRichOrder)

        val record = KafkaTestUtils.getSingleRecord(consumer, submitter.topic, 5_000L)
        assertThat(record.value()).isNotEmpty
        consumer.close()
    }

    @Test
    fun givenTradeOrderSubmitter_whenPublish_writesToKafkaTopic(): Unit = runBlocking {
        val consumer = byteConsumer("richTrade")
        val submitter = RichTradeSubmitter(kafkaTemplate())

        submitter.publish(Valid.richTrade)

        val record = KafkaTestUtils.getSingleRecord(consumer, submitter.topic, 5_000L)
        assertThat(record.value()).isNotEmpty
        consumer.close()
    }

    @Test
    fun givenTempEventSubmitter_whenRepublish_writesEveryEventToKafkaTopic(): Unit = runBlocking {
        val consumer = byteConsumer("tempevents")
        val submitter = TempEventSubmitter(kafkaTemplate())

        submitter.republish(listOf(Valid.testCoreEvent, Valid.testCoreEvent))

        val records = KafkaTestUtils.getRecords(consumer, 5_000L).records(submitter.topic)
        assertThat(records).hasSize(2)
        records.forEach { assertThat(it.value()).isNotEmpty }
        consumer.close()
    }

    private fun <T> kafkaTemplate(): KafkaTemplate<String, T> {
        val props = KafkaTestUtils.producerProps(embeddedKafka)
        props[ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG] = StringSerializer::class.java
        props[ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG] = JsonSerializer::class.java
        return KafkaTemplate(DefaultKafkaProducerFactory<String, T>(props)).also { templates.add(it) }
    }

    private fun byteConsumer(topic: String): Consumer<String, ByteArray> {
        val props = KafkaTestUtils.consumerProps(UUID.randomUUID().toString(), "true", embeddedKafka)
        props[ConsumerConfig.AUTO_OFFSET_RESET_CONFIG] = "earliest"
        props[ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG] = StringDeserializer::class.java
        props[ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG] = ByteArrayDeserializer::class.java
        val consumer = org.springframework.kafka.core.DefaultKafkaConsumerFactory<String, ByteArray>(props).createConsumer()
        embeddedKafka.consumeFromAnEmbeddedTopic(consumer, topic)
        return consumer
    }
}
