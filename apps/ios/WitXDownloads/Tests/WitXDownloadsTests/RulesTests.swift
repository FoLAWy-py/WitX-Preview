import Foundation
import Testing

@testable import WitXDownloads

let server = URL(string: "http://192.168.1.20:47088")!
let digest = String(repeating: "ab", count: 32)

func link(_ target: String, sha256: String? = digest) -> URL {
    var c = URLComponents(string: "witx://download")!
    c.queryItems = [URLQueryItem(name: "url", value: target)]
        + (sha256.map { [URLQueryItem(name: "sha256", value: $0)] } ?? [])
    return c.url!
}

func refusal(_ url: URL) -> DownloadRequest.Refusal? {
    do {
        _ = try DownloadRequest.parse(url, server: server)
        return nil
    } catch { return error }
}

@Test func aLinkToThisServersDownloadPathIsAccepted() throws {
    let request = try DownloadRequest.parse(
        link("http://192.168.1.20:47088/api/v1/downloads/tok_123"), server: server)
    #expect(request.url.absoluteString == "http://192.168.1.20:47088/api/v1/downloads/tok_123")
    #expect(request.sha256 == digest)
}

@Test func aLinkToAnotherServerIsRefused() {
    #expect(refusal(link("http://evil.example/api/v1/downloads/tok")) == .otherServer)
    #expect(refusal(link("http://192.168.1.20:9000/api/v1/downloads/tok")) == .otherServer)
    #expect(refusal(link("https://192.168.1.20:47088/api/v1/downloads/tok")) == .otherServer)
    #expect(refusal(link("http://user@192.168.1.20:47088/api/v1/downloads/tok")) == .otherServer)
}

@Test func aLinkOutsideTheDownloadPathIsRefused() {
    #expect(refusal(link("http://192.168.1.20:47088/api/v1/tasks")) == .notADownloadLink)
    #expect(refusal(link("http://192.168.1.20:47088/api/v1/downloads/")) == .notADownloadLink)
    #expect(refusal(link("http://192.168.1.20:47088/api/v1/downloads/a/../../tasks"))
        == .notADownloadLink)
}

@Test func aLinkWithoutTheFilesDigestIsRefused() {
    let target = "http://192.168.1.20:47088/api/v1/downloads/tok"
    #expect(refusal(link(target, sha256: nil)) == .noDigest)
    #expect(refusal(link(target, sha256: "abc")) == .noDigest)
}

@Test func otherWitxLinksAreNotDownloads() {
    #expect(refusal(URL(string: "witx://settings?url=x")!) == .notADownload)
    #expect(refusal(URL(string: "https://download?url=x")!) == .notADownload)
    #expect(refusal(URL(string: "witx://download")!) == .notADownload)
}

@Test func aFileKeepsTheServersNameWithoutAPath() {
    #expect(keptName("report.pdf", taken: []) == "report.pdf")
    #expect(keptName("../../etc/passwd", taken: []) == "passwd")
    #expect(keptName(".hidden", taken: []) == "hidden")
    #expect(keptName("a:b\u{0}.txt", taken: []) == "a-b.txt")
    #expect(keptName(nil, taken: []) == "download")
}

@Test func aFileDoesNotReplaceOneAlreadyKept() {
    #expect(keptName("report.pdf", taken: ["report.pdf"]) == "report 2.pdf")
    #expect(keptName("report.pdf", taken: ["report.pdf", "report 2.pdf"]) == "report 3.pdf")
    #expect(keptName("notes", taken: ["notes"]) == "notes 2")
}

@Test func theSameFileAskedForAgainIsNotFetchedTwice() {
    let kept = [digest: "report.pdf"]
    #expect(alreadyAsked(digest, downloading: [digest], kept: [:], present: []))
    #expect(alreadyAsked(digest, downloading: [], kept: kept, present: ["report.pdf"]))
    #expect(!alreadyAsked(digest, downloading: [], kept: kept, present: []))  // deleted since
    #expect(!alreadyAsked(digest, downloading: [], kept: [:], present: ["report.pdf"]))
}

@Test func aDroppedDownloadIsResumedLessOftenEachTimeAndThenGivenUp() {
    #expect(resumeDelay(attempt: 0) == .seconds(1))
    #expect(resumeDelay(attempt: 3) == .seconds(8))
    #expect(resumeDelay(attempt: 7) == .seconds(60))
    #expect(resumeDelay(attempt: 8) == nil)
}
