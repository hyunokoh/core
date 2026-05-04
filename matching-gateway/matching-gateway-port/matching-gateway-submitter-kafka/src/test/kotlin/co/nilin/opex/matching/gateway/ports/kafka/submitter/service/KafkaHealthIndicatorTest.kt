package co.nilin.opex.matching.gateway.ports.kafka.submitter.service

import org.apache.kafka.clients.admin.AdminClient
import org.apache.kafka.clients.admin.AdminClientConfig
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test
import org.testcontainers.containers.KafkaContainer
import org.testcontainers.utility.DockerImageName

private class KafkaHealthIndicatorTest {

    @Test
    fun givenKafkaClusterWithEnoughNodes_whenCheck_thenMarkHealthy() {
        KafkaContainer(DockerImageName.parse("confluentinc/cp-kafka:7.3.3")).use { kafka ->
            kafka.start()
            AdminClient.create(mapOf(AdminClientConfig.BOOTSTRAP_SERVERS_CONFIG to kafka.bootstrapServers)).use { adminClient ->
                val indicator = KafkaHealthIndicator(adminClient, healthyNodeSize = 1)

                indicator.check()

                assertThat(indicator.isHealthy).isTrue()
            }
        }
    }

    @Test
    fun givenKafkaClusterWithTooFewNodes_whenCheck_thenMarkUnhealthy() {
        KafkaContainer(DockerImageName.parse("confluentinc/cp-kafka:7.3.3")).use { kafka ->
            kafka.start()
            AdminClient.create(mapOf(AdminClientConfig.BOOTSTRAP_SERVERS_CONFIG to kafka.bootstrapServers)).use { adminClient ->
                val indicator = KafkaHealthIndicator(adminClient, healthyNodeSize = 2)

                indicator.check()

                assertThat(indicator.isHealthy).isFalse()
            }
        }
    }
}
