package co.nilin.opex.wallet.ports.postgres.dao

import co.nilin.opex.wallet.ports.postgres.model.ZkAmlWithdrawCaseModel
import org.springframework.data.repository.reactive.ReactiveCrudRepository
import org.springframework.stereotype.Repository

@Repository
interface ZkAmlWithdrawCaseRepository : ReactiveCrudRepository<ZkAmlWithdrawCaseModel, Long>
