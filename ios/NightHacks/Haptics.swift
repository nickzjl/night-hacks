import CoreHaptics
import QuartzCore
import UIKit

/// Proximity-only vibration: a parking-sensor style tap train that speeds up as things get closer.
/// Runs entirely on the phone so it keeps working when the network doesn't.
final class Haptics {
    struct Band {
        let interval: TimeInterval
        let intensity: Float
        let sharpness: Float
    }

    static let bands: [Band] = [
        Band(interval: 0, intensity: 0, sharpness: 0),        // 0: clear
        Band(interval: 0.70, intensity: 0.45, sharpness: 0.5), // 1: < 2.0 m
        Band(interval: 0.38, intensity: 0.70, sharpness: 0.7), // 2: < 1.3 m
        Band(interval: 0.18, intensity: 0.90, sharpness: 0.9), // 3: < 0.8 m
        Band(interval: 0.08, intensity: 1.00, sharpness: 1.0), // 4: < 0.45 m, near-continuous buzz
    ]

    private let queue = DispatchQueue(label: "nh.haptics", qos: .userInteractive)
    private var engine: CHHapticEngine?
    private var patterns: [CHHapticPattern?] = []
    private var timer: DispatchSourceTimer?
    private var level = 0
    private var enabled = true
    private var needsStart = false
    private var lastTap: TimeInterval = 0
    private let fallback = UIImpactFeedbackGenerator(style: .heavy)

    init() {
        queue.sync { setUpEngine() }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(20))
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        self.timer = timer
    }

    func setLevel(_ newLevel: Int) {
        queue.async { self.level = max(0, min(newLevel, Haptics.bands.count - 1)) }
    }

    func setEnabled(_ on: Bool) {
        queue.async { self.enabled = on }
    }

    func testPulse() {
        queue.async { self.play(level: 3) }
    }

    private func setUpEngine() {
        guard CHHapticEngine.capabilitiesForHardware().supportsHaptics else { return }
        do {
            let engine = try CHHapticEngine()
            engine.playsHapticsOnly = true
            engine.isAutoShutdownEnabled = false
            engine.resetHandler = { [weak self] in
                self?.queue.async { self?.needsStart = true }
            }
            engine.stoppedHandler = { [weak self] _ in
                self?.queue.async { self?.needsStart = true }
            }
            try engine.start()
            self.engine = engine
            patterns = Haptics.bands.map(Haptics.pattern(for:))
        } catch {
            engine = nil
        }
    }

    private static func pattern(for band: Band) -> CHHapticPattern? {
        guard band.interval > 0 else { return nil }
        let params = [
            CHHapticEventParameter(parameterID: .hapticIntensity, value: band.intensity),
            CHHapticEventParameter(parameterID: .hapticSharpness, value: band.sharpness),
        ]
        let event: CHHapticEvent
        if band.interval < 0.1 {
            // Overlapping short continuous events read as one steady buzz.
            event = CHHapticEvent(eventType: .hapticContinuous, parameters: params, relativeTime: 0, duration: band.interval + 0.02)
        } else {
            event = CHHapticEvent(eventType: .hapticTransient, parameters: params, relativeTime: 0)
        }
        return try? CHHapticPattern(events: [event], parameters: [])
    }

    private func tick() {
        guard enabled, level > 0 else { return }
        let now = CACurrentMediaTime()
        if now - lastTap >= Haptics.bands[level].interval {
            lastTap = now
            play(level: level)
        }
    }

    private func play(level: Int) {
        guard let engine, let pattern = patterns[level] else {
            let intensity = CGFloat(Haptics.bands[level].intensity)
            DispatchQueue.main.async { self.fallback.impactOccurred(intensity: intensity) }
            return
        }
        if needsStart {
            try? engine.start()
            needsStart = false
        }
        do {
            try engine.makePlayer(with: pattern).start(atTime: CHHapticTimeImmediate)
        } catch {
            needsStart = true
        }
    }
}
