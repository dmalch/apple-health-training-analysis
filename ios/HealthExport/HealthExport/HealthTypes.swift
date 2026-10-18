import HealthKit

/// What the app asks to read, and the unit each quantity is written in.
///
/// Names are the HealthKit identifiers without their `HKQuantityTypeIdentifier` /
/// `HKCategoryTypeIdentifier` prefix, which is how `records.type` spells them in
/// the DuckDB database. The lists are every non-deprecated identifier in the
/// iOS 27.1 SDK headers; one the running iOS does not know is skipped, not fatal.
enum HealthTypes {
    static let quantityNames = [
        "ActiveEnergyBurned", "AppleExerciseTime", "AppleMoveTime", "AppleSleepingBreathingDisturbances",
        "AppleSleepingWristTemperature", "AppleStandTime", "AppleWalkingSteadiness",
        "AtrialFibrillationBurden", "BasalBodyTemperature", "BasalEnergyBurned", "BloodAlcoholContent",
        "BloodGlucose", "BloodPressureDiastolic", "BloodPressureSystolic", "BodyFatPercentage",
        "BodyMass", "BodyMassIndex", "BodyTemperature", "CrossCountrySkiingSpeed", "CyclingCadence",
        "CyclingFunctionalThresholdPower", "CyclingPower", "CyclingSpeed", "DietaryBiotin",
        "DietaryCaffeine", "DietaryCalcium", "DietaryCarbohydrates", "DietaryChloride",
        "DietaryCholesterol", "DietaryChromium", "DietaryCopper", "DietaryEnergyConsumed",
        "DietaryFatMonounsaturated", "DietaryFatPolyunsaturated", "DietaryFatSaturated",
        "DietaryFatTotal", "DietaryFiber", "DietaryFolate", "DietaryIodine", "DietaryIron",
        "DietaryMagnesium", "DietaryManganese", "DietaryMolybdenum", "DietaryNiacin",
        "DietaryPantothenicAcid", "DietaryPhosphorus", "DietaryPotassium", "DietaryProtein",
        "DietaryRiboflavin", "DietarySelenium", "DietarySodium", "DietarySugar", "DietaryThiamin",
        "DietaryVitaminA", "DietaryVitaminB12", "DietaryVitaminB6", "DietaryVitaminC",
        "DietaryVitaminD", "DietaryVitaminE", "DietaryVitaminK", "DietaryWater", "DietaryZinc",
        "DistanceCrossCountrySkiing", "DistanceCycling", "DistanceDownhillSnowSports",
        "DistancePaddleSports", "DistanceRowing", "DistanceSkatingSports", "DistanceSwimming",
        "DistanceWalkingRunning", "DistanceWheelchair", "ElectrodermalActivity",
        "EnvironmentalAudioExposure", "EnvironmentalSoundReduction", "EstimatedWorkoutEffortScore",
        "FlightsClimbed", "ForcedExpiratoryVolume1", "ForcedVitalCapacity", "HeadphoneAudioExposure",
        "HeartRate", "HeartRateRecoveryOneMinute", "HeartRateVariabilityRMSSD",
        "HeartRateVariabilitySDNN", "Height", "InhalerUsage", "InsulinDelivery", "LeanBodyMass",
        "NumberOfAlcoholicBeverages", "NumberOfTimesFallen", "OxygenSaturation",
        "PaddleSportsSpeed", "PeakExpiratoryFlowRate", "PeripheralPerfusionIndex", "PhysicalEffort",
        "PushCount", "RespiratoryRate", "RestingHeartRate", "RowingSpeed", "RunningGroundContactTime",
        "RunningPower", "RunningSpeed", "RunningStrideLength", "RunningVerticalOscillation",
        "SixMinuteWalkTestDistance", "StairAscentSpeed", "StairDescentSpeed", "StepCount",
        "SwimmingStrokeCount", "TimeInDaylight", "UnderwaterDepth", "UVExposure", "VO2Max",
        "WaistCircumference", "WalkingAsymmetryPercentage", "WalkingDoubleSupportPercentage",
        "WalkingHeartRateAverage", "WalkingSpeed", "WalkingStepLength", "WaterTemperature",
        "WorkoutEffortScore",
    ]

    static let categoryNames = [
        "AbdominalCramps", "Acne", "AppetiteChanges", "AppleStandHour", "AppleWalkingSteadinessEvent",
        "BladderIncontinence", "BleedingAfterMenopause", "BleedingAfterPregnancy",
        "BleedingDuringPregnancy", "Bloating", "BreastPain", "CervicalMucusQuality",
        "ChestTightnessOrPain", "Chills", "Constipation", "Contraceptive", "Coughing", "Diarrhea",
        "Dizziness", "DrySkin", "EnvironmentalAudioExposureEvent", "Fainting", "Fatigue", "Fever",
        "GeneralizedBodyAche", "HairLoss", "HandwashingEvent", "HeadphoneAudioExposureEvent",
        "Headache", "Heartburn", "HighHeartRateEvent", "HotFlashes", "HypertensionEvent",
        "InfrequentMenstrualCycles", "IntermenstrualBleeding", "IrregularHeartRhythmEvent",
        "IrregularMenstrualCycles", "Lactation", "LossOfSmell", "LossOfTaste", "LowCardioFitnessEvent",
        "LowHeartRateEvent", "LowerBackPain", "MemoryLapse", "MenopausalState", "MenstrualFlow",
        "MindfulSession", "MoodChanges", "Nausea", "NightSweats", "OvulationTestResult", "PelvicPain",
        "PersistentIntermenstrualBleeding", "Pregnancy", "PregnancyTestResult",
        "ProgesteroneTestResult", "ProlongedMenstrualPeriods", "RapidPoundingOrFlutteringHeartbeat",
        "RunnyNose", "SexualActivity", "ShortnessOfBreath", "SinusCongestion", "SkippedHeartbeat",
        "SleepAnalysis", "SleepApneaEvent", "SleepChanges", "SoreThroat", "ToothbrushingEvent",
        "VaginalDryness", "Vomiting", "Wheezing",
    ]

    /// Quantity types this iOS knows, each with the unit its values are written in.
    static let quantities: [(name: String, type: HKQuantityType, unit: HKUnit)] = quantityNames.compactMap { name in
        let id = HKQuantityTypeIdentifier(rawValue: "HKQuantityTypeIdentifier" + name)
        guard let type = HKObjectType.quantityType(forIdentifier: id), let unit = unit(for: name, type) else {
            return nil
        }
        return (name, type, unit)
    }

    static let categories: [(name: String, type: HKCategoryType)] = categoryNames.compactMap { name in
        let id = HKCategoryTypeIdentifier(rawValue: "HKCategoryTypeIdentifier" + name)
        return HKObjectType.categoryType(forIdentifier: id).map { (name, $0) }
    }

    static var readTypes: Set<HKObjectType> {
        var types = Set<HKObjectType>(quantities.map(\.type))
        types.formUnion(categories.map(\.type))
        types.insert(HKObjectType.workoutType())
        types.insert(HKSeriesType.workoutRoute())
        types.insert(HKSeriesType.heartbeat())
        types.insert(HKObjectType.activitySummaryType())
        return types
    }

    /// The short name for a quantity type, as `records.type` spells it.
    static func name(of type: HKQuantityType) -> String {
        type.identifier.replacingOccurrences(of: "HKQuantityTypeIdentifier", with: "")
    }

    static func unit(of type: HKQuantityType) -> HKUnit? {
        quantities.first { $0.type == type }?.unit ?? unit(for: name(of: type), type)
    }

    // The units the database already uses. They were read off the backup-built
    // database (its `unit` column, and per-type means where the column is empty):
    // display units, not HealthKit's canonical ones, so heart rate is per minute
    // and walking speed km/h. Distance is km for every distance type, swimming
    // included, because that is what the backup route stores.
    private static let bpm = HKUnit.count().unitDivided(by: .minute())
    private static let kmh = HKUnit.meterUnit(with: .kilo).unitDivided(by: .hour())
    private static let ms = HKUnit.secondUnit(with: .milli)
    private static let km = HKUnit.meterUnit(with: .kilo)
    private static let cm = HKUnit.meterUnit(with: .centi)
    private static let mps = HKUnit.meter().unitDivided(by: .second())
    private static let vo2 = HKUnit.literUnit(with: .milli)
        .unitDivided(by: HKUnit.gramUnit(with: .kilo).unitMultiplied(by: .minute()))
    private static let met = HKUnit.kilocalorie()
        .unitDivided(by: HKUnit.gramUnit(with: .kilo).unitMultiplied(by: .hour()))
    private static let mgdl = HKUnit.gramUnit(with: .milli).unitDivided(by: .literUnit(with: .deci))

    private static let preferred: [String: HKUnit] = [
        "HeartRate": bpm, "RestingHeartRate": bpm, "WalkingHeartRateAverage": bpm,
        "HeartRateRecoveryOneMinute": bpm, "RespiratoryRate": bpm, "CyclingCadence": bpm,
        "HeartRateVariabilitySDNN": ms, "HeartRateVariabilityRMSSD": ms, "RunningGroundContactTime": ms,
        "WalkingSpeed": kmh, "RunningSpeed": kmh,
        "WalkingStepLength": cm, "Height": cm, "RunningVerticalOscillation": cm, "WaistCircumference": cm,
        "RunningStrideLength": .meter(), "SixMinuteWalkTestDistance": .meter(), "UnderwaterDepth": .meter(),
        "StairAscentSpeed": mps, "StairDescentSpeed": mps, "CyclingSpeed": mps,
        "CrossCountrySkiingSpeed": mps, "PaddleSportsSpeed": mps, "RowingSpeed": mps,
        "VO2Max": vo2, "PhysicalEffort": met, "BloodGlucose": mgdl,
        "AppleExerciseTime": .minute(), "AppleStandTime": .minute(), "AppleMoveTime": .minute(),
        "TimeInDaylight": .minute(),
        "DietaryEnergyConsumed": .kilocalorie(), "DietaryWater": .literUnit(with: .milli),
    ]

    /// Tried in order when a type has no preferred unit; the first one the type
    /// accepts wins. Order matters only where a type would accept two of them.
    private static let fallbacks: [HKUnit] = [
        .count(), .kilocalorie(), km, .gramUnit(with: .kilo), .percent(), .degreeCelsius(),
        .decibelAWeightedSoundPressureLevel(), .watt(), bpm, ms, mps, .minute(),
        .millimeterOfMercury(), .liter(), HKUnit.liter().unitDivided(by: .minute()),
        .siemenUnit(with: .micro), .internationalUnit(), .appleEffortScore(), .gram(), mgdl,
    ]

    private static func unit(for name: String, _ type: HKQuantityType) -> HKUnit? {
        if let unit = preferred[name], type.is(compatibleWith: unit) { return unit }
        return fallbacks.first { type.is(compatibleWith: $0) }
    }
}
