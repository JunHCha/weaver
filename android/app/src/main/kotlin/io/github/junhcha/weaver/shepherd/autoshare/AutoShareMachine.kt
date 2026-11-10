package io.github.junhcha.weaver.shepherd.autoshare

/** [sizeBytes] only sizes the transcription time limit; it is metadata from MediaStore. */
data class AutoShareJob(val callId: String, val stem: String, val sizeBytes: Long)

enum class AutoStep(val label: String) {
    LAUNCH("녹음 앱 실행"),
    FIND_RECORDING("녹음 찾기"),
    PREPARE_TRANSCRIPT("텍스트 변환 시작"),
    WAIT_TRANSCRIPT("텍스트 변환 대기"),
    OPEN_OPTIONS("옵션 열기"),
    CHOOSE_SHARE("공유 메뉴"),
    CHOOSE_TEXT_FILE("텍스트 파일 선택"),
    PICK_APP("공유할 앱 선택"),
    AWAIT_SUBMIT("Weaver 수신 확인"),
}

enum class AutoError(val label: String, val restoreScreen: Boolean = true) {
    STEP_TIMEOUT("단계 시간 초과"),
    RECORDING_NOT_FOUND("녹음을 찾지 못함"),
    WRONG_RECORDING("다른 녹음이 열림"),
    UNKNOWN_DIALOG("알 수 없는 대화상자"),
    FOREIGN_APP("다른 앱으로 전환됨", restoreScreen = false),
    USER_TOUCH("사용자 터치로 중단", restoreScreen = false),
    TRANSCRIBE_TIMEOUT("텍스트 변환 시간 초과"),
    TRANSCRIBE_FAILED("텍스트 변환 실패"),
    APP_NOT_IN_SHEET("공유 시트에 Weaver 없음"),
    LAUNCH_FAILED("녹음 앱 실행 실패"),
    SERVICE_STOPPED("접근성 서비스 중지", restoreScreen = false),
}

sealed interface Progress {
    data object Working : Progress
    data object Succeeded : Progress
    data class Failed(val error: AutoError, val step: AutoStep) : Progress
}

data class AutoShareTiming(
    val stepTimeoutMs: Long = 10_000,
    val settleMs: Long = 600,
    val foreignGraceMs: Long = 2_000,
    val transcribeStartGraceMs: Long = 10_000,
    val submitTimeoutMs: Long = 20_000,
    val maxListPages: Int = 30,
    val maxSheetPages: Int = 5,
    val maxTranscribeClicks: Int = 2,
    val maxBackPresses: Int = 3,
)

/**
 * Pure state machine for sharing one call's script. Every call to [advance] re-reads the screen, so
 * the same logic handles the expanded layout (list and detail together) and the folded one.
 * It reads text only for lookups against fixed phrases or the recording's file name; nothing it sees
 * is stored, and script content nodes are never traversed (see [Selectors.nodes]).
 */
class AutoShareMachine(
    private val job: AutoShareJob,
    startedAt: Long,
    private val submitted: () -> Boolean,
    ownPackage: String,
    private val appLabel: String = "Weaver",
    private val timing: AutoShareTiming = AutoShareTiming(),
) {
    private val allowedPackages = Selectors.allowedPackages(ownPackage)
    var step = AutoStep.LAUNCH
        private set
    private var stepSince = startedAt
    private var lastAction = Long.MIN_VALUE / 2
    private var foreignSince: Long? = null
    private var listPages = 0
    private var sheetPages = 0
    private var backPresses = 0
    private var rowClicks = 0
    private var lastListSignature: Int? = null
    private var transcribeClicks = 0
    private var transcribeStartedAt: Long? = null
    private var idleSince = startedAt
    private var finished: Progress? = null

    fun interrupt(error: AutoError): Progress.Failed = Progress.Failed(error, step).also { finished = it }

    fun advance(screen: Screen?, now: Long): Progress {
        finished?.let { return it }
        val result = evaluate(screen, now)
        if (result != Progress.Working) finished = result
        return result
    }

    private fun fail(error: AutoError) = Progress.Failed(error, step)

    private fun moveTo(next: AutoStep, now: Long) {
        step = next
        stepSince = now
    }

    private fun canAct(now: Long) = now - lastAction >= timing.settleMs

    private fun press(target: Selectors.Target, now: Long): Boolean {
        if (!Selectors.permits(target)) return false
        lastAction = now
        return target.node.click()
    }

    private fun evaluate(screen: Screen?, now: Long): Progress {
        if (screen != null) {
            val before = step
            val result = process(screen, now)
            if (result != null) return result
            if (step != before) return Progress.Working
        }
        if (step == AutoStep.WAIT_TRANSCRIPT) {
            val started = transcribeStartedAt ?: now
            if (now - started > transcribeLimit()) return fail(AutoError.TRANSCRIBE_TIMEOUT)
        } else if (step == AutoStep.AWAIT_SUBMIT) {
            if (now - stepSince > timing.submitTimeoutMs) return fail(AutoError.STEP_TIMEOUT)
        } else if (now - stepSince > timing.stepTimeoutMs) {
            return fail(AutoError.STEP_TIMEOUT)
        }
        return Progress.Working
    }

    private fun transcribeLimit() = AutoSharePolicy.transcribeTimeoutMs(job.sizeBytes)

    /** Returns a terminal result, or null to keep going (the step may have changed). */
    private fun process(screen: Screen, now: Long): Progress? {
        if (step == AutoStep.AWAIT_SUBMIT || step == AutoStep.PICK_APP) {
            if (submitted()) return Progress.Succeeded
        }
        val active = screen.activePackage
        if (active != null && active !in allowedPackages) {
            if (step == AutoStep.LAUNCH) return null
            val since = foreignSince ?: now.also { foreignSince = it }
            return if (now - since >= timing.foreignGraceMs) fail(AutoError.FOREIGN_APP) else null
        }
        foreignSince = null
        if (step == AutoStep.AWAIT_SUBMIT) return null
        val nodes = Selectors.nodes(screen)
        val dialog = Selectors.dialogTitle(nodes)
        val shareDialog = dialog == Selectors.SHARE_TEXT
        if (dialog != null && !(shareDialog && step >= AutoStep.OPEN_OPTIONS)) return fail(AutoError.UNKNOWN_DIALOG)
        repeat(6) {
            val before = step
            val result = handle(screen, nodes, shareDialog, now)
            if (result != null) return result
            if (step == before) return null
        }
        return null
    }

    private fun handle(screen: Screen, nodes: List<UiNode>, shareDialog: Boolean, now: Long): Progress? {
        when (step) {
            AutoStep.LAUNCH -> if (screen.activePackage == Selectors.VOICE_NOTE) moveTo(AutoStep.FIND_RECORDING, now)
            AutoStep.FIND_RECORDING -> return findRecording(screen, nodes, now)
            AutoStep.PREPARE_TRANSCRIPT -> return prepareTranscript(nodes, now)
            AutoStep.WAIT_TRANSCRIPT -> return waitTranscript(nodes, now)
            AutoStep.OPEN_OPTIONS -> {
                if (shareDialog) { moveTo(AutoStep.CHOOSE_TEXT_FILE, now); return null }
                wrongRecording(nodes)?.let { return it }
                if (Selectors.shareMenu(nodes) != null) { moveTo(AutoStep.CHOOSE_SHARE, now); return null }
                if (Selectors.detail(nodes, job.stem) == Selectors.Detail.MATCH && canAct(now)) {
                    val target = Selectors.moreOptions(nodes)
                    if (target != null && press(target, now)) moveTo(AutoStep.CHOOSE_SHARE, now)
                }
            }
            AutoStep.CHOOSE_SHARE -> {
                if (shareDialog) { moveTo(AutoStep.CHOOSE_TEXT_FILE, now); return null }
                wrongRecording(nodes)?.let { return it }
                if (canAct(now)) Selectors.shareMenu(nodes)?.let { press(it, now) }
            }
            AutoStep.CHOOSE_TEXT_FILE -> {
                wrongRecording(nodes)?.let { return it }
                if (shareDialog && canAct(now)) {
                    val target = Selectors.textFile(nodes)
                    if (target != null && press(target, now)) moveTo(AutoStep.PICK_APP, now)
                }
            }
            AutoStep.PICK_APP -> return pickApp(screen, nodes, now)
            AutoStep.AWAIT_SUBMIT -> Unit
        }
        return null
    }

    /** Refuse to share when another recording's detail is showing. */
    private fun wrongRecording(nodes: List<UiNode>): Progress? =
        if (Selectors.detail(nodes, job.stem) == Selectors.Detail.OTHER) fail(AutoError.WRONG_RECORDING) else null

    private fun findRecording(screen: Screen, nodes: List<UiNode>, now: Long): Progress? {
        if (screen.activePackage != Selectors.VOICE_NOTE) return null
        val detail = Selectors.detail(nodes, job.stem)
        if (detail == Selectors.Detail.MATCH) { moveTo(AutoStep.PREPARE_TRANSCRIPT, now); return null }
        if (!canAct(now)) return null
        val row = Selectors.row(nodes, job.stem)
        // Two-pane (unfolded): the list beside another detail is clickable, so try the row first.
        if (row != null && rowClicks == 0) { rowClicks++; press(row, now); return null }
        if (detail == Selectors.Detail.OTHER && Selectors.hasDetailContent(nodes)) {
            // The row click did not open it: another recording's detail covers the list (folded). Go back.
            if (backPresses >= timing.maxBackPresses) return fail(AutoError.RECORDING_NOT_FOUND)
            backPresses++
            lastAction = now
            screen.goBack()
            rowClicks = 0
            return null
        }
        if (row != null) { rowClicks++; press(row, now); return null }
        val titles = Selectors.listTitles(nodes)
        if (titles.isNotEmpty()) {
            val signature = titles.mapNotNull { it.text }.hashCode()
            if (signature == lastListSignature || listPages >= timing.maxListPages) return fail(AutoError.RECORDING_NOT_FOUND)
            val container = Selectors.scrollContainer(nodes, titles.first()) ?: return fail(AutoError.RECORDING_NOT_FOUND)
            lastListSignature = signature
            lastAction = now
            if (!container.scroll()) return fail(AutoError.RECORDING_NOT_FOUND)
            listPages++
            stepSince = now
            return null
        }
        if (detail == Selectors.Detail.OTHER) {
            // The app reopened on another recording: leave it with Back, then look in the list.
            if (backPresses >= timing.maxBackPresses) return fail(AutoError.RECORDING_NOT_FOUND)
            backPresses++
            lastAction = now
            screen.goBack()
        }
        return null
    }

    private fun prepareTranscript(nodes: List<UiNode>, now: Long): Progress? {
        wrongRecording(nodes)?.let { return it }
        val hasProgress = Selectors.hasProgress(nodes)
        if (Selectors.hasTranscript(nodes) && !hasProgress) { moveTo(AutoStep.OPEN_OPTIONS, now); return null }
        if (hasProgress) { enterWait(now); return null }
        if (!canAct(now)) return null
        val menu = Selectors.transcribe(nodes)
        if (menu != null) {
            if (transcribeClicks >= timing.maxTranscribeClicks) return fail(AutoError.TRANSCRIBE_FAILED)
            if (press(menu, now)) { transcribeClicks++; enterWait(now) }
            return null
        }
        Selectors.assist(nodes)?.let { press(it, now) }
        return null
    }

    private fun enterWait(now: Long) {
        if (transcribeStartedAt == null) transcribeStartedAt = now
        idleSince = now
        moveTo(AutoStep.WAIT_TRANSCRIPT, now)
    }

    private fun waitTranscript(nodes: List<UiNode>, now: Long): Progress? {
        wrongRecording(nodes)?.let { return it }
        val hasProgress = Selectors.hasProgress(nodes)
        if (Selectors.hasTranscript(nodes) && !hasProgress) { moveTo(AutoStep.OPEN_OPTIONS, now); return null }
        if (hasProgress) { idleSince = now; return null }
        if (now - idleSince >= timing.transcribeStartGraceMs) {
            // Nothing is running and no script appeared: the request was lost or cancelled.
            if (transcribeClicks >= timing.maxTranscribeClicks) return fail(AutoError.TRANSCRIBE_FAILED)
            moveTo(AutoStep.PREPARE_TRANSCRIPT, now)
        }
        return null
    }

    private fun pickApp(screen: Screen, nodes: List<UiNode>, now: Long): Progress? {
        if (screen.activePackage != Selectors.CHOOSER) return null
        if (!Selectors.chooserHasApps(nodes) || !canAct(now)) return null
        val target = Selectors.weaver(nodes, appLabel)
        if (target != null) {
            if (press(target, now)) moveTo(AutoStep.AWAIT_SUBMIT, now)
            return null
        }
        if (sheetPages >= timing.maxSheetPages) return fail(AutoError.APP_NOT_IN_SHEET)
        val container = Selectors.scrollContainer(nodes.filter { it.packageName == Selectors.CHOOSER }, null)
            ?: return fail(AutoError.APP_NOT_IN_SHEET)
        lastAction = now
        if (!container.scroll()) return fail(AutoError.APP_NOT_IN_SHEET)
        sheetPages++
        stepSince = now
        return null
    }
}
