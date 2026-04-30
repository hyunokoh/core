package co.nilin.opex.matching.engine.ports.kafka.submitter.config

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

    @Autowired
    private lateinit var symbols: List<String>

    @Value("\${opex.kafka.topic.partitions:10}")
    private var partitions: Int = 10

    @Value("\${opex.kafka.topic.replicas:3}")
    private var replicas: Int = 3

    @Value("\${opex.kafka.topic.min-insync-replicas:2}")
    private lateinit var minInSyncReplicas: String

    @Autowired
    fun createTopics(applicationContext: GenericApplicationContext) {
        symbols.map { s -> "orders_$s" }
            .forEach { topic ->
                applicationContext.registerBean("topic_${topic}", NewTopic::class.java, Supplier {
                    TopicBuilder.name(topic)
                        .partitions(partitions)
                        .replicas(replicas)
                        .config(TopicConfig.MIN_IN_SYNC_REPLICAS_CONFIG, minInSyncReplicas)
                        .build()
                })
            }

        symbols.map { s -> "events_$s" }
            .forEach { topic ->
                applicationContext.registerBean("topic_${topic}", NewTopic::class.java, Supplier {
                    TopicBuilder.name(topic)
                        .partitions(partitions)
                        .replicas(replicas)
                        .config(TopicConfig.MIN_IN_SYNC_REPLICAS_CONFIG, minInSyncReplicas)
                        .build()
                })
            }

        symbols.map { s -> "trades_$s" }
            .forEach { topic ->
                applicationContext.registerBean("topic_${topic}", NewTopic::class.java, Supplier {
                    TopicBuilder.name(topic)
                        .partitions(partitions)
                        .replicas(replicas)
                        .config(TopicConfig.MIN_IN_SYNC_REPLICAS_CONFIG, minInSyncReplicas)
                        .build()
                })
            }
    }

}
