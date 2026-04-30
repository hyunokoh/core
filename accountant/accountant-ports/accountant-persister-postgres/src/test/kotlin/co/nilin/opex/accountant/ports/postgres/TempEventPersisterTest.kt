package co.nilin.opex.accountant.ports.postgres

import co.nilin.opex.accountant.ports.postgres.dao.TempEventRepository
import co.nilin.opex.accountant.ports.postgres.impl.TempEventPersisterImpl
import co.nilin.opex.matching.engine.core.eventh.events.CoreEvent
import com.fasterxml.jackson.databind.ObjectMapper
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.reactor.awaitSingle
import kotlinx.coroutines.reactor.awaitSingleOrNull
import kotlinx.coroutines.runBlocking
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired

class TempEventPersisterTest : AccountantPostgresIntegrationTest() {

    @Autowired
    private lateinit var tempEventRepository: TempEventRepository

    @Autowired
    private lateinit var objectMapper: ObjectMapper

    private val persister by lazy { TempEventPersisterImpl(tempEventRepository, objectMapper) }

    @BeforeEach
    fun cleanDb(): Unit = runBlocking {
        tempEventRepository.deleteAll().awaitSingleOrNull()
    }

    @Test
    fun givenOuidAndEvent_whenSaving_persistsEvent(): Unit = runBlocking {
        persister.saveTempEvent("event_1", Valid.testEvent)

        val persisted = tempEventRepository.findByOuid("event_1").toList()
        assertThat(persisted).hasSize(1)
        assertThat(persisted[0].ouid).isEqualTo("event_1")
        assertThat(persisted[0].eventType).isEqualTo(Valid.testEvent.javaClass.name)
    }

    @Test
    fun givenOUID_whenLoadingTempEvent_parseEventJSON(): Unit = runBlocking {
        persister.saveTempEvent("event_1", Valid.testEvent)

        val events = persister.loadTempEvents("event_1")

        assertThat(events).isNotEmpty
        with(events[0]) {
            assertThat(this).isInstanceOf(CoreEvent::class.java)
            assertThat(pair.rightSideName).isEqualTo(Valid.testEvent.rightSidePair)
            assertThat(pair.leftSideName).isEqualTo(Valid.testEvent.leftSidePair)
        }
    }

    @Test
    fun givenOuid_whenDeletingByOUID_removesPersistedEvents(): Unit = runBlocking {
        val persisted = tempEventRepository.save(Valid.tempEventModel.copy(id = null)).awaitSingle()

        persister.removeTempEvents(persisted.ouid)

        assertThat(tempEventRepository.findByOuid(persisted.ouid).toList()).isEmpty()
    }
}
