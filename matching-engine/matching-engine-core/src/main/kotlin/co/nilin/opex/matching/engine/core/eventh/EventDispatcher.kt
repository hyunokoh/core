package co.nilin.opex.matching.engine.core.eventh

import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList

object EventDispatcher {

    private val eventsHandler = ConcurrentHashMap<Class<*>, CopyOnWriteArrayList<EventListener<*>>>()

    @JvmStatic
    inline fun <reified T> register(noinline lambda: (T) -> Unit): Registration = register(T::class.java, lambda)

    @JvmStatic
    fun <T> register(type: Class<T>, lambda: (T) -> Unit): Registration = register(type, EventListener(lambda))

    @JvmStatic
    fun <T> register(type: Class<T>, listener: EventListener<T>): Registration {
        eventsHandler.computeIfAbsent(type) { CopyOnWriteArrayList() }.add(listener)
        return Registration { unregister(type, listener) }
    }

    @JvmStatic
    fun <T> unregister(type: Class<T>, listener: EventListener<T>) {
        eventsHandler[type]?.remove(listener)
        if (eventsHandler[type]?.isEmpty() == true) {
            eventsHandler.remove(type)
        }
    }

    @JvmStatic
    fun clearAll() {
        eventsHandler.clear()
    }


    fun emit(event: CoreEvent) {
        var type: Class<*>? = event::class.java
        while (type != null) {
            eventsHandler[type]?.forEach { eventsHandler ->
                kotlin.runCatching {
                    eventsHandler(event)
                }
            }
            type = type.superclass
        }
    }


    open class EventListener<T>(
        val lambda: (T) -> Unit
    ) {
        operator fun invoke(event: Any) {
            lambda(event as T)
        }
    }

    fun interface Registration {
        fun close()
    }
}
