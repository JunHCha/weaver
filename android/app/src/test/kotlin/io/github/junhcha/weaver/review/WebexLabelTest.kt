package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.pipeline.sourceLabel
import io.github.junhcha.weaver.schema.ReviewPackage
import io.github.junhcha.weaver.schema.WeaverCodec
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class WebexLabelTest {
    private fun review() = WeaverCodec.json.decodeFromString<ReviewPackage>(
        File(System.getProperty("weaver.schemaDir"), "examples/ReviewPackage.json").readText(),
    )

    @Test fun webexSourceLabelsAreNamedNotGeneric() {
        assertEquals("Webex", sourceLabel("webex"))
        assertEquals("Webex", review().copy(sourceType = "webex").sourceLabel())
        assertEquals("Webex", review().copy(sourceType = "WEBEX").sourceLabel())
    }

    @Test fun webexReviewTitleNeverShowsUnknownCounterpart() {
        val title = review().copy(sourceType = "webex", title = "2026-11-19 Webex 대화").reviewTitle()
        assertEquals("2026-11-19 Webex 대화", title)
        assertFalse(title.contains("알 수 없음"))
    }
}
