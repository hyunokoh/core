package co.nilin.opex.wallet.core.spi

import co.nilin.opex.wallet.core.model.ZkPolLiabilityEvent

interface ZkPolLiabilityExporter {
    suspend fun export(event: ZkPolLiabilityEvent)
}
