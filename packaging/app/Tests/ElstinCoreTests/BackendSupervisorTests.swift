import XCTest
@testable import ElstinCore

final class BackendSupervisorTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("elstin-supervisor-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    /// A stand-in for the backend: a shell script.
    private func fakeBackend(_ body: String, mode: Int = 0o755) throws -> URL {
        let url = directory.appendingPathComponent("backend-\(UUID().uuidString).sh")
        try ("#!/bin/sh\n" + body + "\n").write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: url.path)
        return url
    }

    private func supervisor(_ script: URL, arguments: [String] = [], environment: [String: String] = [:]) -> BackendSupervisor {
        BackendSupervisor(.init(executable: script, arguments: arguments, environment: environment.merging(["PATH": "/usr/bin:/bin"]) { first, _ in first }))
    }

    private let ready = #"printf 'ELSTIN_READY {"port": 4321, "token": "tok", "pid": %d}\n' $$"#

    func testItReturnsWhatTheBackendAnnounces() async throws {
        let backend = supervisor(try fakeBackend(ready + "\nexec sleep 30"))

        let announced = try await backend.start(timeout: 10)

        XCTAssertEqual(announced.port, 4321)
        XCTAssertEqual(announced.token, "tok")
        XCTAssertTrue(backend.isRunning)
        await backend.stop(grace: 5)
        XCTAssertFalse(backend.isRunning)
    }

    func testALineSplitAcrossReadsAndNoiseAroundItAreHandled() async throws {
        let backend = supervisor(try fakeBackend("""
            echo 'warming up'
            printf 'ELSTIN_READY {"port": 77, '
            sleep 0.3
            printf '"token": "t", "pid": 2}\\n'
            echo 'and a line after'
            exec sleep 30
            """))

        let announced = try await backend.start(timeout: 10)

        XCTAssertEqual(announced, BackendReady(port: 77, token: "t", pid: 2))
        await backend.stop(grace: 5)
    }

    func testTheEnvironmentAndArgumentsReachTheBackend() async throws {
        let backend = supervisor(
            try fakeBackend(#"printf 'ELSTIN_READY {"port": %s, "token": "%s", "pid": 1}\n' "$MY_PORT" "$1"; exec sleep 30"#),
            arguments: ["from-an-argument"], environment: ["MY_PORT": "4242"]
        )

        let announced = try await backend.start(timeout: 10)

        XCTAssertEqual(announced.port, 4242)
        XCTAssertEqual(announced.token, "from-an-argument")
        await backend.stop(grace: 5)
    }

    func testABackendThatSaysWhyItCannotStartIsReportedWithItsReason() async throws {
        let backend = supervisor(try fakeBackend(#"echo 'ELSTIN_ERROR {"error": "already_running", "message": "Another elstin (process 9) is already using it."}'; exit 2"#))

        do {
            _ = try await backend.start(timeout: 10)
            XCTFail("expected a failure")
        } catch let failure as BackendSupervisor.Failure {
            XCTAssertEqual(failure, .exited(status: 2, code: "already_running", message: "Another elstin (process 9) is already using it."))
            XCTAssertTrue(failure.isAnotherInstance)
            XCTAssertEqual(failure.errorDescription, "Another elstin (process 9) is already using it.")
        }
    }

    func testABackendThatJustDiesIsReportedWithItsExitStatus() async throws {
        let backend = supervisor(try fakeBackend("exit 5"))

        do {
            _ = try await backend.start(timeout: 10)
            XCTFail("expected a failure")
        } catch let failure as BackendSupervisor.Failure {
            XCTAssertEqual(failure, .exited(status: 5, code: nil, message: nil))
            XCTAssertFalse(failure.isAnotherInstance)
            XCTAssertTrue(failure.errorDescription?.contains("5") == true)
        }
    }

    func testABackendThatNeverAnswersTimesOutAndIsStopped() async throws {
        let backend = supervisor(try fakeBackend("exec sleep 30"))

        do {
            _ = try await backend.start(timeout: 0.5)
            XCTFail("expected a timeout")
        } catch let failure as BackendSupervisor.Failure {
            XCTAssertEqual(failure, .timedOut)
        }
        try await Task.sleep(nanoseconds: 500_000_000)
        XCTAssertFalse(backend.isRunning)
    }

    func testAMissingOrNonExecutableBackendIsSaidToBeMissing() async throws {
        let missing = BackendSupervisor(.init(executable: directory.appendingPathComponent("nope")))
        let notExecutable = supervisor(try fakeBackend(ready, mode: 0o644))

        for backend in [missing, notExecutable] {
            do {
                _ = try await backend.start(timeout: 5)
                XCTFail("expected a failure")
            } catch let failure as BackendSupervisor.Failure {
                if case .notFound = failure {} else { XCTFail("\(failure)") }
            }
        }
    }

    func testABackendThatEndsOnItsOwnAfterServingIsReported() async throws {
        let backend = supervisor(try fakeBackend(ready + "\nsleep 0.4\nexit 7"))
        let reported = expectation(description: "unexpected exit reported")
        let status = StatusBox()
        backend.onUnexpectedExit = { code in status.set(code); reported.fulfill() }

        _ = try await backend.start(timeout: 10)
        await fulfillment(of: [reported], timeout: 10)

        XCTAssertEqual(status.value, 7)
    }

    func testStoppingItIsNotAnUnexpectedExit() async throws {
        let backend = supervisor(try fakeBackend(ready + "\nexec sleep 30"))
        let reported = expectation(description: "must not be reported")
        reported.isInverted = true
        backend.onUnexpectedExit = { _ in reported.fulfill() }

        _ = try await backend.start(timeout: 10)
        await backend.stop(grace: 5)
        await fulfillment(of: [reported], timeout: 1)
    }

    func testABackendThatIgnoresTermIsKilledAfterTheGracePeriod() async throws {
        let backend = supervisor(try fakeBackend(ready + "\ntrap '' TERM\nwhile :; do sleep 0.1; done"))
        _ = try await backend.start(timeout: 10)

        let started = Date()
        await backend.stop(grace: 0.5)

        XCTAssertFalse(backend.isRunning)
        XCTAssertLessThan(Date().timeIntervalSince(started), 5)
    }

    func testAChattyBackendIsNotBlockedByAnUnreadPipe() async throws {
        let marker = directory.appendingPathComponent("finished")
        let backend = supervisor(try fakeBackend("""
            \(ready)
            head -c 2000000 /dev/zero | tr '\\0' 'x'
            head -c 2000000 /dev/zero | tr '\\0' 'y' 1>&2
            echo done > '\(marker.path)'
            exec sleep 30
            """))
        _ = try await backend.start(timeout: 10)

        let deadline = Date().addingTimeInterval(15)
        while !FileManager.default.fileExists(atPath: marker.path), Date() < deadline {
            try await Task.sleep(nanoseconds: 100_000_000)
        }

        XCTAssertTrue(FileManager.default.fileExists(atPath: marker.path), "the backend stalled writing to a pipe nobody read")
        await backend.stop(grace: 5)
    }
}

private final class StatusBox: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: Int32?
    func set(_ value: Int32) { lock.withLock { stored = value } }
    var value: Int32? { lock.withLock { stored } }
}
