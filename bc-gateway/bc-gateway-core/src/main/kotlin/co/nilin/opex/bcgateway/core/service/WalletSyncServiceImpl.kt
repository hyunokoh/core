package co.nilin.opex.bcgateway.core.service

import co.nilin.opex.bcgateway.core.api.WalletSyncService
import co.nilin.opex.bcgateway.core.model.CurrencyImplementation
import co.nilin.opex.bcgateway.core.model.DepositScreeningRequest
import co.nilin.opex.bcgateway.core.model.Deposit
import co.nilin.opex.bcgateway.core.model.ZkAmlDepositCaseRecord
import co.nilin.opex.bcgateway.core.model.ZkScreeningDecision
import co.nilin.opex.bcgateway.core.model.Transfer
import co.nilin.opex.bcgateway.core.spi.AssignedAddressHandler
import co.nilin.opex.bcgateway.core.spi.CurrencyHandler
import co.nilin.opex.bcgateway.core.spi.DepositHandler
import co.nilin.opex.bcgateway.core.spi.WalletProxy
import co.nilin.opex.bcgateway.core.spi.ZkAmlDepositCaseRecorder
import co.nilin.opex.bcgateway.core.spi.ZkAmlDepositScreeningService
import co.nilin.opex.bcgateway.core.utils.LoggerDelegate
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import org.slf4j.Logger
import org.springframework.stereotype.Service
import org.springframework.transaction.annotation.Transactional
import java.math.BigDecimal

@Service
class WalletSyncServiceImpl(
    private val walletProxy: WalletProxy,
    private val assignedAddressHandler: AssignedAddressHandler,
    private val currencyHandler: CurrencyHandler,
    private val depositHandler: DepositHandler,
    private val zkAmlDepositScreeningService: ZkAmlDepositScreeningService,
    private val zkAmlDepositCaseRecorder: ZkAmlDepositCaseRecorder
) : WalletSyncService {

    private val logger: Logger by LoggerDelegate()

    @Transactional
    override suspend fun syncTransfers(transfers: List<Transfer>) = coroutineScope {
        val groupedByChain = currencyHandler.fetchAllImplementations().groupBy { it.chain.name }
        val deposits = transfers.mapNotNull {
            coroutineScope {
                val currencyImpl = groupedByChain[it.chain]?.find { c -> c.tokenAddress == it.tokenAddress }
                    ?: throw IllegalStateException("Currency implementation not found")
                assignedAddressHandler.findUuid(it.receiver.address, it.receiver.memo)?.let { it to currencyImpl }
            }?.let { (uuid, currencyImpl) ->
                val screening = zkAmlDepositScreeningService.screenDeposit(
                    DepositScreeningRequest(
                        ownerUuid = uuid,
                        chain = it.chain,
                        txHash = it.txHash,
                        amount = it.amount.toPlainString(),
                        receiverAddress = it.receiver.address,
                        receiverMemo = it.receiver.memo,
                        tokenAddress = it.tokenAddress
                    )
                )
                // Persist the screening decision for every deposit (ALLOW included). Regulators
                // need a complete trail mapping each on-chain deposit to its AML decision; the
                // previous code only recorded BLOCK/REVIEW so approved deposits had no link.
                zkAmlDepositCaseRecorder.record(
                    ZkAmlDepositCaseRecord(
                        ownerUuid = uuid,
                        chain = it.chain,
                        txHash = it.txHash,
                        amount = it.amount.toPlainString(),
                        receiverAddress = it.receiver.address,
                        receiverMemo = it.receiver.memo,
                        tokenAddress = it.tokenAddress,
                        decision = screening.decision,
                        reason = screening.reason,
                        externalRef = screening.externalRef
                    )
                )
                if (screening.decision == ZkScreeningDecision.ALLOW) {
                    sendDeposit(uuid, currencyImpl, it)
                    logger.info("Deposit synced for $uuid on ${currencyImpl.currency.symbol} - to ${it.receiver.address}")
                } else {
                    logger.warn(
                        "zkAML held deposit owner={} chain={} txHash={} decision={} reason={} ref={}",
                        uuid,
                        it.chain,
                        it.txHash,
                        screening.decision,
                        screening.reason,
                        screening.externalRef
                    )
                }
                it
            }
        }.map {
            Deposit(
                null,
                it.txHash,
                it.receiver.address,
                it.receiver.memo,
                it.amount,
                it.chain,
                it.isTokenTransfer,
                it.tokenAddress
            )
        }.toList()
        depositHandler.saveAll(deposits)
    }

    private suspend fun sendDeposit(uuid: String, currencyImpl: CurrencyImplementation, transfer: Transfer) {
        val amount = transfer.amount.divide(BigDecimal.TEN.pow(currencyImpl.decimal))
        val symbol = currencyImpl.currency.symbol
        logger.info("Sending deposit to $uuid - $amount $symbol")
        walletProxy.transfer(uuid, symbol, amount, transfer.txHash,transfer.chain)
    }
}
