import ARKit
import SwiftUI

@MainActor
final class Engine: ObservableObject {
    @Published var host: String
    @Published var linkState: BackendLink.State = .disconnected
    @Published var snapshot = Snapshot()
    @Published var brailleWord = ""
    @Published var brailleIndex = -1
    @Published var brailleMask = 0
    @Published var macStatus = ""
    @Published var linkError = ""
    @Published var bleConnected = false
    @Published var arError: String?
    @Published var hapticsEnabled = true {
        didSet { haptics.setEnabled(hapticsEnabled) }
    }

    private let session = ARSession()
    private let haptics = Haptics()
    private let link = BackendLink()
    private let ble = BLELink()
    private let processor: FrameProcessor
    private var started = false

    init() {
        // A host typed in the app wins; otherwise use what the build baked in (the Mac's .local name).
        let saved = UserDefaults.standard.string(forKey: "macHost") ?? ""
        let baked = (Bundle.main.object(forInfoDictionaryKey: "NHDefaultHost") as? String) ?? ""
        host = !saved.isEmpty ? saved : (!baked.isEmpty ? baked : "Nicks-MacBook-Air-632.local")

        processor = FrameProcessor(haptics: haptics, link: link, ble: ble)
        session.delegate = processor
        session.delegateQueue = processor.queue

        processor.onSnapshot = { [weak self] snap in
            DispatchQueue.main.async { self?.snapshot = snap }
        }
        processor.onError = { [weak self] message in
            DispatchQueue.main.async { self?.arError = message }
        }
        link.onState = { [weak self] state in
            DispatchQueue.main.async { self?.linkState = state }
        }
        link.onMessage = { [weak self] message in
            DispatchQueue.main.async { self?.handle(message) }
        }
        link.onError = { [weak self] error in
            DispatchQueue.main.async { self?.linkError = error }
        }
        ble.onState = { [weak self] connected in
            DispatchQueue.main.async { self?.bleConnected = connected }
        }
        ble.onMessage = { [weak self] message in
            DispatchQueue.main.async { self?.handle(message) }
        }
    }

    func start() {
        guard !started else { return }
        started = true
        UIApplication.shared.isIdleTimerDisabled = true
        runAR()
        link.connect(to: host)
    }

    func connect(to newHost: String) {
        host = newHost.trimmingCharacters(in: .whitespacesAndNewlines)
        UserDefaults.standard.set(host, forKey: "macHost")
        link.connect(to: host)
    }

    func testVibration() {
        haptics.testPulse()
    }

    func scenePhaseChanged(_ phase: ScenePhase) {
        guard started else { return }
        switch phase {
        case .active:
            UIApplication.shared.isIdleTimerDisabled = true
            runAR()
        case .background:
            session.pause()
            processor.resetProximity()
        default:
            break
        }
    }

    private func runAR() {
        guard ARWorldTrackingConfiguration.isSupported else {
            arError = "ARKit world tracking is not supported on this device."
            return
        }
        arError = nil
        let config = ARWorldTrackingConfiguration()
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.smoothedSceneDepth) {
            config.frameSemantics.insert(.smoothedSceneDepth)
        } else if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
            config.frameSemantics.insert(.sceneDepth)
        } else {
            arError = "No LiDAR depth on this device; proximity is disabled."
        }
        session.run(config, options: [.resetTracking])
    }

    private func handle(_ message: [String: Any]) {
        switch message["type"] as? String {
        case "braille":
            brailleWord = message["word"] as? String ?? ""
            brailleIndex = message["index"] as? Int ?? -1
            brailleMask = message["mask"] as? Int ?? 0
        case "idle":
            brailleIndex = -1
            brailleMask = 0
        case "status":
            macStatus = message["text"] as? String ?? ""
        default:
            break
        }
    }
}
