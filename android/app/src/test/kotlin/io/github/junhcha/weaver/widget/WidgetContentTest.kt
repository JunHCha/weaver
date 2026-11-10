package io.github.junhcha.weaver.widget

import android.content.Context
import androidx.glance.appwidget.testing.unit.*
import androidx.glance.testing.unit.*
import androidx.test.core.app.ApplicationProvider
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class WidgetContentTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private val row = WidgetRow("pkg_0123456789abcdef", 3, "긴 한국어 요약 ".repeat(30), false)

    @Test fun emptyAndWarningCombinations() {
        for (count in listOf(0, 1, 6)) for (alert in listOf(false, true)) for (large in listOf(false, true)) {
            runGlanceAppWidgetUnitTest {
                setAppWidgetSize(if (large) ReviewWidget.LARGE else ReviewWidget.SMALL)
                val model = WidgetModel(List(count) { row.copy(id = "${row.id}$it") },
                    if (alert) listOf("새 일정 후보입니다") else emptyList())
                provideComposable { WidgetContent(context, model, large) }
                onNode(hasText("Weaver · 검토 대기 ${count}건")).assertExists()
                onAllNodes(hasText("검토 대기 · 긴 한국어 요약")).assertCountEquals(minOf(count, if (large) 5 else 2))
                if (count == 0) onNode(hasText("검토 대기 문서 없음")).assertExists()
                if (alert) onNode(hasText("일정이 바뀌었을 수 있습니다")).assertExists()
                else onAllNodes(hasText("일정이 바뀌었을 수 있습니다")).assertCountEquals(0)
                if (count == 6) onNode(hasText(if (large) "외 1건" else "외 4건")).assertExists()
            }
        }
    }
    @Test fun rowClickCarriesPackageAndRevisionAndSentIsLabeled() = runGlanceAppWidgetUnitTest {
        val model = WidgetModel(listOf(row.copy(submitted = true)))
        provideComposable { WidgetContent(context, model, false) }
        onNode(hasText("전송됨 · 긴 한국어 요약"))
            .assertHasStartActivityClickAction(WidgetReviewActivity.intent(context, row.id, row.revision))
        onNode(hasText("Weaver · 검토 대기 0건")).assertExists()
    }
    @Test fun errorDoesNotPretendTheInboxIsEmpty() = runGlanceAppWidgetUnitTest {
        provideComposable { WidgetContent(context, WidgetModel(status = "목록 읽기 오류", failed = true), false) }
        onNode(hasText("목록 읽기 오류")).assertExists()
        onAllNodes(hasText("검토 대기 문서 없음")).assertCountEquals(0)
    }
}
