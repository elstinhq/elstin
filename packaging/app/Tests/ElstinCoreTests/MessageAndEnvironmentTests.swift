import XCTest
@testable import ElstinCore

final class BackendMessageTests: XCTestCase {
    func testTheReadyLineGivesPortTokenAndPid() {
        let line = #"ELSTIN_READY {"port": 51234, "token": "abc_DEF-123", "pid": 4321}"#

        XCTAssertEqual(BackendMessage.parse(line: line), .ready(BackendReady(port: 51234, token: "abc_DEF-123", pid: 4321)))
    }

    func testTheErrorLineGivesTheBackendsOwnReason() {
        let line = #"ELSTIN_ERROR {"error": "already_running", "message": "Another elstin (process 7) is already using it."}"#

        XCTAssertEqual(BackendMessage.parse(line: line), .failed(code: "already_running", message: "Another elstin (process 7) is already using it."))
    }

    func testAnythingElseIsNotAMessage() {
        for line in ["", "INFO started", "ELSTIN_READY", "ELSTIN_READY not json", #"ELSTIN_READY {"port": "x"}"#, "elstin_ready {}"] {
            XCTAssertNil(BackendMessage.parse(line: line), line)
        }
    }

    func testThePageAddressCarriesTheTokenOnce() {
        let ready = BackendReady(port: 51234, token: "tok-_9", pid: 1)

        XCTAssertEqual(ready.pageURL.absoluteString, "http://127.0.0.1:51234/?token=tok-_9")
        XCTAssertEqual(ready.statusURL.absoluteString, "http://127.0.0.1:51234/api/status")
    }
}

final class ShellEnvironmentTests: XCTestCase {
    private func output(_ body: String, noiseBefore: String = "", noiseAfter: String = "") -> Data {
        Data((noiseBefore + "__ELSTIN_ENV_BEGIN__\n" + body + "__ELSTIN_ENV_END__\n" + noiseAfter).utf8)
    }

    func testVariablesAreReadFromBetweenTheMarkersWhateverTheShellPrintsAround() {
        let data = output("PATH=/opt/homebrew/bin:/usr/bin\0JAVA_HOME=/Library/Java/jdk-21\0EMPTY=\0WITH_EQUALS=a=b=c\0",
                          noiseBefore: "Welcome back!\nLast login: today\n", noiseAfter: "bye\n")

        XCTAssertEqual(ShellEnvironment.parse(output: data), [
            "PATH": "/opt/homebrew/bin:/usr/bin", "JAVA_HOME": "/Library/Java/jdk-21", "EMPTY": "", "WITH_EQUALS": "a=b=c",
        ])
    }

    func testAValueMayContainANewline() {
        let data = output("MOTD=line one\nline two\0A=1\0")

        XCTAssertEqual(ShellEnvironment.parse(output: data)?["MOTD"], "line one\nline two")
        XCTAssertEqual(ShellEnvironment.parse(output: data)?["A"], "1")
    }

    func testOutputWithoutBothMarkersIsNotTrusted() {
        XCTAssertNil(ShellEnvironment.parse(output: Data("__ELSTIN_ENV_BEGIN__\nA=1\0".utf8)))  // cut off: the shell was killed
        XCTAssertNil(ShellEnvironment.parse(output: Data("A=1\0".utf8)))
        XCTAssertNil(ShellEnvironment.parse(output: output("")))
        XCTAssertNil(ShellEnvironment.parse(output: Data()))
    }

    func testThePathComesFromTheShellFirstThenWhatTheAppHadThenTheUsualToolDirectories() {
        let merged = ShellEnvironment.merged(
            gui: ["PATH": "/usr/bin:/bin", "HOME": "/Users/me"],
            shell: ["PATH": "/Users/me/.nvm/bin:/opt/homebrew/bin:/usr/bin", "JAVA_HOME": "/jdk"]
        )

        XCTAssertEqual(merged["PATH"], "/Users/me/.nvm/bin:/opt/homebrew/bin:/usr/bin:/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin")
        XCTAssertEqual(merged["JAVA_HOME"], "/jdk")
        XCTAssertEqual(merged["HOME"], "/Users/me")
    }

    func testWithoutAShellThePathStillGetsTheUsualToolDirectories() {
        let merged = ShellEnvironment.merged(gui: ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"], shell: nil)

        XCTAssertEqual(merged["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin")
    }

    func testNoDuplicateOrEmptyPathEntries() {
        let merged = ShellEnvironment.merged(gui: ["PATH": "/usr/bin::/bin"], shell: ["PATH": "/usr/bin:/usr/bin::/opt/homebrew/bin"])

        XCTAssertEqual(merged["PATH"]?.split(separator: ":").count, Set(merged["PATH"]!.split(separator: ":")).count)
        XCTAssertFalse(merged["PATH"]!.contains("::"))
    }

    func testAnOldDatabaseUrlInTheShellDoesNotSteerTheApp() {
        let fromTheShell = ["DATABASE_URL": "postgresql+psycopg://elstin:elstin@localhost:5433/elstin", "SHLVL": "3", "TERM_PROGRAM": "iTerm.app", "FOO": "bar"]

        let merged = ShellEnvironment.merged(gui: [:], shell: fromTheShell)

        XCTAssertNil(merged["DATABASE_URL"])
        XCTAssertNil(merged["SHLVL"])
        XCTAssertNil(merged["TERM_PROGRAM"])
        XCTAssertEqual(merged["FOO"], "bar")
    }

    func testADatabaseUrlTheAppWasStartedWithIsKept() {
        let merged = ShellEnvironment.merged(gui: ["DATABASE_URL": "sqlite:////tmp/x.db"], shell: ["DATABASE_URL": "postgresql://old"])

        XCTAssertEqual(merged["DATABASE_URL"], "sqlite:////tmp/x.db")
    }

    func testARealLoginShellAnswers() async throws {
        let captured = await ShellEnvironment.capture(shell: "/bin/sh", timeout: 20)
        let environment = try XCTUnwrap(captured)

        XCTAssertNotNil(environment["PATH"])
        XCTAssertNotNil(environment["HOME"])
    }

    func testAShellThatNeverAnswersIsGivenUpOn() async {
        let started = Date()

        let environment = await ShellEnvironment.capture(shell: "/bin/sleep", timeout: 1)  // `sleep -ilc ...` fails or hangs: either way no environment

        XCTAssertNil(environment)
        XCTAssertLessThan(Date().timeIntervalSince(started), 8)
    }
}

final class NavigationPolicyTests: XCTestCase {
    func testElstinsOwnPageStaysInTheWindow() {
        XCTAssertEqual(NavigationPolicy.decide(url: URL(string: "http://127.0.0.1:51234/settings"), backendPort: 51234), .allow)
        XCTAssertEqual(NavigationPolicy.decide(url: URL(string: "about:blank"), backendPort: 51234), .allow)
    }

    func testOtherAddressesOpenInTheUsersBrowser() {
        for text in ["https://github.com/acme/shop/pull/7", "http://127.0.0.1:9999/", "http://localhost:51234/", "https://127.0.0.1:51234/", "mailto:a@b.c"] {
            let url = URL(string: text)!
            XCTAssertEqual(NavigationPolicy.decide(url: url, backendPort: 51234), .openExternally(url), text)
        }
    }

    func testSchemesThatAreNotTheWebGoNowhere() {
        for text in ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi", "about:srcdoc", "x-apple.systempreferences:", "ftp://x"] {
            XCTAssertEqual(NavigationPolicy.decide(url: URL(string: text), backendPort: 51234), .cancel, text)
        }
        XCTAssertEqual(NavigationPolicy.decide(url: nil, backendPort: 51234), .cancel)
    }

    func testBeforeTheBackendIsUpNothingOfElstinsIsAllowed() {
        XCTAssertEqual(NavigationPolicy.decide(url: URL(string: "http://127.0.0.1:51234/"), backendPort: nil), .openExternally(URL(string: "http://127.0.0.1:51234/")!))
    }
}

final class WorkStatusTests: XCTestCase {
    func testItDecodesWhatTheBackendSends() throws {
        let json = #"{"busy": true, "stories": ["ENG-1", "ENG-2"], "po_sessions": 1, "publishing": 0}"#

        let status = try JSONDecoder().decode(WorkStatus.self, from: Data(json.utf8))

        XCTAssertEqual(status, WorkStatus(busy: true, stories: ["ENG-1", "ENG-2"], poSessions: 1, publishing: 0))
    }

    func testAnIdleBackendNeedsNoConfirmationToQuit() {
        XCTAssertNil(WorkStatus.idle.quitPrompt)
    }

    func testTheSummaryNamesWhatIsRunning() {
        XCTAssertEqual(WorkStatus(busy: true, stories: ["ENG-1"], poSessions: 0, publishing: 0).summary, "1 story (ENG-1)")
        XCTAssertEqual(WorkStatus(busy: true, stories: ["A-1", "A-2", "A-3"], poSessions: 0, publishing: 0).summary, "3 stories (A-1, A-2, A-3)")
        XCTAssertEqual(WorkStatus(busy: true, stories: (1...7).map { "A-\($0)" }, poSessions: 0, publishing: 0).summary, "7 stories (A-1, A-2, A-3 and 4 more)")
        XCTAssertEqual(WorkStatus(busy: true, stories: [], poSessions: 2, publishing: 0).summary, "2 PO drafts")
        XCTAssertEqual(WorkStatus(busy: true, stories: ["A-1"], poSessions: 1, publishing: 1).summary, "1 story (A-1), a PO draft and a publish to JIRA")
    }

    func testTheQuitPromptSaysWhatQuittingCostsAndWhatToDoInstead() throws {
        let prompt = try XCTUnwrap(WorkStatus(busy: true, stories: ["ENG-1"], poSessions: 0, publishing: 0).quitPrompt)

        XCTAssertEqual(prompt.title, "Elstin is still working")
        XCTAssertTrue(prompt.detail.contains("1 story (ENG-1)"))
        XCTAssertTrue(prompt.detail.contains("last checkpoint"))
        XCTAssertTrue(prompt.detail.contains("Closing the window instead leaves everything running"))
    }
}
