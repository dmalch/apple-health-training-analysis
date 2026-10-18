import Foundation
import HealthKit

/// One newline-delimited JSON file, buffered.
final class JSONLWriter {
    private let handle: FileHandle
    private var buffer = Data()
    private(set) var count = 0

    init(url: URL) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        handle = try FileHandle(forWritingTo: url)
    }

    func write(_ row: [String: Any]) throws {
        buffer.append(try JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]))
        buffer.append(0x0A)
        count += 1
        if buffer.count > 1 << 20 { try flush() }
    }

    func flush() throws {
        try handle.write(contentsOf: buffer)
        buffer.removeAll(keepingCapacity: true)
    }

    func close() throws {
        try flush()
        try handle.close()
    }
}

/// Values JSONSerialization accepts, from what HealthKit hands back.
///
/// JSONSerialization raises an Objective-C exception -- a crash, not a throw --
/// on anything else, NaN included, so every value written goes through here.
enum JSONValue {
    static func time(_ date: Date) -> Double { date.timeIntervalSince1970 }

    static func number(_ value: Double) -> Any { value.isFinite ? value : NSNull() }

    /// Metadata as strings and numbers. A quantity becomes "1163 cm", which is
    /// how the backup route writes one too; a date becomes epoch seconds.
    static func metadata(_ metadata: [String: Any]?) -> [String: Any]? {
        guard let metadata, !metadata.isEmpty else { return nil }
        var out: [String: Any] = [:]
        for (key, value) in metadata {
            switch value {
            case let quantity as HKQuantity:
                out[key] = "\(quantity)"
            case let date as Date:
                out[key] = time(date)
            case let string as String:
                out[key] = string
            case let number as NSNumber:
                out[key] = number.doubleValue.isFinite ? number : NSNull()
            case let data as Data:
                out[key] = ["base64": data.base64EncodedString()]
            default:
                out[key] = String(describing: value)
            }
        }
        return out
    }
}
