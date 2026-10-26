import SwiftUI

struct ContentView: View {
    @ObservedObject var model: ExportModel
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    StatusCard(dashboard: model.dashboard)
                    ExportButton(model: model)
                    WeekCard(week: model.dashboard.week)
                    WorkoutsCard(workouts: Array(model.dashboard.workouts.prefix(8)))
                    LinksCard(model: model)
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 24)
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Health Export")
            .toolbar {
                Button { showSettings = true } label: { Image(systemName: "gearshape") }
                    .disabled(model.running)
            }
            .sheet(isPresented: $showSettings) { SettingsSheet(model: model) }
            .refreshable { await model.reload() }
        }
        .tint(.pink)
    }
}

// MARK: - cards

private struct Card<Content: View>: View {
    var title: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let title {
                Text(title).font(.headline)
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }
}

private struct StatusCard: View {
    let dashboard: Dashboard

    var body: some View {
        Card {
            HStack(spacing: 18) {
                SignatureGauge(until: dashboard.signedUntil)
                VStack(alignment: .leading, spacing: 4) {
                    if let last = dashboard.batches.first {
                        Text("Last export").font(.subheadline).foregroundStyle(.secondary)
                        Text(last.finished.formatted(.relative(presentation: .named)).capitalizedFirst)
                            .font(.title2.weight(.semibold))
                        Text("\(last.workouts) workouts · \(last.totalRows.formatted()) rows")
                            .font(.subheadline)
                    } else {
                        Text("No export yet").font(.title2.weight(.semibold))
                        Text("The first one reads the last few weeks.").font(.subheadline).foregroundStyle(.secondary)
                    }
                    if let until = dashboard.signedUntil {
                        Label(
                            until > .now ? "Signed until \(until.formatted(.dateTime.day().month()))" : "Signature expired — rebuild",
                            systemImage: until > .now ? "checkmark.seal" : "exclamationmark.triangle.fill"
                        )
                        .font(.caption)
                        .foregroundStyle(until > .now ? Color.secondary : Color.red)
                    }
                }
                Spacer(minLength: 0)
            }
        }
    }
}

/// Days left on the seven-day free signing profile, as a ring that drains.
private struct SignatureGauge: View {
    let until: Date?

    private var daysLeft: Double {
        guard let until else { return 0 }
        return max(0, until.timeIntervalSinceNow / 86_400)
    }

    private var color: Color {
        if until == nil { return .gray }
        return switch daysLeft {
        case 3...: .green
        case 1..<3: .orange
        default: .red
        }
    }

    var body: some View {
        ZStack {
            Circle().stroke(color.opacity(0.18), lineWidth: 9)
            Circle()
                .trim(from: 0, to: min(1, daysLeft / 7))
                .stroke(color.gradient, style: StrokeStyle(lineWidth: 9, lineCap: .round))
                .rotationEffect(.degrees(-90))
            if until == nil {
                // No embedded profile: a simulator build, which needs no signature.
                Image(systemName: "seal").font(.title2).foregroundStyle(.secondary)
            } else {
                VStack(spacing: -2) {
                    Text("\(Int(daysLeft.rounded(.down)))")
                        .font(.system(.title, design: .rounded, weight: .bold))
                        .contentTransition(.numericText())
                    Text("days").font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
        .frame(width: 78, height: 78)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(until == nil ? "Signature unknown" : "\(Int(daysLeft)) days of signature left")
    }
}

private struct ExportButton: View {
    @ObservedObject var model: ExportModel

    /// The exporter logs "Type: count" as each type finishes, which is the only progress there is.
    private var step: String {
        model.lines.last ?? "Asking Health…"
    }

    var body: some View {
        Button {
            Task { await model.export() }
        } label: {
            HStack(spacing: 14) {
                Image(systemName: model.running ? "heart.text.square.fill" : "arrow.up.heart.fill")
                    .font(.system(size: 30, weight: .semibold))
                    .symbolEffect(.pulse, isActive: model.running)
                    .contentTransition(.symbolEffect(.replace))
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.running ? "Exporting…" : "Export now")
                        .font(.title3.weight(.bold))
                    Text(model.running ? step : "Reads only what is new since the last run")
                        .font(.caption)
                        .lineLimit(1)
                        .opacity(0.85)
                }
                Spacer(minLength: 0)
                if model.running {
                    ProgressView().tint(.white)
                } else {
                    Image(systemName: "chevron.right").font(.headline).opacity(0.7)
                }
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 20)
            .padding(.vertical, 18)
            .background(
                LinearGradient(colors: [Color(red: 1, green: 0.27, blue: 0.42), Color(red: 1, green: 0.5, blue: 0.2)],
                               startPoint: .topLeading, endPoint: .bottomTrailing),
                in: RoundedRectangle(cornerRadius: 22, style: .continuous)
            )
            .shadow(color: .pink.opacity(0.35), radius: 14, y: 6)
        }
        .buttonStyle(PressableStyle())
        .disabled(model.running)
        .animation(.snappy, value: model.running)
        .sensoryFeedback(trigger: model.lastResult) { _, result in
            result == true ? .success : .error
        }
    }
}

private struct PressableStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.spring(duration: 0.25), value: configuration.isPressed)
    }
}

private struct WeekCard: View {
    let week: [Dashboard.Rings]

    var body: some View {
        Card(title: "This week") {
            HStack {
                ForEach(week) { day in
                    VStack(spacing: 6) {
                        ActivityRings(rings: day).frame(width: 38, height: 38)
                        Text(day.date.formatted(.dateTime.weekday(.narrow)))
                            .font(.caption2.weight(Calendar.current.isDateInToday(day.date) ? .bold : .regular))
                            .foregroundStyle(Calendar.current.isDateInToday(day.date) ? .primary : .secondary)
                    }
                    .frame(maxWidth: .infinity)
                }
            }
        }
    }
}

private struct ActivityRings: View {
    let rings: Dashboard.Rings

    var body: some View {
        GeometryReader { geo in
            let width = geo.size.width * 0.13
            ZStack {
                ring(rings.move, color: Color(red: 0.98, green: 0.07, blue: 0.31), inset: 0, width: width)
                ring(rings.exercise, color: Color(red: 0.6, green: 0.98, blue: 0.0), inset: width * 1.15, width: width)
                ring(rings.stand, color: Color(red: 0.0, green: 0.93, blue: 0.95), inset: width * 2.3, width: width)
            }
        }
    }

    private func ring(_ fraction: Double, color: Color, inset: CGFloat, width: CGFloat) -> some View {
        ZStack {
            Circle().stroke(color.opacity(0.2), lineWidth: width)
            Circle()
                .trim(from: 0, to: min(1, fraction))
                .stroke(color, style: StrokeStyle(lineWidth: width, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
        .padding(inset + width / 2)
    }
}

private struct WorkoutsCard: View {
    let workouts: [Dashboard.Workout]

    var body: some View {
        Card(title: "Recent workouts") {
            if workouts.isEmpty {
                Text("Workouts show up here after an export.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            ForEach(workouts) { workout in
                WorkoutRow(workout: workout)
                if workout.id != workouts.last?.id { Divider().padding(.leading, 52) }
            }
        }
    }
}

private struct WorkoutRow: View {
    let workout: Dashboard.Workout

    private var color: Color {
        switch workout.type.family {
        case .endurance: .green
        case .strength: .orange
        case .other: .purple
        }
    }

    private var amount: String {
        // A gym class picks up distance from arm swing; only endurance work reads in km.
        if workout.type.family == .endurance, let km = workout.distanceKm {
            return km.formatted(.number.precision(.fractionLength(1))) + " km"
        }
        return "\(Int((workout.duration / 60).rounded())) min"
    }

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: workout.type.symbol)
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(color)
                .frame(width: 40, height: 40)
                .background(color.opacity(0.15), in: Circle())
            VStack(alignment: .leading, spacing: 2) {
                Text(workout.type.displayName).font(.subheadline.weight(.semibold))
                Text(workout.start.formatted(.dateTime.weekday(.abbreviated).day().month().hour().minute()))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 2) {
                Text(amount)
                    .font(.system(.subheadline, design: .rounded, weight: .semibold))
                    .foregroundStyle(color)
                if let avg = workout.avgHR {
                    HStack(spacing: 3) {
                        Image(systemName: "heart.fill").foregroundStyle(.red)
                        Text("\(Int(avg.rounded()))")
                        if let max = workout.maxHR { Text("/ \(Int(max))").foregroundStyle(.secondary) }
                    }
                    .font(.system(.caption, design: .rounded))
                }
            }
        }
    }
}

private struct LinksCard: View {
    @ObservedObject var model: ExportModel

    var body: some View {
        VStack(spacing: 0) {
            NavigationLink { BatchesView(batches: model.dashboard.batches) } label: {
                LinkRow(symbol: "shippingbox.fill", color: .blue, title: "Batches",
                        detail: "\(model.dashboard.batches.count)")
            }
            Divider().padding(.leading, 52)
            NavigationLink { LogView(model: model) } label: {
                LinkRow(symbol: "terminal.fill", color: .gray, title: "Log",
                        detail: model.lines.isEmpty ? "" : "\(model.lines.count) lines")
            }
        }
        .buttonStyle(.plain)
        .padding(.vertical, 4)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }
}

private struct LinkRow: View {
    let symbol: String
    let color: Color
    let title: String
    let detail: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 30, height: 30)
                .background(color.gradient, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            Text(title).foregroundStyle(.primary)
            Spacer()
            Text(detail).foregroundStyle(.secondary)
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .contentShape(Rectangle())
    }
}

// MARK: - secondary screens

private struct BatchesView: View {
    let batches: [Dashboard.Batch]

    var body: some View {
        List(batches) { batch in
            NavigationLink { BatchDetail(batch: batch) } label: {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(batch.started.formatted(date: .abbreviated, time: .shortened)).font(.headline)
                        Spacer()
                        Text(batch.incremental ? "Incremental" : "First pull")
                            .font(.caption2.weight(.semibold))
                            .padding(.horizontal, 8).padding(.vertical, 3)
                            .foregroundStyle(batch.incremental ? .blue : .pink)
                            .background((batch.incremental ? Color.blue : Color.pink).opacity(0.12), in: Capsule())
                    }
                    Text("\(batch.workouts) workouts · \(batch.totalRows.formatted()) rows · \(seconds(batch)) s")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle("Batches")
        .overlay { if batches.isEmpty { ContentUnavailableView("No batches yet", systemImage: "shippingbox") } }
    }

    private func seconds(_ batch: Dashboard.Batch) -> String {
        batch.finished.timeIntervalSince(batch.started).formatted(.number.precision(.fractionLength(0)))
    }
}

private struct BatchDetail: View {
    let batch: Dashboard.Batch

    var body: some View {
        List {
            Section {
                LabeledContent("Batch", value: batch.id).font(.callout.monospaced())
                LabeledContent("Kind", value: batch.incremental ? "Incremental" : "First pull")
            }
            Section("Rows") {
                ForEach(batch.rows.sorted { $0.value > $1.value }, id: \.key) { row in
                    LabeledContent(row.key, value: row.value.formatted())
                }
            }
        }
        .navigationTitle(batch.started.formatted(date: .abbreviated, time: .shortened))
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct LogView: View {
    @ObservedObject var model: ExportModel

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(model.lines.enumerated()), id: \.offset) { index, line in
                        Text(line)
                            .font(.caption.monospaced())
                            .foregroundStyle(color(line))
                            .textSelection(.enabled)
                            .id(index)
                    }
                    if model.lines.isEmpty {
                        Text("Nothing logged since the app started.")
                            .font(.caption.monospaced()).foregroundStyle(.gray)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
            }
            .background(Color(white: 0.08))
            .onChange(of: model.lines.count) { _, count in
                withAnimation { proxy.scrollTo(count - 1, anchor: .bottom) }
            }
        }
        .navigationTitle("Log")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Color(white: 0.08), for: .navigationBar)
        .toolbarColorScheme(.dark, for: .navigationBar)
        .toolbar {
            Button { UIPasteboard.general.string = model.lines.joined(separator: "\n") } label: {
                Image(systemName: "doc.on.doc")
            }
            .disabled(model.lines.isEmpty)
        }
    }

    private func color(_ line: String) -> Color {
        if line.hasPrefix("EXPORT DONE") { return .green }
        if line.hasPrefix("EXPORT FAILED") { return .red }
        return Color(white: 0.85)
    }
}

private struct SettingsSheet: View {
    @ObservedObject var model: ExportModel
    @Environment(\.dismiss) private var dismiss
    @State private var confirmReset = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text(model.stateSummary).font(.callout)
                    Stepper("First pull: \(model.firstPullDays) days", value: $model.firstPullDays, in: 1...5000, step: 30)
                } footer: {
                    Text("Batches land in this app's Documents folder. Keep the phone unlocked while it runs: Health is unreadable while the phone is locked.")
                }
                Section {
                    Button("Reset anchors", role: .destructive) { confirmReset = true }
                } footer: {
                    Text("The next export then reads the whole first-pull window again. Batches already written are kept.")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done") { dismiss() } }
            .confirmationDialog("Reset anchors?", isPresented: $confirmReset, titleVisibility: .visible) {
                Button("Reset", role: .destructive) { model.resetAnchors() }
            }
        }
        .presentationDetents([.medium, .large])
    }
}

private extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}
