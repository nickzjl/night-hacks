import Foundation
import QuartzCore

/// WebSocket to the Mac backend. Sends camera frames (binary JPEG) and telemetry (JSON text),
/// receives Braille state to show on screen. Reconnects on its own; haptics never depend on it.
final class BackendLink: NSObject, URLSessionWebSocketDelegate {
    enum State: String {
        case disconnected, connecting, connected
    }

    static let defaultPort = 8765

    var onState: ((State) -> Void)?
    var onMessage: (([String: Any]) -> Void)?
    var onError: ((String) -> Void)?

    private let queue = DispatchQueue(label: "nh.link")
    private var session: URLSession!
    private var task: URLSessionWebSocketTask?
    private var host = ""
    private var wantConnected = false
    private var state: State = .disconnected
    private var sendingFrame = false
    private var frameSentAt: TimeInterval = 0
    private var lastHeard: TimeInterval = 0
    private var watchdog: DispatchSourceTimer?

    /// The Mac pings every second; this much silence means the link is dead (cable pulled, Wi-Fi drop)
    /// even though the socket hasn't reported an error yet.
    static let silenceLimit: TimeInterval = 4
    static let sendLimit: TimeInterval = 3

    override init() {
        super.init()
        let delegateQueue = OperationQueue()
        delegateQueue.maxConcurrentOperationCount = 1
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 5  // fail fast on a dead network instead of hanging ~60 s
        config.waitsForConnectivity = false
        session = URLSession(configuration: config, delegate: self, delegateQueue: delegateQueue)

        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { [weak self] in self?.checkAlive() }
        timer.resume()
        watchdog = timer
    }

    private func checkAlive() {
        guard state == .connected, let task else { return }
        let now = CACurrentMediaTime()
        let silent = now - lastHeard > Self.silenceLimit
        let stuck = sendingFrame && now - frameSentAt > Self.sendLimit
        if silent || stuck {
            onError?("Lost the Mac, reconnecting…")
            fail(task)
        }
    }

    /// True when a new frame would go out right away; callers skip encoding otherwise.
    var readyForFrame: Bool {
        queue.sync { state == .connected && !sendingFrame }
    }

    var isConnected: Bool {
        queue.sync { state == .connected }
    }

    func connect(to host: String) {
        queue.async {
            self.host = host.trimmingCharacters(in: .whitespacesAndNewlines)
            self.wantConnected = true
            self.open()
        }
    }

    func sendFrame(_ jpeg: Data) {
        queue.async {
            guard self.state == .connected, let task = self.task, !self.sendingFrame else { return }
            self.sendingFrame = true
            self.frameSentAt = CACurrentMediaTime()
            task.send(.data(jpeg)) { [weak self] error in
                self?.queue.async {
                    self?.sendingFrame = false
                    if error != nil { self?.fail(task) }
                }
            }
        }
    }

    func sendJSON(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8) else { return }
        queue.async {
            guard self.state == .connected, let task = self.task else { return }
            task.send(.string(text)) { _ in }
        }
    }

    // MARK: - Connection lifecycle (all on `queue`)

    private func open() {
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        sendingFrame = false
        guard let url = makeURL() else {
            setState(.disconnected)
            return
        }
        setState(.connecting)
        let task = session.webSocketTask(with: url)
        task.maximumMessageSize = 4 << 20
        self.task = task
        task.resume()
        receive(on: task)
    }

    private func makeURL() -> URL? {
        guard !host.isEmpty else { return nil }
        if host.contains("://") { return URL(string: host) }
        let hostPort = host.contains(":") ? host : "\(host):\(Self.defaultPort)"
        return URL(string: "ws://\(hostPort)/ws/phone")
    }

    private func receive(on task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            if case .success = result {
                self.queue.async { self.lastHeard = CACurrentMediaTime() }
            }
            switch result {
            case .success(.string(let text)):
                if let data = text.data(using: .utf8),
                   let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.onMessage?(object)
                }
                self.receive(on: task)
            case .success:
                self.receive(on: task)
            case .failure(let error):
                self.onError?(error.localizedDescription)
                self.fail(task)
            }
        }
    }

    private func fail(_ failed: URLSessionWebSocketTask) {
        queue.async {
            guard failed === self.task else { return }
            self.task = nil
            self.sendingFrame = false
            self.setState(.disconnected)
            guard self.wantConnected else { return }
            self.queue.asyncAfter(deadline: .now() + 1.5) {
                if self.wantConnected && self.task == nil { self.open() }
            }
        }
    }

    private func setState(_ new: State) {
        guard new != state else { return }
        state = new
        onState?(new)
    }

    // MARK: - URLSessionWebSocketDelegate

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        queue.async {
            guard webSocketTask === self.task else { return }
            self.lastHeard = CACurrentMediaTime()
            self.setState(.connected)
            self.onError?("")
            self.sendJSON(["type": "hello", "app": "NightHacks iOS"])
        }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        fail(webSocketTask)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let webSocketTask = task as? URLSessionWebSocketTask else { return }
        if let error { onError?(error.localizedDescription) }
        fail(webSocketTask)
    }
}
