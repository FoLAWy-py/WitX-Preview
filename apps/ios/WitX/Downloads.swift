import CryptoKit
import Foundation
import Observation
import WitXDownloads
import os

/// The app's downloads: the phone's own background downloader fetches each file,
/// so a download goes on while the app is suspended and resumes after the network drops.
/// A file is moved into Documents (shown in Files as "WitX") only once its SHA-256 is the
/// one the link named, so a file there is a whole, verified file: what the Gateway checks.
@MainActor @Observable
final class Downloads {
    static let shared = Downloads()

    struct Transfer: Identifiable {
        let id: Int
        var sha256: String
        var name: String
        var received: Int64 = 0
        var expected: Int64 = 0
        var note: String?
    }

    struct Kept: Identifiable, Hashable {
        var id: URL { url }
        let url: URL
        let size: Int64
    }

    private(set) var transfers: [Int: Transfer] = [:]
    private(set) var files: [Kept] = []
    private(set) var refused: String?

    /// Where kept files go: the app's own folder, shared with Files.
    nonisolated static let folder = URL.documentsDirectory
    /// This deployment's server, set when the app is built (`WITX_SERVER_ORIGIN`).
    nonisolated static let server = URL(
        string: Bundle.main.object(forInfoDictionaryKey: "WITXServerOrigin") as? String ?? "")

    @ObservationIgnored var backgroundDone: (() -> Void)?
    @ObservationIgnored private let worker = Worker()
    @ObservationIgnored private lazy var session: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: "com.witx.downloads")
        config.sessionSendsLaunchEvents = true
        config.isDiscretionary = false
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        return URLSession(configuration: config, delegate: worker, delegateQueue: queue)
    }()

    private init() {
        worker.owner = self
        Task { await reload() }
    }

    /// A `witx://download` link: starts the download it asks for, if it is one of ours.
    func open(_ link: URL) {
        guard let server = Self.server else {
            refused = "This build has no server (WITX_SERVER_ORIGIN)."
            return
        }
        do {
            let request = try DownloadRequest.parse(link, server: server)
            refused = nil
            let downloading = Set(transfers.values.map(\.sha256))
            // Read now: on a cold start by this link, the list may not be loaded yet.
            let present = Set(
                (try? FileManager.default.contentsOfDirectory(atPath: Self.folder.path())) ?? [])
            if alreadyAsked(
                request.sha256, downloading: downloading, kept: Digests.shared.all(),
                present: present)
            {
                Log.app.notice("asked again for a file already here or on its way")
                return
            }
            start(session.downloadTask(with: request.url), Ticket(sha256: request.sha256))
        } catch {
            refused = "Refused \(link.absoluteString): \(error)"
            Log.app.notice("refused a link: \(String(describing: error), privacy: .public)")
        }
    }

    func delete(_ file: Kept) {
        try? FileManager.default.removeItem(at: file.url)
        Task { await reload() }
    }

    fileprivate func start(_ task: URLSessionDownloadTask, _ ticket: Ticket) {
        task.taskDescription = ticket.encoded
        transfers[task.taskIdentifier] = Transfer(
            id: task.taskIdentifier, sha256: ticket.sha256,
            name: task.originalRequest?.url?.lastPathComponent ?? "…",
            note: ticket.attempt > 0 ? "Resumed (\(ticket.attempt))" : nil)
        task.resume()
    }

    fileprivate func progress(_ id: Int, name: String?, received: Int64, expected: Int64) {
        transfers[id, default: Transfer(id: id, sha256: "", name: "…")].received = received
        transfers[id]?.expected = expected
        if let name { transfers[id]?.name = name }
    }

    fileprivate func finished(_ id: Int) {
        transfers[id] = nil
        Task { await reload() }
    }

    fileprivate func failed(_ id: Int, _ note: String) {
        transfers[id]?.note = note
    }

    /// A dropped download: resume it from where it stopped, after a while, or give up.
    fileprivate func dropped(_ id: Int, resumeData: Data, ticket: Ticket) {
        guard let delay = resumeDelay(attempt: ticket.attempt) else {
            failed(id, "Gave up after \(ticket.attempt) tries")
            return
        }
        transfers[id]?.note = "Dropped, resuming in \(delay)"
        Task {
            try? await Task.sleep(for: delay)
            transfers[id] = nil
            start(session.downloadTask(withResumeData: resumeData), ticket.next)
        }
    }

    /// Reconnects to downloads the system kept going while the app was not running.
    func reload() async {
        for task in await session.allTasks {
            guard let task = task as? URLSessionDownloadTask, transfers[task.taskIdentifier] == nil
            else { continue }
            transfers[task.taskIdentifier] = Transfer(
                id: task.taskIdentifier, sha256: Ticket(task.taskDescription)?.sha256 ?? "",
                name: task.originalRequest?.url?.lastPathComponent ?? "…",
                received: task.countOfBytesReceived,
                expected: task.countOfBytesExpectedToReceive)
        }
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey]
        let urls =
            (try? FileManager.default.contentsOfDirectory(
                at: Self.folder, includingPropertiesForKeys: keys, options: .skipsHiddenFiles)) ?? []
        files =
            urls
            .map { (url: $0, values: try? $0.resourceValues(forKeys: Set(keys))) }
            .filter { $0.url.lastPathComponent != "Inbox" }
            .sorted { ($0.values?.contentModificationDate ?? .distantPast) > ($1.values?.contentModificationDate ?? .distantPast) }
            .map { Kept(url: $0.url, size: Int64($0.values?.fileSize ?? 0)) }
    }
}

/// What a download carries with it across resumes and app launches (the task's description).
struct Ticket: Codable, Sendable {
    var sha256: String
    var attempt = 0

    var next: Ticket { Ticket(sha256: sha256, attempt: attempt + 1) }
    var encoded: String { String(decoding: try! JSONEncoder().encode(self), as: UTF8.self) }
    init(sha256: String, attempt: Int = 0) {
        self.sha256 = sha256
        self.attempt = attempt
    }
    init?(_ text: String?) {
        guard let data = text?.data(using: .utf8),
            let t = try? JSONDecoder().decode(Ticket.self, from: data)
        else { return nil }
        self = t
    }
}

/// Which kept file has which SHA-256 (in Application Support, not shown in Files), so the
/// same file asked for again is not fetched twice. Written on the session's queue, read on
/// the main actor.
final class Digests: @unchecked Sendable {
    static let shared = Digests()
    private let lock = NSLock()
    private let url = URL.applicationSupportDirectory.appending(path: "kept.json")

    func all() -> [String: String] {
        lock.withLock { load() }
    }

    func record(_ sha256: String, as name: String) {
        lock.withLock {
            var kept = load()
            kept[sha256] = name
            try? FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? JSONEncoder().encode(kept).write(to: url, options: .atomic)
        }
    }

    private func load() -> [String: String] {
        guard let data = try? Data(contentsOf: url) else { return [:] }
        return (try? JSONDecoder().decode([String: String].self, from: data)) ?? [:]
    }
}

enum Log {
    static let app = Logger(subsystem: "com.witx.app", category: "downloads")
}

/// The session's delegate, on the session's serial queue. A finished file must be moved
/// before `didFinishDownloadingTo` returns, so it is checked and kept here, off the main
/// actor; the main actor only hears what happened.
private final class Worker: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    // Set once in Downloads.init, before the session exists.
    nonisolated(unsafe) weak var owner: Downloads?

    func urlSession(
        _ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData _: Int64,
        totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64
    ) {
        let id = downloadTask.taskIdentifier
        let name = downloadTask.response?.suggestedFilename
        Task { @MainActor [owner] in
            owner?.progress(
                id, name: name, received: totalBytesWritten, expected: totalBytesExpectedToWrite)
        }
    }

    func urlSession(
        _ session: URLSession, downloadTask: URLSessionDownloadTask,
        didFinishDownloadingTo location: URL
    ) {
        let id = downloadTask.taskIdentifier
        let outcome = keep(location, downloadTask)
        Task { @MainActor [owner] in
            switch outcome {
            case .success: owner?.finished(id)
            case .failure(let note): owner?.failed(id, note.text)
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let error else { return }
        let id = task.taskIdentifier
        let resume = (error as NSError).userInfo[NSURLSessionDownloadTaskResumeData] as? Data
        let ticket = Ticket(task.taskDescription)
        Log.app.notice("download \(id) stopped: \(error.localizedDescription, privacy: .public)")
        Task { @MainActor [owner] in
            if let resume, let ticket {
                owner?.dropped(id, resumeData: resume, ticket: ticket)
            } else {
                owner?.failed(id, error.localizedDescription)
            }
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        Task { @MainActor [owner] in
            owner?.backgroundDone?()
            owner?.backgroundDone = nil
        }
    }

    struct Note: Error { let text: String }

    private func keep(_ location: URL, _ task: URLSessionDownloadTask) -> Result<URL, Note> {
        let fm = FileManager.default
        defer { try? fm.removeItem(at: location) }
        guard let status = (task.response as? HTTPURLResponse)?.statusCode, (200..<300).contains(status)
        else {
            let status = (task.response as? HTTPURLResponse)?.statusCode ?? 0
            return .failure(Note(text: "The server answered \(status)"))
        }
        guard let ticket = Ticket(task.taskDescription) else {
            return .failure(Note(text: "The download lost its digest"))
        }
        guard let actual = try? sha256(of: location), actual == ticket.sha256 else {
            return .failure(Note(text: "The file is not the one the link named (SHA-256)"))
        }
        let taken = Set((try? fm.contentsOfDirectory(atPath: Downloads.folder.path())) ?? [])
        let name = keptName(task.response?.suggestedFilename, taken: taken)
        let target = Downloads.folder.appending(path: name)
        do {
            try fm.moveItem(at: location, to: target)
        } catch {
            return .failure(Note(text: "Could not keep the file: \(error.localizedDescription)"))
        }
        Digests.shared.record(ticket.sha256, as: name)
        Log.app.notice("kept \(name, privacy: .public)")
        return .success(target)
    }

    private func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hash = SHA256()
        while let chunk = try handle.read(upToCount: 1 << 20), !chunk.isEmpty {
            hash.update(data: chunk)
        }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
