package co.nilin.opex.eventlog.ports.kafka.listener.config

import org.apache.kafka.clients.admin.NewTopic
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import org.springframework.context.support.GenericApplicationContext
import org.springframework.test.util.ReflectionTestUtils

class KafkaTopicConfigTest {

    @Test
    fun `createTopics applies configured dlt partition and replica counts`() {
        val context = GenericApplicationContext()
        val config = KafkaTopicConfig()
        ReflectionTestUtils.setField(config, "partitions", 1)
        ReflectionTestUtils.setField(config, "replicas", 1)

        config.createTopics(context)
        context.refresh()

        val topic = context.getBean("topic_events.DLT", NewTopic::class.java)
        assertEquals("events.DLT", topic.name())
        assertEquals(1, topic.numPartitions())
        assertEquals(1.toShort(), topic.replicationFactor())
    }
}
