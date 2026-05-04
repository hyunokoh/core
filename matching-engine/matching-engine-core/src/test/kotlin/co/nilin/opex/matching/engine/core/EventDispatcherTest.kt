package co.nilin.opex.matching.engine.core

import co.nilin.opex.matching.engine.core.eventh.EventDispatcher
import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import co.nilin.opex.matching.engine.core.model.Pair
import org.junit.jupiter.api.Assertions
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test

class EventDispatcherTest {

    private val pair = Pair("BTC", "USDT")

    @BeforeEach
    fun resetEventDispatcher() {
        EventDispatcher.clearAll()
    }

    @Test
    fun givenRegisteredListener_whenRegistrationClosed_thenListenerStopsReceivingEvents() {
        var receivedEvents = 0
        val registration = EventDispatcher.register(CoreEvent::class.java) { _: CoreEvent ->
            receivedEvents++
        }

        EventDispatcher.emit(CoreEvent(pair))
        registration.close()
        EventDispatcher.emit(CoreEvent(pair))

        Assertions.assertEquals(1, receivedEvents)
    }

    @Test
    fun givenRegisteredListeners_whenDispatcherCleared_thenNoListenerReceivesEvents() {
        var firstReceivedEvents = 0
        var secondReceivedEvents = 0
        EventDispatcher.register(CoreEvent::class.java) { _: CoreEvent ->
            firstReceivedEvents++
        }
        EventDispatcher.register(CoreEvent::class.java) { _: CoreEvent ->
            secondReceivedEvents++
        }

        EventDispatcher.clearAll()
        EventDispatcher.emit(CoreEvent(pair))

        Assertions.assertEquals(0, firstReceivedEvents)
        Assertions.assertEquals(0, secondReceivedEvents)
    }
}
