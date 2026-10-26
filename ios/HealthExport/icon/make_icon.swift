import AppKit

// swift icon/make_icon.swift HealthExport/Assets.xcassets/AppIcon.appiconset/AppIcon.png
//
// Renders the 1024 px app icon: a pink-to-orange gradient, a white heart with an
// upward arrow.
let size = 1024
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let rect = NSRect(x: 0, y: 0, width: size, height: size)

let gradient = NSGradient(colors: [
    NSColor(red: 1.00, green: 0.55, blue: 0.20, alpha: 1),
    NSColor(red: 1.00, green: 0.24, blue: 0.42, alpha: 1),
    NSColor(red: 0.80, green: 0.10, blue: 0.45, alpha: 1),
])!
gradient.draw(in: rect, angle: 65)

// Soft glow in the upper left, fading out well inside the canvas.
let glow = NSGradient(colors: [NSColor(white: 1, alpha: 0.30), NSColor(white: 1, alpha: 0)])!
glow.draw(fromCenter: NSPoint(x: 280, y: 800), radius: 0, toCenter: NSPoint(x: 280, y: 800), radius: 620,
          options: [])

// The heart symbol, tinted white.
let config = NSImage.SymbolConfiguration(pointSize: 600, weight: .semibold)
let symbol = NSImage(systemSymbolName: "arrow.up.heart.fill", accessibilityDescription: nil)!
    .withSymbolConfiguration(config)!
let white = NSImage(size: symbol.size, flipped: false) { r in
    symbol.draw(in: r)
    NSColor.white.set()
    r.fill(using: .sourceAtop)
    return true
}
let shadow = NSShadow()
shadow.shadowColor = NSColor(red: 0.5, green: 0, blue: 0.2, alpha: 0.35)
shadow.shadowBlurRadius = 40
shadow.shadowOffset = NSSize(width: 0, height: -18)
shadow.set()
let s = white.size
white.draw(in: NSRect(x: (Double(size) - s.width) / 2, y: (Double(size) - s.height) / 2 - 10,
                      width: s.width, height: s.height))

NSGraphicsContext.restoreGraphicsState()
let out = CommandLine.arguments[1]
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
