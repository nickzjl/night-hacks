import ARKit
import ImageIO
import simd

/// Turns one ARFrame's LiDAR depth map into "how far is the nearest thing in front of me".
///
/// The phone is mounted sideways on the head, so everything here is orientation-agnostic:
/// - we sample a cone around the optical axis instead of an image rectangle, and
/// - we use the camera pose (gravity) to drop floor points, whichever way the phone is rotated.
enum DepthProximity {
    struct Sample {
        let distance: Float?          // meters to the nearest obstacle in the cone, nil when clear
        let points: Int               // depth samples that passed every filter
        let orientation: CGImagePropertyOrientation  // rotation that makes the camera image upright
    }

    /// Half-angle of the forward cone we care about.
    static let coneTan: Float = tan(22 * Float.pi / 180)
    /// Points more than this far below the camera are treated as floor.
    static let floorDrop: Float = 1.15
    static let minDepth: Float = 0.15
    static let maxDepth: Float = 6.0
    /// Sample every Nth pixel of the 256x192 depth map.
    static let pixelStride = 3
    /// Need at least this many hits before we believe there's an obstacle.
    static let minPoints = 15

    static func measure(frame: ARFrame) -> Sample {
        let t = frame.camera.transform
        // Camera-to-world rotation; columns are the camera axes expressed in world space.
        let r = simd_float3x3(
            SIMD3<Float>(t.columns.0.x, t.columns.0.y, t.columns.0.z),
            SIMD3<Float>(t.columns.1.x, t.columns.1.y, t.columns.1.z),
            SIMD3<Float>(t.columns.2.x, t.columns.2.y, t.columns.2.z)
        )
        // World "down" in camera coordinates. Image x follows camera +x, image rows follow camera -y.
        let downCam = r.transpose * SIMD3<Float>(0, -1, 0)
        let orientation = imageOrientation(downX: downCam.x, downY: -downCam.y)

        guard let depth = frame.smoothedSceneDepth ?? frame.sceneDepth else {
            return Sample(distance: nil, points: 0, orientation: orientation)
        }

        let depthMap = depth.depthMap
        CVPixelBufferLockBaseAddress(depthMap, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depthMap, .readOnly) }
        guard let depthBase = CVPixelBufferGetBaseAddress(depthMap)?.assumingMemoryBound(to: Float32.self) else {
            return Sample(distance: nil, points: 0, orientation: orientation)
        }
        let width = CVPixelBufferGetWidth(depthMap)
        let height = CVPixelBufferGetHeight(depthMap)
        let depthRow = CVPixelBufferGetBytesPerRow(depthMap) / MemoryLayout<Float32>.stride

        var confBase: UnsafeMutablePointer<UInt8>?
        var confRow = 0
        let confMap = depth.confidenceMap
        if let confMap {
            CVPixelBufferLockBaseAddress(confMap, .readOnly)
            confBase = CVPixelBufferGetBaseAddress(confMap)?.assumingMemoryBound(to: UInt8.self)
            confRow = CVPixelBufferGetBytesPerRow(confMap)
        }
        defer { if let confMap { CVPixelBufferUnlockBaseAddress(confMap, .readOnly) } }
        let minConfidence = UInt8(ARConfidenceLevel.medium.rawValue)

        // Intrinsics are for capturedImage; scale them down to the depth map.
        let k = frame.camera.intrinsics
        let res = frame.camera.imageResolution
        let sx = Float(width) / Float(res.width)
        let sy = Float(height) / Float(res.height)
        let fx = k[0][0] * sx, fy = k[1][1] * sy
        let cx = k[2][0] * sx, cy = k[2][1] * sy
        // World-up component of each camera axis, for the floor test.
        let upX = r.columns.0.y, upY = r.columns.1.y, upZ = r.columns.2.y

        var hits: [Float] = []
        hits.reserveCapacity(1024)
        for v in stride(from: 0, to: height, by: pixelStride) {
            for u in stride(from: 0, to: width, by: pixelStride) {
                let d = depthBase[v * depthRow + u]
                guard d > minDepth, d < maxDepth else { continue }   // also rejects NaN
                if let confBase, confBase[v * confRow + u] < minConfidence { continue }
                let xc = (Float(u) - cx) * d / fx
                let yc = -(Float(v) - cy) * d / fy
                let coneRadius = coneTan * d
                if xc * xc + yc * yc > coneRadius * coneRadius { continue }
                let heightBelowCamera = upX * xc + upY * yc + upZ * -d
                if heightBelowCamera < -floorDrop { continue }
                hits.append(d)
            }
        }

        guard hits.count >= minPoints else {
            return Sample(distance: nil, points: hits.count, orientation: orientation)
        }
        hits.sort()
        // 10th percentile: ignores a few stray close pixels but still reacts to thin obstacles.
        return Sample(distance: hits[hits.count / 10], points: hits.count, orientation: orientation)
    }

    /// Rotation that makes the sensor image upright, given which image direction gravity points to.
    static func imageOrientation(downX: Float, downY: Float) -> CGImagePropertyOrientation {
        if abs(downY) >= abs(downX) {
            return downY >= 0 ? .up : .down
        }
        return downX >= 0 ? .right : .left
    }
}

/// Debounces noisy per-frame distances into a stable 0-4 proximity level.
final class ProximityFilter {
    /// Enter level n+1 when the filtered distance drops below thresholds[n] (meters).
    static let thresholds: [Float] = [2.0, 1.3, 0.8, 0.45]
    static let hysteresis: Float = 0.15
    static let window = 5
    /// Consecutive agreeing updates needed before the level changes (at ~30 Hz).
    static let framesToRise = 3
    static let framesToFall = 9

    private var recent: [Float] = []
    private(set) var level = 0
    private(set) var distance: Float?
    private var pendingLevel = 0
    private var pendingCount = 0

    func update(_ raw: Float?) -> (level: Int, distance: Float?) {
        recent.append(raw ?? .infinity)
        if recent.count > Self.window { recent.removeFirst() }
        let median = recent.sorted()[recent.count / 2]
        distance = median.isFinite ? median : nil

        let target = targetLevel(for: median)
        if target == level {
            pendingCount = 0
        } else {
            if target == pendingLevel {
                pendingCount += 1
            } else {
                pendingLevel = target
                pendingCount = 1
            }
            if pendingCount >= (target > level ? Self.framesToRise : Self.framesToFall) {
                level = target
                pendingCount = 0
            }
        }
        return (level, distance)
    }

    func reset() {
        recent.removeAll()
        level = 0
        distance = nil
        pendingCount = 0
    }

    private func targetLevel(for d: Float) -> Int {
        var target = 0
        for (i, threshold) in Self.thresholds.enumerated() {
            let lvl = i + 1
            // Staying in a level we're already at is easier than entering it.
            let limit = lvl <= level ? threshold + Self.hysteresis : threshold
            if d < limit { target = lvl }
        }
        return target
    }
}
