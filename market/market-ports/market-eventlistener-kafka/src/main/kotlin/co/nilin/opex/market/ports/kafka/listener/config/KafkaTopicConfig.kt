package co.nilin.opex.market.ports.kafka.listener.config

import org.apache.kafka.clients.admin.NewTopic
import org.apache.kafka.common.config.TopicConfig
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.beans.factory.annotation.Value
import org.springframework.context.annotation.Configuration
import org.springframework.context.support.GenericApplicationContext
import org.springframework.kafka.config.TopicBuilder
import java.util.function.Supplier

@Configuration
class KafkaTopicConfig {

    @Value("\${opex.kafka.topic.partitions:10}")
    private var partitions: Int = 10

    @Value("\${opex.kafka.topic.replicas:3}")
    private var replicas: Int = 3

    @Value("\${opex.kafka.topic.min-insync-replicas:2}")
    private lateinit var minInSyncReplicas: String

    @Autowired
    fun createTopics(applicationContext: GenericApplicationContext) {
        applicationContext.registerBean("topic_richOrder", NewTopic::class.java, Supplier {
            TopicBuilder.name("richOrder")
                .partitions(partitions)
                .replicas(replicas)
                .config(TopicConfig.MIN_IN_SYNC_REPLICAS_CONFIG, minInSyncReplicas)
                .build()
        })

        applicationContext.registerBean("topic_richTrade", NewTopic::class.java, Supplier {
            TopicBuilder.name("richTrade")
                .partitions(partitions)
                .replicas(replicas)
                .config(TopicConfig.MIN_IN_SYNC_REPLICAS_CONFIG, minInSyncReplicas)
                .build()
        })
    }

}
