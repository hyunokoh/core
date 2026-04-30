package co.nilin.opex.wallet.app.integration.zk

import com.fasterxml.jackson.annotation.JsonProperty
import co.nilin.opex.wallet.core.model.WithdrawScreeningRequest
import co.nilin.opex.wallet.core.model.WithdrawScreeningResult
import co.nilin.opex.wallet.core.model.ZkScreeningDecision
import co.nilin.opex.wallet.core.spi.ZkAmlScreeningService
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.annotation.Value
import org.springframework.http.MediaType
import org.springframework.stereotype.Component
import org.springframework.web.reactive.function.client.WebClient
import org.springframework.web.reactive.function.client.bodyToMono
import kotlinx.coroutines.reactive.awaitFirst
import java.net.URI

@Component
class ZkAmlHttpScreeningService(
    private val webClient: WebClient
) : ZkAmlScreeningService {

    private val logger = LoggerFactory.getLogger(ZkAmlHttpScreeningService::class.java)

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

    override suspend fun screenWithdraw(request: WithdrawScreeningRequest): WithdrawScreeningResult {
        if (!enabled) {
            return WithdrawScreeningResult(ZkScreeningDecision.ALLOW, "zkAML disabled")
        }

        return try {
            val response = webClient.post()
                .uri(URI.create("$baseUrl/screen/address"))
                .contentType(MediaType.APPLICATION_JSON)
                .headers { headers ->
                    if (apiKey.isNotBlank()) {
                        headers.add("x-api-key", apiKey)
                    }
                }
                .bodyValue(
                    AddressScreenRequest(
                        chain = request.destinationNetwork.lowercase(),
                        address = request.destinationAddress
                    )
                )
                .retrieve()
                .bodyToMono<AddressScreenResponse>()
                .awaitFirst()

            when {
                response.directMatch -> WithdrawScreeningResult(
                    ZkScreeningDecision.BLOCK,
                    response.reasonCodes.joinToString(", ").ifBlank { "direct match detected by zkAML" }
                )

                response.riskScore >= reviewRiskScore || response.exposure.isNotEmpty() -> WithdrawScreeningResult(
                    ZkScreeningDecision.REVIEW,
                    "zkAML review required: severity=${response.severity}, riskScore=${response.riskScore}, reasons=${response.reasonCodes.joinToString(",")}"
                )

                else -> WithdrawScreeningResult(
                    ZkScreeningDecision.ALLOW,
                    "zkAML allow: reasons=${response.reasonCodes.joinToString(",")}"
                )
            }
        } catch (e: Exception) {
            logger.warn("zkAML withdraw screening failed for {}: {}", request.destinationAddress, e.message)
            if (failClosed) {
                WithdrawScreeningResult(ZkScreeningDecision.REVIEW, "zkAML unavailable")
            } else {
                WithdrawScreeningResult(ZkScreeningDecision.ALLOW, "zkAML unavailable fail-open")
            }
        }
    }
}

data class AddressScreenRequest(
    val chain: String,
    val address: String
)

data class AddressScreenExposure(
    @JsonProperty("hop_distance")
    val hopDistance: Int? = null,
    val address: String? = null
)

data class AddressScreenResponse(
    @JsonProperty("subject_type")
    val subjectType: String,
    val chain: String,
    @JsonProperty("risk_score")
    val riskScore: Int,
    val severity: String,
    @JsonProperty("direct_match")
    val directMatch: Boolean,
    val exposure: List<AddressScreenExposure> = emptyList(),
    @JsonProperty("reason_codes")
    val reasonCodes: List<String> = emptyList()
)
