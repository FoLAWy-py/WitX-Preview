import Foundation

/// A download the Gateway asked for: `witx://download?url=<link>&sha256=<hex>`.
///
/// Any page or app can open a `witx://` link, so the link is only a request: the file comes
/// only from this deployment's own server (`server`, set when the app is built), only from
/// its single-use download path, and is kept only if its SHA-256 is the one the link named.
public struct DownloadRequest: Equatable, Sendable {
    public let url: URL
    public let sha256: String

    /// The path the cloud serves its single-use links under.
    public static let path = "/api/v1/downloads/"

    public enum Refusal: Error, Equatable, Sendable {
        case notADownload, noLink, otherServer, notADownloadLink, noDigest
    }

    public static func parse(_ link: URL, server: URL) throws(Refusal) -> DownloadRequest {
        guard link.scheme?.lowercased() == "witx", link.host()?.lowercased() == "download",
            let items = URLComponents(url: link, resolvingAgainstBaseURL: false)?.queryItems
        else { throw .notADownload }
        guard let text = items.first(where: { $0.name == "url" })?.value,
            let target = URLComponents(string: text), let url = target.url
        else { throw .noLink }
        guard target.user == nil, target.password == nil, target.fragment == nil,
            sameOrigin(target, server)
        else { throw .otherServer }
        let path = target.percentEncodedPath
        guard path.hasPrefix(Self.path), path.count > Self.path.count,
            !path.dropFirst(Self.path.count).contains("/")
        else { throw .notADownloadLink }
        guard let digest = items.first(where: { $0.name == "sha256" })?.value?.lowercased(),
            digest.count == 64, digest.allSatisfy(\.isHexDigit)
        else { throw .noDigest }
        return DownloadRequest(url: url, sha256: digest)
    }

    private static func sameOrigin(_ a: URLComponents, _ server: URL) -> Bool {
        guard let b = URLComponents(url: server, resolvingAgainstBaseURL: false),
            let scheme = a.scheme?.lowercased(), ["http", "https"].contains(scheme),
            scheme == b.scheme?.lowercased(), let host = a.host?.lowercased(),
            host == b.host?.lowercased()
        else { return false }
        return port(a) == port(b)
    }

    private static func port(_ c: URLComponents) -> Int? {
        c.port ?? (c.scheme?.lowercased() == "https" ? 443 : 80)
    }
}

/// The name a downloaded file is kept under: the server's name for it, made safe for one
/// directory (no path, no hidden file, no control characters, at most 255 bytes), and not
/// the name of a file already there (`report.pdf` becomes `report 2.pdf`).
public func keptName(_ suggested: String?, taken: Set<String>) -> String {
    var name = (suggested ?? "").split(separator: "/").last.map(String.init) ?? ""
    name = String(name.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) })
        .replacingOccurrences(of: ":", with: "-")
        .trimmingCharacters(in: .whitespaces)
    while name.hasPrefix(".") { name.removeFirst() }
    if name.isEmpty { name = "download" }
    while name.utf8.count > 255 { name.removeFirst() }

    let dot = name.lastIndex(of: ".").flatMap { $0 == name.startIndex ? nil : $0 }
    let stem = dot.map { String(name[..<$0]) } ?? name
    let ext = dot.map { String(name[$0...]) } ?? ""
    var candidate = name
    var n = 2
    while taken.contains(candidate) {
        candidate = "\(stem) \(n)\(ext)"
        n += 1
    }
    return candidate
}

/// Whether the file a link asks for is already here or on its way: the same file asked for
/// again (the Gateway sent the link twice, the agent sent the file twice) changes nothing.
/// `kept` maps the SHA-256 of each kept file to its name; one deleted since (in Files) is
/// not in `present` and is fetched again.
public func alreadyAsked(
    _ sha256: String, downloading: Set<String>, kept: [String: String], present: Set<String>
) -> Bool {
    if downloading.contains(sha256) { return true }
    guard let name = kept[sha256] else { return false }
    return present.contains(name)
}

/// How long to wait before resuming a download that dropped, after `attempt` tries: the
/// phone's own downloader already waits for a network; this spaces out a server that keeps
/// dropping. `nil` once it is time to give up.
public func resumeDelay(attempt: Int) -> Duration? {
    attempt < 8 ? .seconds(min(60, 1 << attempt)) : nil
}
