package io.github.junhcha.weaver.shepherd.autoshare

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AutoShareMachineTest {
    private fun machine(device: FakeDevice, size: Long = 0, timing: AutoShareTiming = AutoShareTiming()) =
        AutoShareMachine(AutoShareJob("call-1", STEM, size), device.now, { device.submitted }, device.weaverPackage, timing = timing)

    private fun assertOnlyWhitelisted(device: FakeDevice) {
        val outside = device.clicks.filter { it !in FakeDevice.whitelist }
        assertTrue("clicked outside the whitelist: $outside", outside.isEmpty())
    }

    private val fullFlow = listOf("row:$STEM", "assist", "transcribe", "more_options:detail", "share", "text_file", "app:Weaver")

    @Test fun expandedLayoutRunsTheWholeFlowAndPicksTheDetailToolbarMenu() {
        val device = FakeDevice(expanded = true, detail = "통화 김철수_261109_090000")
        val result = run(device, machine(device))
        assertEquals(Progress.Succeeded, result)
        assertEquals(fullFlow, device.clicks)
        assertTrue("more_options:list" !in device.clicks)
    }

    @Test fun foldedLayoutRunsTheWholeFlowAndIgnoresHiddenListNodes() {
        val device = FakeDevice(expanded = false)
        val result = run(device, machine(device))
        assertEquals(Progress.Succeeded, result)
        // The hidden list row stays in the tree after the detail opens; it must not be clicked again.
        assertEquals(fullFlow, device.clicks)
    }

    @Test fun foldedLayoutLeavesAnotherRecordingsDetailWithBack() {
        val device = FakeDevice(expanded = false, detail = "통화 김철수_261109_090000")
        assertEquals(Progress.Succeeded, run(device, machine(device)))
        assertEquals(1, device.backs)
        assertEquals(fullFlow, device.clicks)
    }

    @Test fun foldedLayoutGoesBackBeforePressingARowThatAnotherDetailCovers() {
        // The covered row beneath the detail still reports visible; pressing it does nothing on a device.
        val device = FakeDevice(expanded = false, detail = "통화 김철수_261109_090000", coveredListStaysVisible = true)
        assertEquals(Progress.Succeeded, run(device, machine(device)))
        assertEquals(1, device.backs)
        assertEquals(fullFlow, device.clicks)
    }

    @Test fun nodeEqualityFollowsTheUnderlyingNodeNotTheWrapper() {
        // Every parent/children access returns a new wrapper; the toolbar title must still be found under its toolbar.
        val device = FakeDevice(expanded = false, detail = STEM)
        val nodes = Selectors.nodes(device.screen())
        assertEquals(Selectors.Detail.MATCH, Selectors.detail(nodes, STEM))
        assertEquals(Selectors.Detail.OTHER, Selectors.detail(nodes, "통화 김철수_261109_090000"))
        val toolbar = nodes.first { it.idName() == "fragment_toolbar" }
        val viaChild = nodes.first { it.idName() == "more_options" && it.ancestors().any { a -> a == toolbar } }
        assertTrue(viaChild.parent !== viaChild.parent)
        assertEquals(viaChild.parent, viaChild.parent)
        assertTrue(Selectors.moreOptions(nodes) != null)
    }

    @Test fun shareSheetPicksTheAppRowEvenThoughDirectShareRowsAreAlsoItems() {
        val device = FakeDevice(expanded = true)
        val result = run(device, machine(device))
        assertEquals(Progress.Succeeded, result)
        assertTrue("DIRECT" !in device.clicks && "DIRECT-OTHER" !in device.clicks)
        assertEquals("app:Weaver", device.clicks.last())
    }

    @Test fun debugBuildTreatsItsOwnSuffixedPackageAsWeaver() {
        // With a mismatched package the screen counts as a foreign app and the job is abandoned.
        val debug = FakeDevice(expanded = true, weaverPackage = WEAVER_DEBUG, autoSubmit = false)
        val matching = run(debug, machine(debug))
        assertEquals(Progress.Failed(AutoError.STEP_TIMEOUT, AutoStep.AWAIT_SUBMIT), matching)
        val other = FakeDevice(expanded = true, weaverPackage = WEAVER_DEBUG, autoSubmit = false)
        val mismatched = run(other, AutoShareMachine(AutoShareJob("call-1", STEM, 0), other.now, { other.submitted }, WEAVER))
        assertEquals(Progress.Failed(AutoError.FOREIGN_APP, AutoStep.AWAIT_SUBMIT), mismatched)
        assertEquals(setOf(Selectors.VOICE_NOTE, Selectors.CHOOSER, WEAVER_DEBUG), Selectors.allowedPackages(WEAVER_DEBUG))
        assertTrue(WEAVER_DEBUG !in Selectors.inspectedPackages && WEAVER !in Selectors.inspectedPackages)
    }

    @Test fun alreadyTranscribedRecordingSkipsTranscription() {
        val device = FakeDevice(expanded = true, transcribed = mutableSetOf(STEM))
        assertEquals(Progress.Succeeded, run(device, machine(device)))
        assertEquals(listOf("row:$STEM", "more_options:detail", "share", "text_file", "app:Weaver"), device.clicks)
    }

    @Test fun waitsWhileTranscribingThenContinuesAfterCompletion() {
        val device = FakeDevice(expanded = true, transcribeMs = 40_000)
        val machine = machine(device, size = 480_000)
        var sawWait = false
        val result = run(device, machine) { if (machine.step == AutoStep.WAIT_TRANSCRIPT) sawWait = true }
        assertTrue(sawWait)
        assertEquals(Progress.Succeeded, result)
        assertOnlyWhitelisted(device)
    }

    @Test fun transcriptionTimeoutFails() {
        val device = FakeDevice(expanded = true, transcribeMs = Long.MAX_VALUE / 4)
        val result = run(device, machine(device, size = 0))
        assertEquals(Progress.Failed(AutoError.TRANSCRIBE_TIMEOUT, AutoStep.WAIT_TRANSCRIPT), result)
        assertTrue("share" !in device.clicks)
    }

    @Test fun scrollsTheListToFindTheRecording() {
        val others = (1..9).map { "통화 상대${it}_261101_0${it}0000" }
        val device = FakeDevice(expanded = true, recordings = others + others.map { it + "x" } + STEM, pageSize = 6)
        assertEquals(Progress.Succeeded, run(device, machine(device)))
        assertTrue(device.scrolls >= 2)
        assertOnlyWhitelisted(device)
    }

    @Test fun missingRecordingFailsAfterReachingTheEndOfTheList() {
        val device = FakeDevice(expanded = true, recordings = listOf("통화 김철수_261109_090000", "통화 이영희_261108_080000"))
        val result = run(device, machine(device))
        assertEquals(Progress.Failed(AutoError.RECORDING_NOT_FOUND, AutoStep.FIND_RECORDING), result)
        assertTrue(device.clicks.isEmpty())
    }

    @Test fun shareSheetWithoutWeaverFailsAndNeverUsesTheDirectShareRow() {
        val device = FakeDevice(expanded = true, sheetApps = listOf("Gmail", "Drive"))
        val result = run(device, machine(device))
        assertEquals(Progress.Failed(AutoError.APP_NOT_IN_SHEET, AutoStep.PICK_APP), result)
        assertTrue("DIRECT" !in device.clicks)
        assertOnlyWhitelisted(device)
    }

    @Test fun scrollsTheShareSheetToFindWeaver() {
        val device = FakeDevice(expanded = true, sheetApps = listOf("A", "B", "C", "D", "Weaver"), sheetPageSize = 2)
        assertEquals(Progress.Succeeded, run(device, machine(device)))
        assertTrue(device.scrolls >= 2)
        assertOnlyWhitelisted(device)
    }

    @Test fun unknownDialogStopsWithoutClicking() {
        val device = FakeDevice(expanded = true)
        device.unknownDialog = true
        val result = run(device, machine(device))
        assertEquals(Progress.Failed(AutoError.UNKNOWN_DIALOG, AutoStep.LAUNCH), result)
        assertTrue(device.clicks.isEmpty())
    }

    @Test fun userTouchStopsImmediatelyAndStaysStopped() {
        val device = FakeDevice(expanded = true)
        val machine = machine(device)
        machine.advance(device.screen(), device.now)
        val failed = machine.interrupt(AutoError.USER_TOUCH)
        assertEquals(AutoError.USER_TOUCH, failed.error)
        assertTrue(!failed.error.restoreScreen)
        val clicksBefore = device.clicks.size
        device.now += 1_000
        assertEquals(failed, machine.advance(device.screen(), device.now))
        assertEquals(clicksBefore, device.clicks.size)
    }

    @Test fun switchingToAnotherAppStopsAfterTheGracePeriodWithoutClicking() {
        val device = FakeDevice(expanded = true)
        val machine = machine(device)
        var left = false
        val result = run(device, machine) {
            if (!left && "row:$STEM" in device.clicks) { left = true; device.foreground = "com.example.other" }
        }
        assertEquals(Progress.Failed(AutoError.FOREIGN_APP, AutoStep.FIND_RECORDING), result)
        assertTrue("FOREIGN-SHARE" !in device.clicks)
        assertTrue(!AutoError.FOREIGN_APP.restoreScreen)
    }

    @Test fun launcherInFrontOfTheRecorderIsToleratedWhileLaunching() {
        val device = FakeDevice(expanded = true)
        device.foreground = "com.sec.android.app.launcher"
        val machine = machine(device)
        var arrived = false
        val result = run(device, machine) {
            if (!arrived && device.now >= 3_000) { arrived = true; device.foreground = Selectors.VOICE_NOTE }
        }
        assertEquals(Progress.Succeeded, result)
    }

    @Test fun anotherRecordingOpeningMidFlowIsNeverShared() {
        val device = FakeDevice(expanded = true)
        device.hijackAfterAssist = "통화 김철수_261109_090000"
        val result = run(device, machine(device))
        assertEquals(AutoError.WRONG_RECORDING, (result as Progress.Failed).error)
        assertTrue("share" !in device.clicks)
        assertTrue("text_file" !in device.clicks)
    }

    @Test fun launchThatNeverShowsTheRecorderTimesOut() {
        val device = FakeDevice(expanded = true)
        val machine = machine(device)
        var result: Progress = Progress.Working
        var t = 0L
        while (result == Progress.Working && t < 30_000) { result = machine.advance(null, t); t += 500 }
        assertEquals(Progress.Failed(AutoError.STEP_TIMEOUT, AutoStep.LAUNCH), result)
    }

    @Test fun missingConfirmationFromWeaverTimesOut() {
        val device = FakeDevice(expanded = true, autoSubmit = false)
        val result = run(device, machine(device))
        assertEquals(Progress.Failed(AutoError.STEP_TIMEOUT, AutoStep.AWAIT_SUBMIT), result)
    }

    @Test fun neverClicksDestructiveMediaOrCancelItemsAndNeverReadsScriptText() {
        // The menus contain delete items and the detail holds script nodes whose text throws if read.
        listOf(true, false).forEach { expanded ->
            val device = FakeDevice(expanded = expanded)
            assertEquals(Progress.Succeeded, run(device, machine(device)))
            assertEquals(fullFlow, device.clicks)
            val forbidden = setOf("PLAY", "PAUSE", "DELETE-ROW", "DELETE-TRANSCRIPT", "CANCEL", "SUMMARY", "TRANSLATE",
                "VOICE_FILE", "BOTH", "PDF", "DIRECT", "UP", "SEARCH", "more_options:list")
            assertTrue(device.clicks.none { it in forbidden })
        }
    }

    @Test fun clickGateRefusesNonWhitelistedNodes() {
        val device = FakeDevice(expanded = true, detail = STEM, transcribed = mutableSetOf(STEM))
        val nodes = Selectors.nodes(device.screen())
        val delete = nodes.first { it.idName() == "menu_item_layout" || it.idName() == "more_options" }
        // Build a target around a node that is not allowed for the claimed kind.
        assertTrue(!Selectors.permits(Selectors.Target(ClickTarget.SHARE, delete, delete)))
        val play = Selectors.nodes(FakeDevice(expanded = true).screen()).first { it.idName() == "listRow_play_icon" }
        assertTrue(!Selectors.permits(Selectors.Target(ClickTarget.ROW, play, play)))
    }

    @Test fun selectorsFallBackToTextWhenIdsAreMissing() {
        val chooser = FakeNode(Selectors.CHOOSER, kids = listOf(
            FakeNode(Selectors.CHOOSER, clickable = true, kids = listOf(FakeNode(Selectors.CHOOSER, label = "Weaver")))))
        val screen = Screen(listOf(chooser), Selectors.CHOOSER)
        assertTrue(Selectors.weaver(Selectors.nodes(screen), "Weaver") != null)
        val menu = FakeNode(Selectors.VOICE_NOTE, kids = listOf(FakeNode(Selectors.VOICE_NOTE, label = "텍스트 변환", clickable = true)))
        assertTrue(Selectors.transcribe(Selectors.nodes(Screen(listOf(menu), Selectors.VOICE_NOTE))) != null)
    }
}
