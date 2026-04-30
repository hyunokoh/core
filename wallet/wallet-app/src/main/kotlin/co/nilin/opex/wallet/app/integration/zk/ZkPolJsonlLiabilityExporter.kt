package co.nilin.opex.wallet.app.integration.zk

import co.nilin.opex.wallet.core.model.ZkPolLiabilityEvent
import co.nilin.opex.wallet.core.spi.ZkPolLiabilityExporter
import com.fasterxml.jackson.databind.ObjectMapper
import co.nilin.opex.wallet.ports.postgres.dao.ZkPolLiabilityOutboxRepository
import co.nilin.opex.wallet.ports.postgres.model.ZkPolLiabilityOutboxModel
import kotlinx.coroutines.reactive.awaitFirstOrNull
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.annotation.Value
import org.springframework.stereotype.Component
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths

@Component
class ZkPolJsonlLiabilityExporter(
    private val objectMapper: ObjectMapper,
    private val outboxRepository: ZkPolLiabilityOutboxRepository
) : ZkPolLiabilityExporter {

    private val logger = LoggerFactory.getLogger(ZkPolJsonlLiabilityExporter::class.java)

    @Value("\${app.zkpol.export.enabled:false}")
    private var enabled: Boolean = false

    @Value("\${app.zkpol.export.path:}")
    private lateinit var exportPath: String

    @Value("\${app.zkpol.outbox.enabled:false}")
    private var outboxEnabled: Boolean = false

    override suspend fun export(event: ZkPolLiabilityEvent) {
        if (!enabled) {
            return
        }

        var exported = false

        if (outboxEnabled) {
            outboxRepository.save(
                ZkPolLiabilityOutboxModel(
                    tokenId = event.tokenId,
                    accountId = event.accountId,
                    balance = event.balance,
                    delta = event.delta,
                    eventType = event.eventType,
                    occurredAt = event.occurredAt,
                    referenceId = event.referenceId,
                    sourceSystem = event.sourceSystem
                )
            ).awaitFirstOrNull()
            exported = true
        }

        if (exportPath.isNotBlank()) {
            val path = Paths.get(exportPath)
            ensureParent(path)
            val line = objectMapper.writeValueAsString(event) + "\n"
            Files.write(
                path,
                line.toByteArray(StandardCharsets.UTF_8),
                java.nio.file.StandardOpenOption.CREATE,
                java.nio.file.StandardOpenOption.WRITE,
                java.nio.file.StandardOpenOption.APPEND
            )
            exported = true
        }

        if (!exported) {
            logger.warn("zkPoL exporter enabled but no sink configured")
        }
    }

    private fun ensureParent(path: Path) {
        path.parent?.let { Files.createDirectories(it) }
    }
}
