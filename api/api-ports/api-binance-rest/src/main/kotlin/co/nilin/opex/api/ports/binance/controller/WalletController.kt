package co.nilin.opex.api.ports.binance.controller

import co.nilin.opex.api.core.inout.DepositDetails
import co.nilin.opex.api.core.inout.TransactionHistoryResponse
import co.nilin.opex.api.core.inout.WithdrawHistoryResponse
import co.nilin.opex.api.core.spi.*
import co.nilin.opex.api.ports.binance.data.*
import co.nilin.opex.api.ports.binance.util.jwtAuthentication
import co.nilin.opex.api.ports.binance.util.tokenValue
import co.nilin.opex.api.ports.binance.util.validateSignedRequest
import co.nilin.opex.common.OpexError
import co.nilin.opex.common.utils.Interval
import org.springframework.security.core.annotation.CurrentSecurityContext
import org.springframework.security.core.context.SecurityContext
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController
import java.math.BigDecimal
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneId
import java.util.*

@RestController
class WalletController(
    private val walletProxy: WalletProxy,
    private val symbolMapper: SymbolMapper,
    private val marketDataProxy: MarketDataProxy,
    private val accountantProxy: AccountantProxy,
    private val bcGatewayProxy: BlockchainGatewayProxy,
) {

    @GetMapping("/v1/capital/deposit/address")
    suspend fun assignAddress(
        @RequestParam
        coin: String,
        @RequestParam
        network: String,
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long,
        @CurrentSecurityContext securityContext: SecurityContext
    ): AssignAddressResponse {
        validateSignedRequest(recvWindow, timestamp)
        val response = bcGatewayProxy.assignAddress(securityContext.jwtAuthentication().name, coin, network)
        val address = response?.addresses
        if (address.isNullOrEmpty()) throw OpexError.InternalServerError.exception()
        return AssignAddressResponse(address[0].address, coin, network, "", "")
    }

    @GetMapping("/v1/capital/deposit/hisrec")
    suspend fun getDepositTransactions(
        @RequestParam(required = false)
        coin: String?,
        @RequestParam("status", required = false)
        status: Int?,
        @RequestParam(required = false)
        startTime: Long?,
        @RequestParam(required = false)
        endTime: Long?,
        @RequestParam(required = false)
        offset: Int?,
        @RequestParam(required = false)
        limit: Int?,
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long,
        @RequestParam(required = false)
        ascendingByTime: Boolean? = false,
        @CurrentSecurityContext securityContext: SecurityContext
    ): List<DepositResponse> {
        validateSignedRequest(recvWindow, timestamp)
        validateDepositHistoryStatus(status)
        val validLimit = validWalletHistoryLimit(limit)
        val validOffset = validWalletHistoryOffset(offset)
        validateWalletHistoryTimeRange(startTime, endTime)
        val deposits = walletProxy.getDepositTransactions(
            securityContext.jwtAuthentication().name,
            securityContext.jwtAuthentication().tokenValue(),
            coin,
            startTime ?: null,
            endTime ?: null,
            validLimit,
            validOffset,
            ascendingByTime
        )
        if (deposits.isEmpty())
            return emptyList()

        val details = bcGatewayProxy.getDepositDetails(deposits.filterNot { it.ref.isNullOrBlank() }.map { it.ref!! })
        return matchDepositsAndDetails(deposits, details)
            .filter { status == null || it.status == status }
    }

    @GetMapping("/v1/capital/withdraw/history")
    suspend fun getWithdrawTransactions(
        @RequestParam(required = false)
        coin: String?,
        @RequestParam(required = false)
        withdrawOrderId: String?,
        @RequestParam("status", required = false)
        withdrawStatus: Int?,
        @RequestParam(required = false)
        offset: Int?,
        @RequestParam(required = false)
        limit: Int?,
        @RequestParam(required = false)
        startTime: Long?,
        @RequestParam(required = false)
        endTime: Long?,
        @RequestParam(required = false)
        ascendingByTime: Boolean? = false,
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long,
        @CurrentSecurityContext securityContext: SecurityContext
    ): List<WithdrawResponse> {
        validateSignedRequest(recvWindow, timestamp)
        validateUnsupportedWithdrawHistoryParams(withdrawOrderId)
        validateWithdrawHistoryStatus(withdrawStatus)
        val validLimit = validWalletHistoryLimit(limit)
        val validOffset = validWalletHistoryOffset(offset)
        validateWalletHistoryTimeRange(startTime, endTime)
        val response = walletProxy.getWithdrawTransactions(
            securityContext.jwtAuthentication().name,
            securityContext.jwtAuthentication().tokenValue(),
            coin,
            startTime ?: null,
            endTime ?: null,
            validLimit,
            validOffset,
            ascendingByTime
        )
        return response
            .map { it.asWithdrawResponse() }
            .filter { withdrawStatus == null || it.status == withdrawStatus }
    }


    @PostMapping("/v2/capital/withdraw/history")
    suspend fun getWithdrawTransactionsV2(
        @RequestBody withdrawRequest: WithDrawRequest,
        @CurrentSecurityContext securityContext: SecurityContext
    ): List<WithdrawResponse> {
        validateSignedRequest(withdrawRequest.recvWindow, withdrawRequest.timestamp)
        validateUnsupportedWithdrawHistoryParams(withdrawRequest.withdrawOrderId)
        validateWithdrawHistoryStatus(withdrawRequest.withdrawStatus)
        val validLimit = validWalletHistoryLimit(withdrawRequest.limit)
        val validOffset = validWalletHistoryOffset(withdrawRequest.offset)
        validateWalletHistoryTimeRange(withdrawRequest.startTime, withdrawRequest.endTime)
        val response = walletProxy.getWithdrawTransactions(
            securityContext.jwtAuthentication().name,
            securityContext.jwtAuthentication().tokenValue(),
            withdrawRequest.coin,
            withdrawRequest.startTime ?: null,
            withdrawRequest.endTime ?: null,
            validLimit,
            validOffset,
            withdrawRequest.ascendingByTime
        )
        return response
            .map { it.asWithdrawResponse() }
            .filter { withdrawRequest.withdrawStatus == null || it.status == withdrawRequest.withdrawStatus }
    }

    @GetMapping("/v1/asset/tradeFee")
    suspend fun getPairFees(
        @RequestParam(required = false)
        symbol: String?,
        @RequestParam(required = false)
        recvWindow: Long?, //The value cannot be greater than 60000
        @RequestParam
        timestamp: Long
    ): List<PairFeeResponse> {
        validateSignedRequest(recvWindow, timestamp)
        validateOptionalAssetParam(symbol, "symbol")
        return if (symbol != null) {
            val internalSymbol = symbolMapper.toInternalSymbol(symbol) ?: throw OpexError.SymbolNotFound.exception()

            val fee = accountantProxy.getFeeConfig(internalSymbol)
            arrayListOf<PairFeeResponse>().apply {
                add(
                    PairFeeResponse(
                        symbol,
                        fee.makerFee.toDouble(),
                        fee.takerFee.toDouble()
                    )
                )
            }
        } else
            accountantProxy.getFeeConfigs()
                .distinctBy { it.pair }
                .map {
                    PairFeeResponse(
                        symbolMapper.fromInternalSymbol(it.pair) ?: it.pair,
                        it.makerFee.toDouble(),
                        it.takerFee.toDouble()
                    )
                }
    }

    @GetMapping("/v1/asset/getUserAsset")
    suspend fun getUserAssets(
        @CurrentSecurityContext
        securityContext: SecurityContext,
        @RequestParam(required = false)
        symbol: String?,
        @RequestParam(required = false)
        quoteAsset: String?,
        @RequestParam(required = false)
        calculateEvaluation: Boolean?
    ): List<AssetResponse> {
        validateOptionalAssetParam(symbol, "symbol")
        validateOptionalAssetParam(quoteAsset, "quoteAsset")
        val auth = securityContext.jwtAuthentication()
        val result = arrayListOf<AssetResponse>()

        if (symbol != null) {
            val wallet = walletProxy.getWallet(auth.name, auth.tokenValue(), symbol.uppercase())
            result.add(AssetResponse(wallet.asset, wallet.balance, wallet.locked, wallet.withdraw))
        } else {
            result.addAll(
                walletProxy.getWallets(auth.name, auth.tokenValue())
                    .map { AssetResponse(it.asset, it.balance, it.locked, it.withdraw) }
            )
        }

        if (quoteAsset == null)
            return result

        val prices = marketDataProxy.getBestPriceForSymbols(
            result.map { "${it.asset.uppercase()}_${quoteAsset.uppercase()}" }
        ).associateBy { it.symbol.split("_")[0].uppercase() }

        result.associateWith { prices[it.asset.uppercase()] }
            .forEach { (asset, price) -> asset.valuation = price?.bidPrice ?: BigDecimal.ZERO }

        if (calculateEvaluation == true)
            result.forEach {
                it.free = it.free.multiply(it.valuation)
                it.locked = it.locked.multiply(it.valuation)
                it.withdrawing = it.withdrawing.multiply(it.valuation)
            }

        return result
    }

    @GetMapping("/v1/asset/estimatedValue")
    suspend fun assetsEstimatedValue(
        @CurrentSecurityContext
        securityContext: SecurityContext,
        @RequestParam
        quoteAsset: String
    ): AssetsEstimatedValue {
        validateRequiredAssetParam(quoteAsset, "quoteAsset")
        val auth = securityContext.jwtAuthentication()
        val wallets = walletProxy.getWallets(auth.name, auth.tokenValue())
        val rates = marketDataProxy.getBestPriceForSymbols(
            wallets.map { "${it.asset.uppercase()}_${quoteAsset.uppercase()}" }
        ).associateBy { it.symbol.split("_")[0].uppercase() }

        var value = BigDecimal.ZERO
        val zeroAssets = arrayListOf<String>()
        wallets.filter { !it.asset.equals(quoteAsset, true) }
            .associateWith { rates[it.asset.uppercase()] }
            .forEach { (asset, price) ->
                if (price == null || (price.bidPrice ?: BigDecimal.ZERO) == BigDecimal.ZERO)
                    zeroAssets.add(asset.asset)
                else
                    value += asset.balance.multiply(price.bidPrice)
            }

        // Add quote asset balance with rate of 1
        wallets.find { it.asset.equals(quoteAsset, true) }?.let { value += it.balance }
        return AssetsEstimatedValue(value, quoteAsset.uppercase(), zeroAssets)
    }

    private fun matchDepositsAndDetails(
        deposits: List<TransactionHistoryResponse>,
        details: List<DepositDetails>
    ): List<DepositResponse> {
        val detailMap = details.associateBy { it.hash }
        return deposits.associateWith {
            detailMap[it.ref]
        }.mapNotNull { (deposit, detail) ->
            detail?.let {
                DepositResponse(
                    deposit.amount,
                    deposit.currency,
                    detail.chain,
                    1,
                    detail.address,
                    null,
                    deposit.ref ?: deposit.id.toString(),
                    deposit.date,
                    1,
                    "1/1",
                    "1/1",
                    deposit.date
                )
            }
        }
    }

    private fun validWalletHistoryLimit(limit: Int?): Int {
        val validLimit = limit ?: 1000
        if (validLimit !in 1..1000)
            throw OpexError.InvalidRequestParam.exception("Parameter 'limit' is either missing or invalid")
        return validLimit
    }

    private fun validWalletHistoryOffset(offset: Int?): Int {
        val validOffset = offset ?: 0
        if (validOffset < 0)
            throw OpexError.InvalidRequestParam.exception("Parameter 'offset' is either missing or invalid")
        return validOffset
    }

    private fun validateWalletHistoryTimeRange(startTime: Long?, endTime: Long?) {
        if (startTime != null && startTime <= 0)
            throw OpexError.InvalidRequestParam.exception("Parameter 'startTime' is either missing or invalid")
        if (endTime != null && endTime <= 0)
            throw OpexError.InvalidRequestParam.exception("Parameter 'endTime' is either missing or invalid")
        if (startTime != null && endTime != null && startTime > endTime)
            throw OpexError.InvalidRequestParam.exception("Parameter 'startTime' is either missing or invalid")
    }

    private fun validateUnsupportedWithdrawHistoryParams(withdrawOrderId: String?) {
        if (withdrawOrderId != null)
            throw OpexError.InvalidRequestParam.exception("Parameter 'withdrawOrderId' is either missing or invalid")
    }

    private fun validateDepositHistoryStatus(status: Int?) {
        if (status != null && status !in 0..1)
            throw OpexError.InvalidRequestParam.exception("Parameter 'status' is either missing or invalid")
    }

    private fun validateWithdrawHistoryStatus(status: Int?) {
        if (status != null && status !in 0..2)
            throw OpexError.InvalidRequestParam.exception("Parameter 'status' is either missing or invalid")
    }

    private fun validateOptionalAssetParam(value: String?, paramName: String) {
        if (value != null && value.isBlank())
            throw OpexError.InvalidRequestParam.exception("Parameter '$paramName' is either missing or invalid")
    }

    private fun validateRequiredAssetParam(value: String, paramName: String) {
        if (value.isBlank())
            throw OpexError.InvalidRequestParam.exception("Parameter '$paramName' is either missing or invalid")
    }

    private fun WithdrawHistoryResponse.asWithdrawResponse(): WithdrawResponse {
        val binanceStatus = when (status) {
            "CREATED" -> 0
            "DONE" -> 1
            "REJECTED" -> 2
            else -> -1
        }

        return WithdrawResponse(
            destAddress ?: "0x0",
            amount,
            LocalDateTime.ofInstant(Instant.ofEpochMilli(createDate), ZoneId.systemDefault())
                .toString()
                .replace("T", " "),
            destSymbol ?: "",
            withdrawId?.toString() ?: "",
            "",
            destNetwork ?: "",
            1,
            binanceStatus,
            appliedFee.toString(),
            3,
            destTransactionRef ?: withdrawId.toString(),
            if (binanceStatus == 1 && acceptDate != null) acceptDate!! else createDate
        )
    }
}
