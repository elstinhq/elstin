// Draws the placeholder app icon: `swift make-icon.swift out.png [size]`.
// Generated at build time (packaging/build-app.sh turns it into an .icns) so no binary
// asset lives in the repository; replace this script's output with a designed icon when
// there is one — the build only needs a 1024x1024 PNG at the path it names.
import AppKit

let arguments = CommandLine.arguments
guard arguments.count >= 2 else { FileHandle.standardError.write(Data("usage: make-icon.swift out.png [size]\n".utf8)); exit(2) }
let size = arguments.count > 2 ? Double(arguments[2]) ?? 1024 : 1024

let image = NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
    // macOS app icons sit inside a margin, as a rounded square.
    let inset = size * 0.098
    let body = rect.insetBy(dx: inset, dy: inset)
    let shape = NSBezierPath(roundedRect: body, xRadius: body.width * 0.2237, yRadius: body.width * 0.2237)

    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.35)
    shadow.shadowBlurRadius = size * 0.02
    shadow.shadowOffset = NSSize(width: 0, height: -size * 0.01)
    shadow.set()
    NSColor.black.setFill()
    shape.fill()
    NSGraphicsContext.restoreGraphicsState()

    NSGradient(colors: [NSColor(red: 0.20, green: 0.24, blue: 0.62, alpha: 1), NSColor(red: 0.07, green: 0.09, blue: 0.27, alpha: 1)])?
        .draw(in: shape, angle: -90)

    let font = NSFont.systemFont(ofSize: body.height * 0.62, weight: .heavy)
    let text = NSAttributedString(string: "E", attributes: [.font: font, .foregroundColor: NSColor.white])
    let box = text.size()
    text.draw(at: NSPoint(x: body.midX - box.width / 2, y: body.midY - box.height / 2 - body.height * 0.01))
    return true
}

guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
      let png = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
try png.write(to: URL(fileURLWithPath: arguments[1]))
