import CoreLocation

/// GPS for Lexis companion mode. Runs only while the wearer has sharing on.
/// Street names come from reverse geocoding, throttled to once every 30 s.
final class LocationTracker: NSObject, CLLocationManagerDelegate {
    var onFix: ((CLLocation, String) -> Void)?
    var onAuthorizationDenied: (() -> Void)?

    private let manager = CLLocationManager()
    private let geocoder = CLGeocoder()
    private var address = ""
    private var lastGeocodeAt = Date.distantPast
    private(set) var latest: CLLocation?
    private var wanted = false

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyBest
        manager.distanceFilter = 5
        manager.activityType = .fitness
    }

    func start() {
        wanted = true
        if manager.authorizationStatus == .notDetermined {
            manager.requestWhenInUseAuthorization()
        }
        manager.startUpdatingLocation()
    }

    func stop() {
        wanted = false
        manager.stopUpdatingLocation()
        latest = nil
        address = ""
    }

    /// Forward-geocodes a typed destination ("Doe Library, Berkeley").
    func geocode(_ query: String, completion: @escaping (CLLocation?, String) -> Void) {
        CLGeocoder().geocodeAddressString(query) { placemarks, _ in
            let place = placemarks?.first
            completion(place?.location, place?.name ?? query)
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        switch manager.authorizationStatus {
        case .denied, .restricted:
            onAuthorizationDenied?()
        case .authorizedWhenInUse, .authorizedAlways:
            if wanted { manager.startUpdatingLocation() }
        default:
            break
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard wanted, let fix = locations.last, fix.horizontalAccuracy >= 0, fix.horizontalAccuracy <= 100 else { return }
        latest = fix
        reverseGeocodeIfDue(fix)
        onFix?(fix, address)
    }

    private func reverseGeocodeIfDue(_ fix: CLLocation) {
        guard !geocoder.isGeocoding, Date().timeIntervalSince(lastGeocodeAt) > 30 else { return }
        lastGeocodeAt = Date()
        geocoder.reverseGeocodeLocation(fix) { [weak self] placemarks, _ in
            guard let place = placemarks?.first else { return }
            let street = [place.subThoroughfare, place.thoroughfare].compactMap { $0 }.joined(separator: " ")
            var parts: [String] = []
            if let name = place.name, !name.isEmpty, name != street { parts.append(name) }
            if !street.isEmpty { parts.append(street) }
            if let city = place.locality { parts.append(city) }
            self?.address = parts.joined(separator: ", ")
        }
    }
}
