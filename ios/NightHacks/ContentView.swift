import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var engine: Engine
    @State private var hostField = ""
    @FocusState private var hostFocused: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                header
                proximity
                braille
                connection
                controls
                diagnostics
            }
            .padding(20)
        }
        .scrollDismissesKeyboard(.immediately)
        .background(Color.black.ignoresSafeArea())
        .foregroundStyle(.white)
        .onAppear {
            hostField = engine.host
            engine.start()
        }
    }

    private var header: some View {
        HStack {
            Text("Night Hacks").font(.title.bold())
            Spacer()
            VStack(alignment: .trailing, spacing: 4) {
                Label("Wi-Fi \(engine.linkState.rawValue)", systemImage: "circle.fill")
                    .foregroundStyle(linkColor)
                Label(engine.bleConnected ? "Bluetooth connected" : "Bluetooth waiting", systemImage: "circle.fill")
                    .foregroundStyle(engine.bleConnected ? Color.green : Color.yellow)
            }
            .labelStyle(.titleAndIcon)
            .font(.footnote.weight(.semibold))
        }
    }

    private var proximity: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("NEAREST OBSTACLE").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Text(engine.snapshot.distance.map { String(format: "%.2f m", $0) } ?? "clear")
                .font(.system(size: 56, weight: .bold, design: .rounded))
                .monospacedDigit()
            HStack(spacing: 6) {
                ForEach(1...4, id: \.self) { lvl in
                    RoundedRectangle(cornerRadius: 4)
                        .fill(lvl <= engine.snapshot.level ? levelColor(lvl) : Color.white.opacity(0.12))
                        .frame(height: 14)
                }
            }
        }
    }

    private var braille: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("BRAILLE").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            HStack(alignment: .center, spacing: 24) {
                BrailleCell(mask: engine.brailleMask)
                wordView
            }
        }
    }

    private var wordView: some View {
        let letters = Array(engine.brailleWord)
        return HStack(spacing: 2) {
            if letters.isEmpty {
                Text("—").foregroundStyle(.secondary)
            }
            ForEach(Array(letters.enumerated()), id: \.offset) { index, ch in
                Text(String(ch))
                    .foregroundStyle(index == engine.brailleIndex ? Color.yellow : Color.white.opacity(0.7))
            }
        }
        .font(.system(size: 34, weight: .bold, design: .monospaced))
    }

    private var connection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("MAC BACKEND").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            HStack {
                TextField("Mac name or IP", text: $hostField)
                    .keyboardType(.numbersAndPunctuation)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($hostFocused)
                    .padding(10)
                    .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
                Button("Connect") {
                    hostFocused = false
                    engine.connect(to: hostField)
                }
                .buttonStyle(.borderedProminent)
            }
            if engine.linkState != .connected && !engine.linkError.isEmpty {
                Text(engine.linkError).font(.footnote).foregroundStyle(.orange)
            } else if !engine.macStatus.isEmpty {
                Text(engine.macStatus).font(.footnote).foregroundStyle(.secondary)
            }
        }
    }

    private var controls: some View {
        HStack {
            Toggle("Vibration", isOn: $engine.hapticsEnabled)
                .fixedSize()
            Spacer()
            Button("Test vibration") { engine.testVibration() }
                .buttonStyle(.bordered)
        }
    }

    private var diagnostics: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let error = engine.arError {
                Text(error).foregroundStyle(.red)
            }
            Text("tracking \(engine.snapshot.tracking) · \(Int(engine.snapshot.fps)) fps · \(engine.snapshot.points) depth pts · image \(engine.snapshot.orientation)")
        }
        .font(.footnote.monospaced())
        .foregroundStyle(.secondary)
    }

    private var linkColor: Color {
        switch engine.linkState {
        case .connected: return .green
        case .connecting: return .yellow
        case .disconnected: return .red
        }
    }

    private func levelColor(_ level: Int) -> Color {
        [Color.green, .yellow, .orange, .red][level - 1]
    }
}

/// 2x3 Braille cell; bit0 = dot 1 (top left) ... bit5 = dot 6 (bottom right).
struct BrailleCell: View {
    let mask: Int

    var body: some View {
        HStack(spacing: 12) {
            column(dots: [0, 1, 2])
            column(dots: [3, 4, 5])
        }
        .padding(12)
        .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 10))
    }

    private func column(dots: [Int]) -> some View {
        VStack(spacing: 12) {
            ForEach(dots, id: \.self) { bit in
                Circle()
                    .fill(mask & (1 << bit) != 0 ? Color.yellow : Color.white.opacity(0.15))
                    .frame(width: 22, height: 22)
            }
        }
    }
}
