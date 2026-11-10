package io.github.junhcha.weaver.review

import io.github.junhcha.weaver.courier.*
import io.github.junhcha.weaver.schema.*
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class ReviewTest {
    @get:Rule val temporary = TemporaryFolder()
    private val id = "pkg_01ARZ3NDEKTSV4RRFFQ69G5FA0"
    private fun review(): ReviewPackage = CourierClient.decoded<ReviewPackage>(
        File(System.getProperty("weaver.schemaDir"), "examples/ReviewPackage.json").readText())
    private fun verdict(p: ReviewPackage, action: Verdict.Action) = ReviewDraft.verdict(
        p, p.questions.map { it.choices.first() }, action, "수정 본문", "다시 정리해 주세요")

    private class Folder : CourierFolder {
        val files = mutableMapOf<String, String>()
        var fail = false
        var published = 0
        override val identity = "synthetic-root"
        override fun list(folder: String) = files.keys.filter { it.startsWith("$folder/") }
            .map { CourierEntry(it.substringAfter('/'), 0) }
        override fun read(folder: String, name: String) = files.getValue("$folder/$name").byteInputStream()
        override fun publish(folder: String, name: String, write: (java.io.OutputStream) -> Unit) {
            check(!fail) { "synthetic_failure" }
            check("$folder/$name" !in files)
            val out = java.io.ByteArrayOutputStream(); write(out)
            files["$folder/$name"] = out.toString("UTF-8"); published++
        }
        override fun delete(folder: String, name: String): Unit = error("phone must not delete review files")
        fun put(p: ReviewPackage) { files["proposals/${p.packageId}.r${p.revision}.json"] = WeaverCodec.encode(p) }
    }

    @Test fun missingPackageUsesDedicatedException() {
        val store = ReviewStore(temporary.newFolder())
        assertThrows(ReviewStore.PackageMissingException::class.java) { store.latest(Folder(), id) }
        assertThrows(IllegalArgumentException::class.java) { store.latest(Folder(), "../escape") }
    }

    @Test fun fourVerdictsRoundTripAndPublishOnce() {
        Verdict.Action.entries.forEach { action ->
            val folder = Folder(); val p = review(); folder.put(p)
            val store = ReviewStore(temporary.newFolder())
            val v = verdict(p, action)
            assertEquals(emptyList<String>(), SharedSchema.errors("Verdict", WeaverCodec.encode(v)))
            store.queue(folder, p, v); store.queue(folder, p, v)
            assertTrue(store.synchronize(folder)); assertTrue(store.synchronize(folder))
            assertEquals(1, folder.published)
            assertEquals(v, CourierClient.decoded<Verdict>(folder.files.getValue("decisions/${p.packageId}.r${p.revision}.json")))
            assertEquals(ReviewStatus.SENT, store.records().single().status)
        }
    }

    @Test fun answersMustSelectOneChoicePerQuestion() {
        val folder = Folder(); val p = review(); folder.put(p)
        val store = ReviewStore(temporary.newFolder())
        val bad = verdict(p, Verdict.Action.APPROVE).copy(answers = listOf("목록에 없는 답"))
        assertThrows(IllegalArgumentException::class.java) { store.queue(folder, p, bad) }
        assertTrue(store.records().isEmpty())
    }

    @Test fun retryAfterRestartKeepsExactBodyAndRejectsConflicts() {
        val root = temporary.newFolder(); val folder = Folder(); val p = review(); folder.put(p)
        val v = verdict(p, Verdict.Action.REJECT)
        ReviewStore(root).queue(folder, p, v)
        folder.fail = true
        assertFalse(ReviewStore(root).synchronize(folder))
        assertEquals(ReviewStatus.ERROR, ReviewStore(root).records().single().status)
        folder.fail = false
        assertTrue(ReviewStore(root).synchronize(folder))
        assertEquals(v, ReviewStore(root).records().single().verdict)
        assertThrows(IllegalStateException::class.java) {
            ReviewStore(root).queue(folder, p, v.copy(action = Verdict.Action.APPROVE))
        }
        val name = "decisions/${p.packageId}.r${p.revision}.json"
        folder.files[name] = "different"
        assertFalse(ReviewStore(root).synchronize(folder))
        assertEquals(ReviewStatus.CONFLICT, ReviewStore(root).records().single().status)
        assertEquals("different", folder.files[name])
    }

    @Test fun newerRevisionBlocksOpenAndQueuedOldRevisionEvenAfterResync() {
        val folder = Folder(); val p = review(); folder.put(p)
        val root = temporary.newFolder(); val store = ReviewStore(root)
        val v = verdict(p, Verdict.Action.APPROVE)
        store.queue(folder, p, v)
        folder.put(p.copy(revision = p.revision + 1))
        assertEquals(p.revision + 1, store.latest(folder, id).revision)
        assertFalse(store.synchronize(folder)); assertEquals(0, folder.published)
        assertEquals(ReviewStatus.STALE, store.records().single().status)
        assertThrows(IllegalStateException::class.java) { store.queue(folder, p, v) }
        folder.files.remove("proposals/$id.r${p.revision + 1}.json")
        assertThrows(IllegalStateException::class.java) { ReviewStore(root).latest(folder, id) }
    }

    @Test fun disappearanceIsOnlyInferredAndNeverRepublishes() {
        val folder = Folder(); val p = review(); folder.put(p)
        val store = ReviewStore(temporary.newFolder())
        store.queue(folder, p, verdict(p, Verdict.Action.FEEDBACK))
        assertTrue(store.synchronize(folder))
        folder.files.remove("decisions/${p.packageId}.r${p.revision}.json")
        assertTrue(store.synchronize(folder))
        assertEquals(ReviewStatus.INFERRED, store.records().single().status)
        assertEquals(1, folder.published)
    }

    @Test fun sameExistingFinalIsSuccessAndDifferentFinalIsNeverReplaced() {
        val folder = Folder(); val p = review(); folder.put(p)
        val store = ReviewStore(temporary.newFolder())
        val v = verdict(p, Verdict.Action.APPROVE)
        store.queue(folder, p, v)
        folder.files["decisions/${p.packageId}.r${p.revision}.json"] = WeaverCodec.encode(v)
        assertTrue(store.synchronize(folder)); assertEquals(0, folder.published)
        folder.files.remove("proposals/${p.packageId}.r${p.revision}.json")
        assertTrue(store.synchronize(folder))
        assertEquals(ReviewStatus.INFERRED, store.records().single().status)
    }

    @Test fun serverGeneratedPackageIdsAreAccepted() {
        val folder = Folder(); val p = review().copy(packageId = "pkg_0123456789abcdef0123456789abcdef"); folder.put(p)
        assertEquals(p, ReviewStore(temporary.newFolder()).latest(folder, p.packageId))
    }
}
