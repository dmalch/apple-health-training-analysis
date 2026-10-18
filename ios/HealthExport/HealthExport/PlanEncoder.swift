import Foundation
import WorkoutKit

/// The plan a workout was run from, as plain JSON.
///
/// This is the public counterpart of the `_HKPrivateWorkoutConfiguration` the
/// backup route decodes: WorkoutKit's `HKWorkout.workoutPlan`. Blocks keep the
/// backup route's order -- warm-up, the interval blocks, cool-down -- and only
/// steps are listed, so a step's position is "block.step" in both.
enum PlanEncoder {
    static func encode(_ plan: WorkoutPlan) -> [String: Any] {
        switch plan.workout {
        case .custom(let workout):
            var blocks: [[String: Any]] = []
            if let warmup = workout.warmup {
                blocks.append(["role": "warmup", "iterations": 1, "steps": [step(warmup, kind: "warmup")]])
            }
            for block in workout.blocks {
                let steps = block.steps.map { step($0.step, kind: $0.purpose == .work ? "work" : "recovery") }
                blocks.append(["role": "intervals", "iterations": block.iterations, "steps": steps])
            }
            if let cooldown = workout.cooldown {
                blocks.append(["role": "cooldown", "iterations": 1, "steps": [step(cooldown, kind: "cooldown")]])
            }
            return [
                "kind": "custom", "name": workout.displayName ?? NSNull(),
                "activity_type": Int(workout.activity.rawValue), "blocks": blocks,
            ]
        case .goal(let workout):
            return ["kind": "goal", "goal": goal(workout.goal)]
        case .pacer:
            return ["kind": "pacer"]
        case .swimBikeRun:
            return ["kind": "swimBikeRun"]
        @unknown default:
            return ["kind": "unknown"]
        }
    }

    private static func step(_ step: WorkoutStep, kind: String) -> [String: Any] {
        var out: [String: Any] = ["kind": kind]
        out.merge(goal(step.goal)) { a, _ in a }
        if let alert = step.alert { out.merge(target(alert)) { a, _ in a } }
        return out
    }

    /// The goal in the units the backup route reports: s, m or kcal.
    private static func goal(_ goal: WorkoutGoal) -> [String: Any] {
        switch goal {
        case .open:
            return ["goal_unit": "open"]
        case .time(let value, let unit):
            return ["goal_value": Measurement(value: value, unit: unit).converted(to: .seconds).value, "goal_unit": "s"]
        case .distance(let value, let unit):
            return ["goal_value": Measurement(value: value, unit: unit).converted(to: .meters).value, "goal_unit": "m"]
        case .energy(let value, let unit):
            return [
                "goal_value": Measurement(value: value, unit: unit).converted(to: .kilocalories).value,
                "goal_unit": "kcal",
            ]
        case .poolSwimDistanceWithTime(let distance, let time):
            return [
                "goal_value": distance.converted(to: .meters).value, "goal_unit": "m",
                "goal_time_s": time.converted(to: .seconds).value,
            ]
        @unknown default:
            return ["goal_unit": "unknown"]
        }
    }

    /// The step's alert as type + range. Speeds in m/s and heart rate in bpm,
    /// which is what `analyze_intervals.target_pace` expects; the type names
    /// follow the backup route's ("instantaneous_pace") where it has one.
    private static func target(_ alert: any WorkoutAlert) -> [String: Any] {
        func metric(_ m: WorkoutAlertMetric) -> String { m == .average ? "average" : "instantaneous" }
        func bpm(_ f: Measurement<UnitFrequency>) -> Double { f.converted(to: .hertz).value * 60 }
        switch alert {
        case let a as SpeedRangeAlert:
            return [
                "target_type": "\(metric(a.metric))_pace",
                "target_min": a.target.lowerBound.converted(to: .metersPerSecond).value,
                "target_max": a.target.upperBound.converted(to: .metersPerSecond).value,
            ]
        case let a as SpeedThresholdAlert:
            // The backup route names a threshold the same as a range and gives it
            // equal ends; so does this, so the two databases agree.
            let v = a.target.converted(to: .metersPerSecond).value
            return ["target_type": "\(metric(a.metric))_pace", "target_min": v, "target_max": v]
        case let a as HeartRateRangeAlert:
            return [
                "target_type": "heart_rate", "target_min": bpm(a.target.lowerBound),
                "target_max": bpm(a.target.upperBound),
            ]
        case let a as HeartRateZoneAlert:
            return ["target_type": "heart_rate_zone", "target_min": a.zone, "target_max": a.zone]
        case let a as CadenceRangeAlert:
            return [
                "target_type": "cadence", "target_min": bpm(a.target.lowerBound),
                "target_max": bpm(a.target.upperBound),
            ]
        case let a as CadenceThresholdAlert:
            let v = bpm(a.target)
            return ["target_type": "cadence_threshold", "target_min": v, "target_max": v]
        case let a as PowerRangeAlert:
            return [
                "target_type": "\(metric(a.metric))_power",
                "target_min": a.target.lowerBound.converted(to: .watts).value,
                "target_max": a.target.upperBound.converted(to: .watts).value,
            ]
        case let a as PowerThresholdAlert:
            let v = a.target.converted(to: .watts).value
            return ["target_type": "\(metric(a.metric))_power_threshold", "target_min": v, "target_max": v]
        case let a as PowerZoneAlert:
            return ["target_type": "power_zone", "target_min": a.zone, "target_max": a.zone]
        default:
            return ["target_type": String(describing: type(of: alert))]
        }
    }
}
