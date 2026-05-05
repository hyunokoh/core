package co.nilin.opex.accountant.ports.kafka.listener.config

import org.apache.kafka.clients.producer.ProducerConfig
import org.apache.kafka.common.serialization.StringSerializer
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import org.springframework.kafka.support.serializer.JsonDeserializer
import org.springframework.kafka.support.serializer.JsonSerializer

class AccountantKafkaConfigTest {

    @Test
    fun givenAccountantKafkaConfig_whenProducerConfigsCreated_thenUseSerializerSettingsForDltPublishing() {
        val config = AccountantKafkaConfig()
        val bootstrapServersField = AccountantKafkaConfig::class.java.getDeclaredField("bootstrapServers")
        bootstrapServersField.isAccessible = true
        bootstrapServersField.set(config, "localhost:9092")

        val producerConfigs = config.producerConfigs()

        assertThat(producerConfigs[ProducerConfig.BOOTSTRAP_SERVERS_CONFIG]).isEqualTo("localhost:9092")
        assertThat(producerConfigs[ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG]).isEqualTo(StringSerializer::class.java)
        assertThat(producerConfigs[ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG]).isEqualTo(JsonSerializer::class.java)
        assertThat(producerConfigs[ProducerConfig.ACKS_CONFIG]).isEqualTo("all")
        assertThat(producerConfigs[JsonSerializer.TYPE_MAPPINGS].toString())
            .contains("kyc_level_updated_event")
            .contains("fiAction_response_event")
    }

    @Test
    fun givenAccountantKafkaConfig_whenConsumerConfigsCreated_thenDeserializeOrderEditRequests() {
        val config = AccountantKafkaConfig()
        val bootstrapServersField = AccountantKafkaConfig::class.java.getDeclaredField("bootstrapServers")
        bootstrapServersField.isAccessible = true
        bootstrapServersField.set(config, "localhost:9092")
        val groupIdField = AccountantKafkaConfig::class.java.getDeclaredField("groupId")
        groupIdField.isAccessible = true
        groupIdField.set(config, "accountant")

        val consumerConfigs = config.consumerConfigs()

        assertThat(consumerConfigs[JsonDeserializer.TYPE_MAPPINGS].toString())
            .contains("order_request_submit")
            .contains("order_request_cancel")
            .contains("order_request_edit")
            .contains("OrderEditRequestEvent")
    }
}
