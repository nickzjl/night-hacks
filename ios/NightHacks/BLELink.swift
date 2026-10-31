import CoreBluetooth
import Foundation

/// Bluetooth LE link to the Mac, for venues whose Wi-Fi blocks phone-to-Mac traffic.
/// The phone is the peripheral and the Mac backend (bleak) connects as the central.
///
/// Uplink notifications (phone -> Mac), one packet each:
///   "J" + JSON                                  telemetry / hello, must fit one packet
///   "S" + frameId + UInt32 length (BE) + bytes  first chunk of a JPEG frame
///   "C" + frameId + bytes                       following chunks
/// Downlink writes (Mac -> phone): one JSON message per write (braille, status).
final class BLELink: NSObject, CBPeripheralManagerDelegate {
    static let serviceUUID = CBUUID(string: "7A1E0001-4E48-4B9C-9A3D-1C5D2E8F0A01")
    static let uplinkUUID = CBUUID(string: "7A1E0002-4E48-4B9C-9A3D-1C5D2E8F0A01")
    static let downlinkUUID = CBUUID(string: "7A1E0003-4E48-4B9C-9A3D-1C5D2E8F0A01")

    var onState: ((Bool) -> Void)?
    var onMessage: (([String: Any]) -> Void)?

    private let queue = DispatchQueue(label: "nh.ble", qos: .userInitiated)
    private var manager: CBPeripheralManager!
    private var uplink: CBMutableCharacteristic?
    private var central: CBCentral?
    private var packets: [Data] = []
    private var frameId: UInt8 = 0
    private var connected = false

    override init() {
        super.init()
        manager = CBPeripheralManager(delegate: self, queue: queue)
    }

    /// True when the Mac is subscribed and the previous frame has fully gone out.
    var readyForFrame: Bool {
        queue.sync { connected && !packets.contains { $0.first == UInt8(ascii: "S") || $0.first == UInt8(ascii: "C") } }
    }

    var isConnected: Bool {
        queue.sync { connected }
    }

    func sendJSON(_ object: [String: Any]) {
        guard let json = try? JSONSerialization.data(withJSONObject: object) else { return }
        queue.async {
            guard self.connected, let central = self.central else { return }
            let packet = Data("J".utf8) + json
            guard packet.count <= central.maximumUpdateValueLength else { return }
            // Fresh telemetry jumps ahead of frame chunks; older telemetry is dropped.
            self.packets.removeAll { $0.first == UInt8(ascii: "J") }
            self.packets.insert(packet, at: 0)
            self.pump()
        }
    }

    func sendFrame(_ jpeg: Data) {
        queue.async {
            guard self.connected, let central = self.central else { return }
            self.frameId &+= 1
            let payload = central.maximumUpdateValueLength - 2
            var length = UInt32(jpeg.count).bigEndian
            var first = Data([UInt8(ascii: "S"), self.frameId])
            first.append(Data(bytes: &length, count: 4))
            let firstTake = min(jpeg.count, payload - 4)
            first.append(jpeg.subdata(in: 0..<firstTake))
            self.packets.append(first)
            var offset = firstTake
            while offset < jpeg.count {
                let take = min(payload, jpeg.count - offset)
                var chunk = Data([UInt8(ascii: "C"), self.frameId])
                chunk.append(jpeg.subdata(in: offset..<(offset + take)))
                self.packets.append(chunk)
                offset += take
            }
            self.pump()
        }
    }

    // MARK: - Sending (on `queue`)

    private func pump() {
        guard let uplink else { return }
        while let packet = packets.first {
            // false = transmit queue full; peripheralManagerIsReady() calls us again.
            guard manager.updateValue(packet, for: uplink, onSubscribedCentrals: nil) else { return }
            packets.removeFirst()
        }
    }

    private func setConnected(_ value: Bool) {
        guard value != connected else { return }
        connected = value
        onState?(value)
    }

    // MARK: - CBPeripheralManagerDelegate

    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        guard peripheral.state == .poweredOn else {
            central = nil
            packets.removeAll()
            setConnected(false)
            return
        }
        let uplink = CBMutableCharacteristic(type: Self.uplinkUUID, properties: [.notify], value: nil, permissions: [.readable])
        let downlink = CBMutableCharacteristic(type: Self.downlinkUUID, properties: [.write, .writeWithoutResponse], value: nil, permissions: [.writeable])
        let service = CBMutableService(type: Self.serviceUUID, primary: true)
        service.characteristics = [uplink, downlink]
        self.uplink = uplink
        peripheral.removeAllServices()
        peripheral.add(service)
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didAdd service: CBService, error: Error?) {
        guard error == nil else { return }
        peripheral.startAdvertising([
            CBAdvertisementDataServiceUUIDsKey: [Self.serviceUUID],
            CBAdvertisementDataLocalNameKey: "NightHacks",
        ])
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral, didSubscribeTo characteristic: CBCharacteristic) {
        self.central = central
        peripheral.setDesiredConnectionLatency(.low, for: central)
        setConnected(true)
        sendJSON(["type": "hello", "app": "NightHacks iOS", "via": "ble"])
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral, didUnsubscribeFrom characteristic: CBCharacteristic) {
        self.central = nil
        packets.removeAll()
        setConnected(false)
    }

    func peripheralManagerIsReady(toUpdateSubscribers peripheral: CBPeripheralManager) {
        pump()
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveWrite requests: [CBATTRequest]) {
        for request in requests {
            if let value = request.value,
               let object = try? JSONSerialization.jsonObject(with: value) as? [String: Any] {
                onMessage?(object)
            }
        }
        if let first = requests.first {
            peripheral.respond(to: first, withResult: .success)
        }
    }
}
