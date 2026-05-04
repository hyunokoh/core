package co.nilin.opex.accountant.core.inout

import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.Test

internal class OrderStatusTest {

    @Test
    fun givenTerminalStatuses_whenIsTerminal_thenReturnTrue(): Unit {
        val terminalStatuses = listOf(
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED
        )

        terminalStatuses.forEach {
            assertThat(it.code.isTerminal()).isTrue()
        }
    }

    @Test
    fun givenActiveStatuses_whenIsTerminal_thenReturnFalse(): Unit {
        val activeStatuses = listOf(
            OrderStatus.REQUESTED,
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED
        )

        activeStatuses.forEach {
            assertThat(it.code.isTerminal()).isFalse()
        }
    }

    @Test
    fun givenUnknownOrNullStatus_whenIsTerminal_thenReturnFalse(): Unit {
        assertThat(null.isTerminal()).isFalse()
        assertThat((-1).isTerminal()).isFalse()
    }
}
