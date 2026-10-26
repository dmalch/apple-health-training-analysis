import SwiftUI

@main
struct HealthExportApp: App {
    @StateObject private var model = ExportModel()

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .task {
                    await model.reload()
                    await model.runIfLaunchedToExport()
                }
        }
    }
}

@MainActor
final class ExportModel: ObservableObject {
    @Published private(set) var lines: [String] = []
    @Published private(set) var running = false
    @Published private(set) var lastResult: Bool?
    @Published private(set) var dashboard = Dashboard()
    @Published var firstPullDays = 30

    private lazy var exporter = Exporter(documents: Self.documents) { line in
        Task { @MainActor in self.append(line) }
    }

    static var documents: URL { FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0] }

    var stateSummary: String {
        guard let state = exporter.loadState() else { return "No export yet: the first one reads \(firstPullDays) days." }
        return "Incremental since \(state.since.formatted(date: .abbreviated, time: .omitted)), \(state.anchors.count) anchors."
    }

    func reload() async {
        let documents = Self.documents
        dashboard = await Task.detached { Dashboard.load(documents: documents) }.value
    }

    /// `-autoExport YES` on the launch command line, as the Mac's sync script
    /// passes it through `devicectl device process launch`: export, then quit,
    /// so the script sees the process end. `-days N` sets the first pull, and
    /// `-resetAnchors YES` makes this run a first pull again.
    func runIfLaunchedToExport() async {
        let defaults = UserDefaults.standard
        guard defaults.bool(forKey: "autoExport") else { return }
        let days = defaults.integer(forKey: "days")
        if days > 0 { firstPullDays = days }
        if defaults.bool(forKey: "resetAnchors") { resetAnchors() }
        let ok = await export()
        // Give the console a moment to drain before the process goes away.
        try? await Task.sleep(for: .milliseconds(300))
        exit(ok ? 0 : 1)
    }

    @discardableResult
    func export() async -> Bool {
        guard !running else { return false }
        running = true
        defer { running = false }
        let ok: Bool
        do {
            try await exporter.authorize()
            let batch = try await exporter.run(firstPullDays: firstPullDays)
            append("EXPORT DONE \(batch)")
            ok = true
        } catch {
            append("EXPORT FAILED: \(error.localizedDescription)")
            ok = false
        }
        lastResult = ok
        await reload()
        return ok
    }

    func resetAnchors() {
        do {
            try exporter.resetState()
            append("Anchors cleared: the next export reads \(firstPullDays) days again.")
        } catch {
            append("Could not clear the anchors: \(error.localizedDescription)")
        }
        objectWillChange.send()
    }

    private func append(_ line: String) {
        lines.append(line)
        // devicectl --console shows stdout; unbuffered so progress arrives live.
        print(line)
        fflush(stdout)
    }
}
