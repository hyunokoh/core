package co.nilin.opex.api.ports.binance.util

import co.nilin.opex.common.OpexError
import java.util.Date

private const val DEFAULT_RECV_WINDOW = 5000L
private const val MAX_RECV_WINDOW = 60000L
private const val MAX_FUTURE_TIMESTAMP_SKEW = 1000L

fun validateSignedRequest(recvWindow: Long?, timestamp: Long) {
    val validWindow = recvWindow ?: DEFAULT_RECV_WINDOW
    if (validWindow !in 1..MAX_RECV_WINDOW)
        throw OpexError.InvalidRequestParam.exception("Parameter 'recvWindow' is either missing or invalid")

    val now = Date().time
    if (timestamp <= 0 || timestamp >= now + MAX_FUTURE_TIMESTAMP_SKEW || now - timestamp > validWindow)
        throw OpexError.InvalidRequestParam.exception("Parameter 'timestamp' is either missing or invalid")
}
