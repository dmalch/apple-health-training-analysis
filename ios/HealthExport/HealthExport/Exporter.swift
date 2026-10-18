import CoreLocation
import Foundation
import HealthKit
import WorkoutKit

/// Reads HealthKit incrementally and writes one batch of JSONL files.
///
/// Every sample type gets its own HKAnchoredObjectQuery, and the anchors are
/// saved only after a batch has been written in full, so a run that dies half
/// way leaves the previous anchors in place and the next run re-reads what it
/// lost. The loader on the Mac de-duplicates by UUID, so a re-read costs time,
/// never correctness.
///
/// Layout under Documents/:
///   state.json                  the anchors, and the start date of the first pull
///   batches/<id>/*.jsonl        one finished batch per run
///   batches/<id>/manifest.json  written last; a batch without it is incomplete
final class Exporter {
    struct State: Codable {
        var since: Date
        var anchors: [String: Data] = [:]
    }

    private let store = HKHealthStore()
    private let documents: URL
    private let log: (String) -> Void
    private let pageSize = 20_000

    init(documents: URL, log: @escaping (String) -> Void) {
        self.documents = documents
        self.log = log
    }

    var stateURL: URL { documents.appendingPathComponent("state.json") }

    func loadState() -> State? {
        guard let data = try? Data(contentsOf: stateURL) else { return nil }
        return try? JSONDecoder().decode(State.self, from: data)
    }

    func resetState() throws {
        if FileManager.default.fileExists(atPath: stateURL.path) {
            try FileManager.default.removeItem(at: stateURL)
        }
    }

    func authorize() async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw ExportError.healthUnavailable }
        try await store.requestAuthorization(toShare: [], read: HealthTypes.readTypes)
    }

    /// Runs one export and returns the batch id.
    func run(firstPullDays: Int) async throws -> String {
        let started = Date()
        let since = Calendar.current.date(byAdding: .day, value: -firstPullDays, to: started)!
        let previous = loadState()
        var state = previous ?? State(since: Calendar.current.startOfDay(for: since))
        let batchID = Self.batchFormatter.string(from: started)
        let batches = documents.appendingPathComponent("batches")
        let partial = documents.appendingPathComponent("partial-\(batchID)")
        try FileManager.default.createDirectory(at: partial, withIntermediateDirectories: true)

        let out = try Batch(dir: partial)
        let predicate = HKQuery.predicateForSamples(withStart: state.since, end: nil, options: [])
        log("since \(state.since), \(state.anchors.isEmpty ? "first pull" : "incremental")")

        for (name, type, unit) in HealthTypes.quantities {
            let n = try await pull(type, key: name, predicate: predicate, state: &state, out: out) { samples in
                try await self.writeQuantities(samples, name: name, type: type, unit: unit, out: out)
            }
            if n > 0 { log("\(name): \(n)") }
        }
        for (name, type) in HealthTypes.categories {
            let n = try await pull(type, key: name, predicate: predicate, state: &state, out: out) { samples in
                for sample in samples {
                    var row = Self.base(sample, type: name)
                    if let category = sample as? HKCategorySample { row["value_int"] = category.value }
                    try out.samples.write(row)
                }
            }
            if n > 0 { log("\(name): \(n)") }
        }
        let beats = try await pull(
            HKSeriesType.heartbeat(), key: "HeartbeatSeries", predicate: predicate, state: &state, out: out
        ) { samples in
            for sample in samples {
                guard let series = sample as? HKHeartbeatSeriesSample else { continue }
                try out.samples.write(Self.base(series, type: "HeartbeatSeries"))
                try await self.writeHeartbeats(series, out: out)
            }
        }
        log("HeartbeatSeries: \(beats)")
        let workouts = try await pull(
            HKObjectType.workoutType(), key: "Workout", predicate: predicate, state: &state, out: out
        ) { samples in
            for sample in samples {
                guard let workout = sample as? HKWorkout else { continue }
                try out.workouts.write(await self.workoutRow(workout))
                try await self.writeRoutes(of: workout, out: out)
            }
        }
        log("Workout: \(workouts)")
        try await writeActivitySummaries(since: state.since, out: out)

        let manifest: [String: Any] = [
            "batch": batchID,
            "since": JSONValue.time(state.since),
            "incremental": previous != nil,
            "started": JSONValue.time(started),
            "finished": JSONValue.time(Date()),
            "time_zone": TimeZone.current.identifier,
            "system_version": ProcessInfo.processInfo.operatingSystemVersionString,
            "app_version": Bundle.main.infoDictionary?["CFBundleShortVersionString"] ?? "",
            "rows": out.counts,
        ]
        try out.close(manifest: manifest)
        try FileManager.default.createDirectory(at: batches, withIntermediateDirectories: true)
        try FileManager.default.moveItem(at: partial, to: batches.appendingPathComponent(batchID))
        try JSONEncoder().encode(state).write(to: stateURL, options: .atomic)
        log("rows: \(out.counts.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: " "))")
        return batchID
    }

    // MARK: - anchored pulls

    /// Pages through one type from its saved anchor, handing each page to
    /// `handle` and recording deletions. Returns how many samples arrived.
    private func pull(
        _ type: HKSampleType, key: String, predicate: NSPredicate, state: inout State, out: Batch,
        handle: ([HKSample]) async throws -> Void
    ) async throws -> Int {
        var anchor = state.anchors[key].flatMap {
            try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: $0)
        }
        var total = 0
        while true {
            let (added, deleted, next) = try await page(type, predicate: predicate, anchor: anchor)
            try await handle(added)
            for object in deleted {
                try out.deleted.write(["uuid": object.uuid.uuidString, "type": key])
            }
            total += added.count
            anchor = next ?? anchor
            if added.count + deleted.count < pageSize { break }
        }
        if let anchor {
            state.anchors[key] = try NSKeyedArchiver.archivedData(withRootObject: anchor, requiringSecureCoding: true)
        }
        return total
    }

    private func page(_ type: HKSampleType, predicate: NSPredicate, anchor: HKQueryAnchor?) async throws
        -> ([HKSample], [HKDeletedObject], HKQueryAnchor?)
    {
        try await withCheckedThrowingContinuation { continuation in
            let query = HKAnchoredObjectQuery(type: type, predicate: predicate, anchor: anchor, limit: pageSize) {
                _, added, deleted, next, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: (added ?? [], deleted ?? [], next))
                }
            }
            store.execute(query)
        }
    }

    // MARK: - samples

    private func writeQuantities(
        _ samples: [HKSample], name: String, type: HKQuantityType, unit: HKUnit, out: Batch
    ) async throws {
        var series: [UUID] = []
        for sample in samples {
            guard let sample = sample as? HKQuantitySample else { continue }
            var row = Self.base(sample, type: name)
            row["value"] = JSONValue.number(sample.quantity.doubleValue(for: unit))
            row["unit"] = unit.unitString
            row["count"] = sample.count
            try out.samples.write(row)
            if sample.count > 1 { series.append(sample.uuid) }
        }
        // A series sample's own value is none of its readings; the readings come
        // from a series query. Heart rate only, as on the backup route: energy,
        // distance and steps hold a sum, so one row per series keeps totals exact.
        guard name == "HeartRate", !series.isEmpty else { return }
        for start in stride(from: 0, to: series.count, by: 500) {
            let chunk = Set(series[start..<min(start + 500, series.count)])
            try await writeSeriesPoints(type, uuids: chunk, unit: unit, out: out)
        }
    }

    private func writeSeriesPoints(_ type: HKQuantityType, uuids: Set<UUID>, unit: HKUnit, out: Batch) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            var finished = false
            let query = HKQuantitySeriesSampleQuery(
                quantityType: type, predicate: HKQuery.predicateForObjects(with: uuids)
            ) { _, quantity, interval, sample, done, error in
                guard !finished else { return }
                do {
                    if let error { throw error }
                    if let quantity, let interval, let sample {
                        try out.seriesPoints.write([
                            "uuid": sample.uuid.uuidString,
                            "t": JSONValue.time(interval.start),
                            "dur": interval.duration,
                            "value": JSONValue.number(quantity.doubleValue(for: unit)),
                        ])
                    }
                    if done {
                        finished = true
                        continuation.resume()
                    }
                } catch {
                    finished = true
                    continuation.resume(throwing: error)
                }
            }
            query.includeSample = true
            query.orderByQuantitySampleStartDate = true
            store.execute(query)
        }
    }

    private func writeHeartbeats(_ series: HKHeartbeatSeriesSample, out: Batch) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            var finished = false
            let start = series.startDate
            let query = HKHeartbeatSeriesQuery(heartbeatSeries: series) { _, sinceStart, gap, done, error in
                guard !finished else { return }
                do {
                    if let error { throw error }
                    // An empty series still calls back once, with done set.
                    if series.count > 0 {
                        try out.heartbeats.write([
                            "uuid": series.uuid.uuidString,
                            "t": JSONValue.time(start.addingTimeInterval(sinceStart)),
                            "gap": gap,
                        ])
                    }
                    if done {
                        finished = true
                        continuation.resume()
                    }
                } catch {
                    finished = true
                    continuation.resume(throwing: error)
                }
            }
            store.execute(query)
        }
    }

    // MARK: - workouts

    private func workoutRow(_ workout: HKWorkout) async -> [String: Any] {
        var row = Self.base(workout, type: "Workout")
        row["activity_type"] = Int(workout.workoutActivityType.rawValue)
        row["duration_s"] = workout.duration
        if let distance = workout.totalDistance {
            row["distance_km"] = JSONValue.number(distance.doubleValue(for: .meterUnit(with: .kilo)))
        }
        if let energy = workout.statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity() {
            row["energy_kcal"] = JSONValue.number(energy.doubleValue(for: .kilocalorie()))
        }
        row["statistics"] = Self.statistics(workout.allStatistics)
        row["events"] = (workout.workoutEvents ?? []).map(Self.event)
        row["activities"] = workout.workoutActivities.enumerated().map { index, activity in
            var a: [String: Any] = [
                "uuid": activity.uuid.uuidString,
                "index": index,
                "activity_type": Int(activity.workoutConfiguration.activityType.rawValue),
                "location_type": activity.workoutConfiguration.locationType.rawValue,
                "start": JSONValue.time(activity.startDate),
                "duration_s": activity.duration,
                "events": activity.workoutEvents.map(Self.event),
                "statistics": Self.statistics(activity.allStatistics),
            ]
            if let end = activity.endDate { a["end"] = JSONValue.time(end) }
            if let metadata = JSONValue.metadata(activity.metadata) { a["metadata"] = metadata }
            return a
        }
        do {
            if let plan = try await workout.workoutPlan { row["plan"] = PlanEncoder.encode(plan) }
        } catch {
            row["plan_error"] = String(describing: error)
        }
        return row
    }

    private func writeRoutes(of workout: HKWorkout, out: Batch) async throws {
        let routes: [HKSample] = try await withCheckedThrowingContinuation { continuation in
            let query = HKSampleQuery(
                sampleType: HKSeriesType.workoutRoute(), predicate: HKQuery.predicateForObjects(from: workout),
                limit: HKObjectQueryNoLimit, sortDescriptors: nil
            ) { _, samples, error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume(returning: samples ?? []) }
            }
            store.execute(query)
        }
        for case let route as HKWorkoutRoute in routes {
            var row = Self.base(route, type: "WorkoutRoute")
            row["workout"] = workout.uuid.uuidString
            row["count"] = route.count
            try out.routes.write(row)
            try await writeRoutePoints(route, out: out)
        }
    }

    private func writeRoutePoints(_ route: HKWorkoutRoute, out: Batch) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            var finished = false
            let query = HKWorkoutRouteQuery(route: route) { _, locations, done, error in
                guard !finished else { return }
                do {
                    if let error { throw error }
                    for l in locations ?? [] {
                        try out.routePoints.write([
                            "route": route.uuid.uuidString,
                            "t": JSONValue.time(l.timestamp),
                            "lat": l.coordinate.latitude, "lon": l.coordinate.longitude,
                            "ele": JSONValue.number(l.altitude),
                            "speed": JSONValue.number(l.speed), "course": JSONValue.number(l.course),
                            "hacc": JSONValue.number(l.horizontalAccuracy),
                            "vacc": JSONValue.number(l.verticalAccuracy),
                        ])
                    }
                    if done {
                        finished = true
                        continuation.resume()
                    }
                } catch {
                    finished = true
                    continuation.resume(throwing: error)
                }
            }
            store.execute(query)
        }
    }

    /// Daily rings. Not anchored: one row a day, so every run re-reads them all
    /// and the loader keeps the newest copy of each day.
    private func writeActivitySummaries(since: Date, out: Batch) async throws {
        let calendar = Calendar.current
        var from = calendar.dateComponents([.era, .year, .month, .day], from: since)
        var to = calendar.dateComponents([.era, .year, .month, .day], from: Date())
        from.calendar = calendar
        to.calendar = calendar
        let summaries: [HKActivitySummary] = try await withCheckedThrowingContinuation { continuation in
            let query = HKActivitySummaryQuery(
                predicate: HKQuery.predicate(forActivitySummariesBetweenStart: from, end: to)
            ) { _, summaries, error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume(returning: summaries ?? []) }
            }
            store.execute(query)
        }
        for s in summaries {
            let day = s.dateComponents(for: calendar)
            guard let y = day.year, let m = day.month, let d = day.day else { continue }
            try out.activitySummaries.write([
                "date": String(format: "%04d-%02d-%02d", y, m, d),
                "active_energy": s.activeEnergyBurned.doubleValue(for: .kilocalorie()),
                "active_energy_goal": s.activeEnergyBurnedGoal.doubleValue(for: .kilocalorie()),
                "exercise_min": s.appleExerciseTime.doubleValue(for: .minute()),
                "exercise_goal": s.exerciseTimeGoal?.doubleValue(for: .minute()) ?? NSNull(),
                "stand_hours": s.appleStandHours.doubleValue(for: .count()),
                "stand_goal": s.standHoursGoal?.doubleValue(for: .count()) ?? NSNull(),
            ])
        }
        log("ActivitySummary: \(summaries.count)")
    }

    // MARK: - rows

    /// The columns every sample shares: identity, time, and where it came from.
    private static func base(_ sample: HKSample, type: String) -> [String: Any] {
        let revision = sample.sourceRevision
        var row: [String: Any] = [
            "uuid": sample.uuid.uuidString,
            "type": type,
            "start": JSONValue.time(sample.startDate),
            "end": JSONValue.time(sample.endDate),
            "source": revision.source.name,
            "bundle": revision.source.bundleIdentifier,
        ]
        if let version = revision.version { row["source_version"] = version }
        if let product = revision.productType { row["product"] = product }
        if let device = sample.device {
            if let name = device.name { row["device"] = name }
            if let model = device.model { row["model"] = model }
            if let hardware = device.hardwareVersion { row["hardware"] = hardware }
            if let manufacturer = device.manufacturer { row["manufacturer"] = manufacturer }
        }
        if let metadata = JSONValue.metadata(sample.metadata) { row["metadata"] = metadata }
        return row
    }

    private static func statistics(_ all: [HKQuantityType: HKStatistics]) -> [[String: Any]] {
        all.compactMap { type, stats in
            guard let unit = HealthTypes.unit(of: type) else { return nil }
            var row: [String: Any] = [
                "type": HealthTypes.name(of: type), "unit": unit.unitString,
                "start": JSONValue.time(stats.startDate), "end": JSONValue.time(stats.endDate),
            ]
            if let q = stats.averageQuantity() { row["avg"] = JSONValue.number(q.doubleValue(for: unit)) }
            if let q = stats.minimumQuantity() { row["min"] = JSONValue.number(q.doubleValue(for: unit)) }
            if let q = stats.maximumQuantity() { row["max"] = JSONValue.number(q.doubleValue(for: unit)) }
            if let q = stats.sumQuantity() { row["sum"] = JSONValue.number(q.doubleValue(for: unit)) }
            return row
        }
        .sorted { ($0["type"] as? String ?? "") < ($1["type"] as? String ?? "") }
    }

    private static func event(_ event: HKWorkoutEvent) -> [String: Any] {
        var row: [String: Any] = [
            "type": event.type.rawValue,
            "start": JSONValue.time(event.dateInterval.start),
            "duration_s": event.dateInterval.duration,
        ]
        if let metadata = JSONValue.metadata(event.metadata) { row["metadata"] = metadata }
        return row
    }

    private static let batchFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
        return f
    }()
}

enum ExportError: LocalizedError {
    case healthUnavailable

    var errorDescription: String? {
        switch self {
        case .healthUnavailable: "Health data is not available on this device."
        }
    }
}

/// The files of one batch. Unchecked Sendable because HealthKit calls each
/// query's handler on its own queue, and the exporter runs one query at a time.
final class Batch: @unchecked Sendable {
    let samples, seriesPoints, heartbeats, workouts, routes, routePoints, activitySummaries, deleted: JSONLWriter
    private let dir: URL

    init(dir: URL) throws {
        self.dir = dir
        func file(_ name: String) throws -> JSONLWriter {
            try JSONLWriter(url: dir.appendingPathComponent(name + ".jsonl"))
        }
        samples = try file("samples")
        seriesPoints = try file("series_points")
        heartbeats = try file("heartbeats")
        workouts = try file("workouts")
        routes = try file("routes")
        routePoints = try file("route_points")
        activitySummaries = try file("activity_summary")
        deleted = try file("deleted")
    }

    private var all: [(String, JSONLWriter)] {
        [
            ("samples", samples), ("series_points", seriesPoints), ("heartbeats", heartbeats),
            ("workouts", workouts), ("routes", routes), ("route_points", routePoints),
            ("activity_summary", activitySummaries), ("deleted", deleted),
        ]
    }

    var counts: [String: Int] { Dictionary(uniqueKeysWithValues: all.map { ($0.0, $0.1.count) }) }

    func close(manifest: [String: Any]) throws {
        for (_, writer) in all { try writer.close() }
        let data = try JSONSerialization.data(withJSONObject: manifest, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: dir.appendingPathComponent("manifest.json"))
    }
}
