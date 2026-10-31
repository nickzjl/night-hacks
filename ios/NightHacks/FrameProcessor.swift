import ARKit
import CoreImage
import ImageIO
import QuartzCore

struct Snapshot {
    var distance: Float?
    var level = 0
    var points = 0
    var orientation = "up"
    var fps = 0.0
    var tracking = "starting"
}

/// ARSession delegate. Every frame: LiDAR depth -> filtered proximity -> haptics (local, ~30 Hz).
/// A few times a second: an upright, downscaled JPEG of the camera image goes to the Mac.
final class FrameProcessor: NSObject, ARSessionDelegate {
    let queue = DispatchQueue(label: "nh.frames", qos: .userInteractive)
    var onSnapshot: ((Snapshot) -> Void)?
    var onError: ((String) -> Void)?

    /// Frames to the Mac (recognition is throttled again on the Mac side). Wi-Fi/USB carries
    /// a live-looking stream; Bluetooth LE only manages ~20-40 KB/s, so it gets smaller, rarer frames.
    struct FrameSpec {
        let interval: TimeInterval
        let longEdge: CGFloat
        let quality: Double
    }
    static let wifiFrames = FrameSpec(interval: 0.3, longEdge: 640, quality: 0.6)
    static let bleFrames = FrameSpec(interval: 1.0, longEdge: 480, quality: 0.45)

    private let haptics: Haptics
    private let link: BackendLink
    private let ble: BLELink
    private let filter = ProximityFilter()
    private let ciContext = CIContext(options: [.cacheIntermediates: false])
    private let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!

    private var lastProcessed: TimeInterval = 0
    private var lastFrameSent: TimeInterval = 0
    private var lastTelemetry: TimeInterval = 0
    private var lastBLETelemetry: TimeInterval = 0
    private var lastSnapshot: TimeInterval = 0
    private var fpsStart: TimeInterval = 0
    private var fpsCount = 0
    private var fps = 0.0

    init(haptics: Haptics, link: BackendLink, ble: BLELink) {
        self.haptics = haptics
        self.link = link
        self.ble = ble
    }

    func resetProximity() {
        queue.async {
            self.filter.reset()
            self.haptics.setLevel(0)
        }
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        // Cap processing at ~30 Hz even when ARKit runs at 60.
        guard frame.timestamp - lastProcessed >= 1.0 / 32 else { return }
        lastProcessed = frame.timestamp
        let now = CACurrentMediaTime()

        fpsCount += 1
        if now - fpsStart >= 1 {
            fps = Double(fpsCount) / (now - fpsStart)
            fpsStart = now
            fpsCount = 0
        }

        let sample = DepthProximity.measure(frame: frame)
        let (level, distance) = filter.update(sample.distance)
        haptics.setLevel(level)

        // Wi-Fi/USB when it's up, otherwise Bluetooth.
        if link.isConnected {
            if now - lastFrameSent >= Self.wifiFrames.interval, link.readyForFrame,
               let jpeg = encodeJPEG(frame.capturedImage, orientation: sample.orientation, spec: Self.wifiFrames) {
                lastFrameSent = now
                link.sendFrame(jpeg)
            }
        } else if now - lastFrameSent >= Self.bleFrames.interval, ble.readyForFrame,
                  let jpeg = encodeJPEG(frame.capturedImage, orientation: sample.orientation, spec: Self.bleFrames) {
            lastFrameSent = now
            ble.sendFrame(jpeg)
        }

        let orientation = Self.name(of: sample.orientation)
        let telemetry: [String: Any] = [
            "type": "telemetry",
            "distance": distance.map { (Double($0) * 100).rounded() / 100 } ?? NSNull(),
            "level": level,
            "points": sample.points,
            "orientation": orientation,
            "fps": Int(fps.rounded()),
        ]
        if now - lastTelemetry >= 0.2 {
            lastTelemetry = now
            link.sendJSON(telemetry)
        }
        if now - lastBLETelemetry >= 0.4 {
            lastBLETelemetry = now
            ble.sendJSON(telemetry, replaceable: true)
        }

        if now - lastSnapshot >= 0.1 {
            lastSnapshot = now
            onSnapshot?(Snapshot(
                distance: distance,
                level: level,
                points: sample.points,
                orientation: orientation,
                fps: fps,
                tracking: Self.name(of: frame.camera.trackingState)
            ))
        }
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        onError?(error.localizedDescription)
    }

    func sessionWasInterrupted(_ session: ARSession) {
        resetProximity()
    }

    private func encodeJPEG(_ pixelBuffer: CVPixelBuffer, orientation: CGImagePropertyOrientation, spec: FrameSpec) -> Data? {
        var image = CIImage(cvPixelBuffer: pixelBuffer).oriented(orientation)
        let scale = spec.longEdge / max(image.extent.width, image.extent.height)
        image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        image = image.transformed(by: CGAffineTransform(translationX: -image.extent.origin.x, y: -image.extent.origin.y))
        let options = [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: spec.quality]
        return ciContext.jpegRepresentation(of: image, colorSpace: colorSpace, options: options)
    }

    private static func name(of orientation: CGImagePropertyOrientation) -> String {
        switch orientation {
        case .up: return "up"
        case .down: return "down"
        case .left: return "left"
        case .right: return "right"
        default: return "other"
        }
    }

    private static func name(of state: ARCamera.TrackingState) -> String {
        switch state {
        case .normal: return "normal"
        case .notAvailable: return "unavailable"
        case .limited(let reason):
            switch reason {
            case .initializing: return "initializing"
            case .excessiveMotion: return "fast motion"
            case .insufficientFeatures: return "low detail"
            case .relocalizing: return "relocalizing"
            @unknown default: return "limited"
            }
        }
    }
}
