import SwiftUI

struct ContentView: View {
    @ObservedObject var model: ExportModel

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(model.stateSummary).font(.callout)
                    Stepper("First pull: \(model.firstPullDays) days", value: $model.firstPullDays, in: 1...5000, step: 30)
                    Button(model.running ? "Exporting…" : "Export") {
                        Task { await model.export() }
                    }
                    .disabled(model.running)
                    Button("Reset anchors", role: .destructive) { model.resetAnchors() }
                        .disabled(model.running)
                } footer: {
                    Text("Batches land in this app's Documents folder. Keep the phone unlocked while it runs: Health is unreadable while the phone is locked.")
                }
                Section("Log") {
                    ForEach(Array(model.lines.enumerated()), id: \.offset) { _, line in
                        Text(line).font(.caption.monospaced())
                    }
                }
            }
            .navigationTitle("Health Export")
        }
    }
}
