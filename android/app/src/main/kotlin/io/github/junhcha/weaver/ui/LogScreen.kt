package io.github.junhcha.weaver.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import io.github.junhcha.weaver.phone.PhoneThread
import java.time.ZoneId

/** 로그 > 전체: every Thread the phone knows, newest occurrence first. */
@Composable
internal fun LogThreads(threads: List<PhoneThread>, emptyText: String, zone: ZoneId, onThread: (PhoneThread) -> Unit, modifier: Modifier = Modifier) {
    val ordered = remember(threads) { threads.sortedWith(compareByDescending<PhoneThread> { it.occurredAt }.thenByDescending { it.seq }) }
    LazyColumn(
        modifier.testTag("log-threads"), contentPadding = PaddingValues(horizontal = 16.dp, vertical = 4.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        if (ordered.isEmpty()) item { Muted(emptyText) }
        items(ordered, key = { it.id }) { thread ->
            val at = thread.occurredAt.atZone(zone)
            ThreadCard(
                thread, "${at.toLocalDate().shortDate()} ${"%02d:%02d".format(at.hour, at.minute)} · ${thread.kind.label} · ${thread.source}",
                onClick = { onThread(thread) },
            )
        }
    }
}
