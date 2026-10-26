import Foundation
import HealthKit

/// What the main screen shows, read back from the batches this app has already
/// written: nothing here queries HealthKit, so the screen draws the same data the
/// Mac copies off, and it works before Health access is granted.
struct Dashboard {
    struct Batch: Identifiable {
        let id: String
        let started: Date
        let finished: Date
        let incremental: Bool
        let rows: [String: Int]

        var workouts: Int { rows["workouts"] ?? 0 }
        /// Everything read, without the deletion records.
        var totalRows: Int { rows.filter { $0.key != "deleted" }.values.reduce(0, +) }
    }

    struct Workout: Identifiable {
        let id: String
        let type: HKWorkoutActivityType
        let start: Date
        let duration: TimeInterval
        let distanceKm: Double?
        let energyKcal: Double?
        let avgHR: Double?
        let maxHR: Double?
    }

    /// One day of Activity rings, each as a fraction of its goal (above 1 when closed twice).
    struct Rings: Identifiable {
        let date: Date
        let move: Double
        let exercise: Double
        let stand: Double
        var id: Date { date }
    }

    var batches: [Batch] = []    // newest first
    var workouts: [Workout] = [] // newest first
    var week: [Rings] = []       // the last seven days, oldest first
    var signedUntil: Date?

    static func load(documents: URL) -> Dashboard {
        let root = documents.appendingPathComponent("batches")
        let dirs = ((try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? [])
            .filter { FileManager.default.fileExists(atPath: $0.appendingPathComponent("manifest.json").path) }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }

        var dashboard = Dashboard(signedUntil: provisioningExpiry())
        var workouts: [String: Workout] = [:]
        var deleted = Set<String>()
        for dir in dirs {
            if let batch = readManifest(dir) { dashboard.batches.insert(batch, at: 0) }
            for row in jsonLines(dir.appendingPathComponent("workouts.jsonl")) {
                if let workout = Workout(row) { workouts[workout.id] = workout }
            }
            for row in jsonLines(dir.appendingPathComponent("deleted.jsonl")) where row["type"] as? String == "Workout" {
                if let uuid = row["uuid"] as? String { deleted.insert(uuid) }
            }
        }
        dashboard.workouts = workouts.values.filter { !deleted.contains($0.id) }.sorted { $0.start > $1.start }

        // The rings are re-read in full on every run, so the newest batch that has them has them all.
        for dir in dirs.reversed() {
            let rows = jsonLines(dir.appendingPathComponent("activity_summary.jsonl"))
            if !rows.isEmpty {
                dashboard.week = lastWeek(rows)
                break
            }
        }
        if dashboard.week.isEmpty { dashboard.week = lastWeek([]) }
        return dashboard
    }

    private static func readManifest(_ dir: URL) -> Batch? {
        guard let data = try? Data(contentsOf: dir.appendingPathComponent("manifest.json")),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let started = json["started"] as? Double, let finished = json["finished"] as? Double
        else { return nil }
        return Batch(
            id: dir.lastPathComponent,
            started: Date(timeIntervalSince1970: started),
            finished: Date(timeIntervalSince1970: finished),
            incremental: json["incremental"] as? Bool ?? false,
            rows: json["rows"] as? [String: Int] ?? [:]
        )
    }

    private static func lastWeek(_ rows: [[String: Any]]) -> [Rings] {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        var byDay: [String: [String: Any]] = [:]
        for row in rows { if let day = row["date"] as? String { byDay[day] = row } }

        func fraction(_ row: [String: Any]?, _ value: String, _ goal: String) -> Double {
            guard let row, let v = row[value] as? Double, let g = row[goal] as? Double, g > 0 else { return 0 }
            return v / g
        }
        let today = Calendar.current.startOfDay(for: Date())
        return (0..<7).reversed().map { back in
            let date = Calendar.current.date(byAdding: .day, value: -back, to: today)!
            let row = byDay[formatter.string(from: date)]
            return Rings(
                date: date,
                move: fraction(row, "active_energy", "active_energy_goal"),
                exercise: fraction(row, "exercise_min", "exercise_goal"),
                stand: fraction(row, "stand_hours", "stand_goal")
            )
        }
    }

    /// The expiry of the profile this build was signed with. A free team's lasts
    /// seven days, and after it the app no longer launches, so the countdown is
    /// the one thing worth seeing before the Mac's sync fails on it.
    private static func provisioningExpiry() -> Date? {
        guard let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
              let data = try? Data(contentsOf: url),
              let start = data.range(of: Data("<?xml".utf8)),
              let end = data.range(of: Data("</plist>".utf8), in: start.lowerBound..<data.endIndex),
              let plist = try? PropertyListSerialization.propertyList(
                  from: data[start.lowerBound..<end.upperBound], format: nil) as? [String: Any]
        else { return nil }
        return plist["ExpirationDate"] as? Date
    }

    private static func jsonLines(_ url: URL) -> [[String: Any]] {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap {
            try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any]
        }
    }
}

extension Dashboard.Workout {
    init?(_ row: [String: Any]) {
        guard let uuid = row["uuid"] as? String, let raw = row["activity_type"] as? Int,
              let start = row["start"] as? Double, let duration = row["duration_s"] as? Double
        else { return nil }
        let heart = (row["statistics"] as? [[String: Any]])?.first { $0["type"] as? String == "HeartRate" }
        self.init(
            id: uuid,
            type: HKWorkoutActivityType(rawValue: UInt(raw)) ?? .other,
            start: Date(timeIntervalSince1970: start),
            duration: duration,
            distanceKm: (row["distance_km"] as? Double).flatMap { $0 > 0 ? $0 : nil },
            energyKcal: row["energy_kcal"] as? Double,
            avgHR: heart?["avg"] as? Double,
            maxHR: heart?["max"] as? Double
        )
    }
}

/// A name and a symbol for the activities that turn up in practice. Apple's
/// names are kept as they are, even where a watch uses one loosely.
extension HKWorkoutActivityType {
    var displayName: String {
        switch self {
        case .running: "Run"
        case .walking: "Walk"
        case .hiking: "Hike"
        case .cycling: "Ride"
        case .swimming: "Swim"
        case .rowing: "Row"
        case .elliptical: "Elliptical"
        case .traditionalStrengthTraining: "Strength"
        case .functionalStrengthTraining: "Functional strength"
        case .crossTraining: "Cross training"
        case .highIntensityIntervalTraining: "HIIT"
        case .coreTraining: "Core"
        case .yoga: "Yoga"
        case .pilates: "Pilates"
        case .flexibility: "Flexibility"
        case .cooldown: "Cooldown"
        case .mixedCardio: "Mixed cardio"
        case .stairClimbing: "Stairs"
        case .downhillSkiing: "Skiing"
        case .crossCountrySkiing: "Cross-country ski"
        default: "Workout"
        }
    }

    var symbol: String {
        switch self {
        case .running: "figure.run"
        case .walking: "figure.walk"
        case .hiking: "figure.hiking"
        case .cycling: "figure.outdoor.cycle"
        case .swimming: "figure.pool.swim"
        case .rowing: "figure.rower"
        case .elliptical: "figure.elliptical"
        case .traditionalStrengthTraining: "figure.strengthtraining.traditional"
        case .functionalStrengthTraining: "figure.strengthtraining.functional"
        case .crossTraining: "figure.cross.training"
        case .highIntensityIntervalTraining: "figure.highintensity.intervaltraining"
        case .coreTraining: "figure.core.training"
        case .yoga: "figure.yoga"
        case .pilates: "figure.pilates"
        case .flexibility: "figure.flexibility"
        case .cooldown: "figure.cooldown"
        case .mixedCardio: "figure.mixed.cardio"
        case .stairClimbing: "figure.stair.stepper"
        case .downhillSkiing: "figure.skiing.downhill"
        case .crossCountrySkiing: "figure.skiing.crosscountry"
        default: "figure.mixed.cardio"
        }
    }

    /// Endurance green, strength orange, everything else purple.
    enum Family { case endurance, strength, other }

    var family: Family {
        switch self {
        case .running, .walking, .hiking, .cycling, .swimming, .rowing, .elliptical, .stairClimbing,
             .downhillSkiing, .crossCountrySkiing:
            .endurance
        case .traditionalStrengthTraining, .functionalStrengthTraining, .crossTraining,
             .highIntensityIntervalTraining, .coreTraining:
            .strength
        default:
            .other
        }
    }
}
