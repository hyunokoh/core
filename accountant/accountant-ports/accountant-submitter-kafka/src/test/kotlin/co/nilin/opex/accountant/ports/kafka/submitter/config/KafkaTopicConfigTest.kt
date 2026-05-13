package co.nilin.opex.accountant.ports.kafka.submitter.config

import org.apache.kafka.clients.admin.NewTopic
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import org.springframework.context.support.GenericApplicationContext
import org.springframework.test.util.ReflectionTestUtils

class KafkaTopicConfigTest {

    @Test
    fun `rich topics honor configurable partition and replica counts`() {
        val context = GenericApplicationContext()
        val config = KafkaTopicConfig()

        ReflectionTestUtils.setField(config, "partitionCount", 1)
        ReflectionTestUtils.setField(config, "replicaCount", 1)
        ReflectionTestUtils.setField(config, "minSyncReplicaCount", "1")

        config.createTopics(context)
        context.refresh()

        val richOrder = context.getBean("topic_richOrder", NewTopic::class.java)
        val richTrade = context.getBean("topic_richTrade", NewTopic::class.java)

        assertEquals(1, richOrder.numPartitions())
        assertEquals(1.toShort(), richOrder.replicationFactor())
        assertEquals("1", richOrder.configs()["min.insync.replicas"])

        assertEquals(1, richTrade.numPartitions())
        assertEquals(1.toShort(), richTrade.replicationFactor())
        assertEquals("1", richTrade.configs()["min.insync.replicas"])
    }
}
