import ARKit
import CoreLocation
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

    // Lexis companion mode. Sharing is off at every launch; the wearer turns it on.
    @Published private(set) var sharing = false
    @Published private(set) var journeyActive = false
    @Published var destinationName = ""
    @Published private(set) var destination: CLLocation?
    @Published var intervalMinutes = 2 {
        didSet { if journeyActive { sendCompanion() } }
    }
    @Published private(set) var companionStatus = ""
    @Published private(set) var locationText = ""

    private let session = ARSession()
    private let haptics = Haptics()
    private let link = BackendLink()
    private let ble = BLELink()
    private let processor: FrameProcessor
    private let tracker = LocationTracker()
    private var started = false
    private var journeyId = ""
    private var journeyStartedAt = Date()
    private var lastLocationSent: (at: Date, fix: CLLocation)?
    private var companionTimer: Timer?

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
        tracker.onFix = { [weak self] fix, address in
            self?.sendLocation(fix, address: address)
        }
        tracker.onAuthorizationDenied = { [weak self] in
            self?.companionStatus = "Location permission is off. Allow it in Settings to share."
        }
    }

    func start() {
        guard !started else { return }
        started = true
        UIApplication.shared.isIdleTimerDisabled = true
        runAR()
        link.connect(to: host)
        // Heartbeat so the Mac always knows the current sharing/journey state, even after restarts.
        companionTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.sendCompanion() }
        }
    }

    // MARK: - Lexis companion

    func setSharing(_ on: Bool) {
        sharing = on
        if on {
            tracker.start()
            companionStatus = "Sharing with your trusted contacts."
        } else {
            journeyActive = false
            tracker.stop()
            lastLocationSent = nil
            locationText = ""
            companionStatus = "Sharing is off. Nothing is sent to your contacts."
        }
        sendCompanion()
    }

    func stopSharingNow() {
        setSharing(false)
        haptics.testPulse()
    }

    func useCurrentSpotAsDestination() {
        guard let fix = tracker.latest else {
            companionStatus = sharing ? "Waiting for GPS…" : "Turn on sharing first to use your current spot."
            return
        }
        destination = fix
        if destinationName.trimmingCharacters(in: .whitespaces).isEmpty { destinationName = "the marked spot" }
        companionStatus = "Destination set to your current spot."
    }

    func startJourney() {
        if !sharing { setSharing(true) }
        let query = destinationName.trimmingCharacters(in: .whitespacesAndNewlines)
        if destination == nil && !query.isEmpty {
            companionStatus = "Finding \(query)…"
            tracker.geocode(query) { [weak self] location, name in
                Task { @MainActor in
                    guard let self else { return }
                    self.destination = location
                    if location == nil { self.companionStatus = "Couldn't find \(query); starting without arrival alerts." }
                    self.beginJourney()
                }
            }
            return
        }
        beginJourney()
    }

    private func beginJourney() {
        journeyId = UUID().uuidString
        journeyStartedAt = Date()
        journeyActive = true
        companionStatus = "Journey started. Your contacts get updates every \(intervalMinutes) min."
        sendCompanion()
    }

    func endJourney() {
        journeyActive = false
        destination = nil
        companionStatus = "Journey ended."
        sendCompanion()
    }

    func destinationEdited() {
        destination = nil  // a new name means a new lookup
    }

    private func sendCompanion() {
        var payload: [String: Any] = ["type": "companion", "sharing": sharing]
        if sharing && journeyActive {
            var journey: [String: Any] = [
                "active": true,
                "id": journeyId,
                "interval_s": intervalMinutes * 60,
                "started_at": journeyStartedAt.timeIntervalSince1970,
            ]
            let name = destinationName.trimmingCharacters(in: .whitespaces)
            if let destination {
                journey["destination"] = ["name": name.isEmpty ? "the destination" : name,
                                          "lat": destination.coordinate.latitude, "lon": destination.coordinate.longitude]
            } else if !name.isEmpty {
                journey["destination"] = ["name": name]
            }
            payload["journey"] = journey
        } else {
            payload["journey"] = NSNull()
        }
        sendControl(payload)
    }

    private func sendLocation(_ fix: CLLocation, address: String) {
        guard sharing else { return }
        locationText = address.isEmpty ? String(format: "GPS ±%.0f m", fix.horizontalAccuracy) : "Near \(address)"
        if let last = lastLocationSent, Date().timeIntervalSince(last.at) < 10, fix.distance(from: last.fix) < 15 { return }
        lastLocationSent = (Date(), fix)
        sendControl([
            "type": "location",
            "lat": fix.coordinate.latitude,
            "lon": fix.coordinate.longitude,
            "acc": fix.horizontalAccuracy,
            "ts": fix.timestamp.timeIntervalSince1970,
            "address": address,
        ])
    }

    /// Wi-Fi/USB when connected, otherwise Bluetooth (as a message that is never dropped).
    private func sendControl(_ object: [String: Any]) {
        if link.isConnected {
            link.sendJSON(object)
        } else {
            ble.sendJSON(object)
        }
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
        case "companion_stop":
            setSharing(false)
            companionStatus = "Sharing was stopped from the Mac."
        case "journey_arrived":
            journeyActive = false
            destination = nil
            companionStatus = "You've arrived. Your contacts were told."
            sendCompanion()
        default:
            break
        }
    }
}
