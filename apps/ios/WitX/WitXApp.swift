import SwiftUI
import UIKit

/// WitX's thin iPhone app: it only takes files the Gateway hands it by a
/// `witx://download` link, keeps them in its own folder (Files › On My iPhone › WitX),
/// and shares them on. It controls nothing and holds no credential.
@main
struct WitXApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            FilesView(downloads: .shared)
                .onOpenURL { Downloads.shared.open($0) }
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    /// The system woke the app because a background download finished.
    func application(
        _ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
        completionHandler: @escaping () -> Void
    ) {
        Downloads.shared.backgroundDone = completionHandler
    }
}

struct FilesView: View {
    let downloads: Downloads
    @Environment(\.scenePhase) private var phase

    var body: some View {
        NavigationStack {
            List {
                if let refused = downloads.refused {
                    Section { Label(refused, systemImage: "hand.raised").foregroundStyle(.red) }
                }
                if !downloads.transfers.isEmpty {
                    Section("Downloading") {
                        ForEach(downloads.transfers.values.sorted { $0.id < $1.id }) { t in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(t.name).lineLimit(1)
                                if t.expected > 0 {
                                    ProgressView(value: Double(t.received), total: Double(t.expected))
                                } else {
                                    ProgressView()
                                }
                                Text(t.note ?? size(t.received, of: t.expected))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            .accessibilityIdentifier("transfer")
                        }
                    }
                }
                Section("Files") {
                    if downloads.files.isEmpty {
                        Text("Files the Gateway sends to this phone appear here and in Files.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(downloads.files) { file in
                        HStack {
                            VStack(alignment: .leading) {
                                Text(file.url.lastPathComponent).lineLimit(2)
                                Text(file.size.formatted(.byteCount(style: .file)))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            ShareLink(item: file.url) { Image(systemName: "square.and.arrow.up") }
                                .accessibilityLabel("Share \(file.url.lastPathComponent)")
                        }
                        .accessibilityIdentifier("file")
                        .swipeActions { Button("Delete", role: .destructive) { downloads.delete(file) } }
                    }
                }
            }
            .navigationTitle("WitX")
            .refreshable { await downloads.reload() }
        }
        .onChange(of: phase) { _, now in
            if now == .active { Task { await downloads.reload() } }
        }
    }

    private func size(_ received: Int64, of expected: Int64) -> String {
        let r = received.formatted(.byteCount(style: .file))
        return expected > 0 ? "\(r) of \(expected.formatted(.byteCount(style: .file)))" : r
    }
}
