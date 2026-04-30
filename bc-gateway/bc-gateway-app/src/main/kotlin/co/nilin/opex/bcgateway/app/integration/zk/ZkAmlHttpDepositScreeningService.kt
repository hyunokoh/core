package co.nilin.opex.bcgateway.app.integration.zk

import com.fasterxml.jackson.annotation.JsonProperty
import co.nilin.opex.bcgateway.core.model.DepositScreeningRequest
import co.nilin.opex.bcgateway.core.model.DepositScreeningResult
import co.nilin.opex.bcgateway.core.model.ZkScreeningDecision
import co.nilin.opex.bcgateway.core.spi.ZkAmlDepositScreeningService
import kotlinx.coroutines.reactive.awaitFirst
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.annotation.Value
import org.springframework.http.MediaType
import org.springframework.stereotype.Component
import org.springframework.web.reactive.function.client.WebClient
import org.springframework.web.reactive.function.client.bodyToMono
import java.net.URI

@Component
class ZkAmlHttpDepositScreeningService(
    private val webClient: WebClient
) : ZkAmlDepositScreeningService {

    private val logger = LoggerFactory.getLogger(ZkAmlHttpDepositScreeningService::class.java)

    @Value("\${app.zkaml.enabled:false}")
    private var enabled: Boolean = false

    @Value("\${app.zkaml.fail-closed:false}")
    private var failClosed: Boolean = false

    @Value("\${app.zkaml.url:http://localhost:8000}")
    private lateinit var baseUrl: String

    @Value("\${app.zkaml.api-key:}")
    private lateinit var apiKey: String

    @Value("\${app.zkaml.review-risk-score:50}")
    private var reviewRiskScore: Int = 50

    override suspend fun screenDeposit(request: DepositScreeningRequest): DepositScreeningResult {
        if (!enabled) {
            return DepositScreeningResult(ZkScreeningDecision.ALLOW, "zkAML disabled")
        }

        return try {
            val response = webClient.post()
                .uri(URI.create("$baseUrl/screen/tx"))
                .contentType(MediaType.APPLICATION_JSON)
                .headers { headers ->
                    if (apiKey.isNotBlank()) {
                        headers.add("x-api-key", apiKey)
                    }
                }
                .bodyValue(
                    TxScreenRequest(
                        chain = request.chain.lowercase(),
                        txHash = request.txHash
                    )
                )
                .retrieve()
                .bodyToMono<TxScreenResponse>()
                .awaitFirst()

            when {
                response.directMatch -> DepositScreeningResult(
                    ZkScreeningDecision.BLOCK,
                    response.reasonCodes.joinToString(", ").ifBlank { "direct match detected by zkAML" }
                )

                response.riskScore >= reviewRiskScore || response.exposure.isNotEmpty() -> DepositScreeningResult(
                    ZkScreeningDecision.REVIEW,
                    "zkAML review required: severity=${response.severity}, riskScore=${response.riskScore}, reasons=${response.reasonCodes.joinToString(",")}"
                )

                else -> DepositScreeningResult(
                    ZkScreeningDecision.ALLOW,
                    "zkAML allow: reasons=${response.reasonCodes.joinToString(",")}"
                )
            }
        } catch (e: Exception) {
            logger.warn("zkAML deposit screening failed for {}: {}", request.txHash, e.message)
            if (failClosed) {
                DepositScreeningResult(ZkScreeningDecision.REVIEW, "zkAML unavailable")
            } else {
                DepositScreeningResult(ZkScreeningDecision.ALLOW, "zkAML unavailable fail-open")
            }
        }
    }
}

data class TxScreenRequest(
    val chain: String,
    @JsonProperty("tx_hash")
    val txHash: String
)

data class TxScreenExposure(
    @JsonProperty("hop_distance")
    val hopDistance: Int? = null,
    val address: String? = null
)

data class TxScreenResponse(
    @JsonProperty("subject_type")
    val subjectType: String,
    val chain: String,
    @JsonProperty("risk_score")
    val riskScore: Int,
    val severity: String,
    @JsonProperty("direct_match")
    val directMatch: Boolean,
    val exposure: List<TxScreenExposure> = emptyList(),
    @JsonProperty("reason_codes")
    val reasonCodes: List<String> = emptyList()
)
