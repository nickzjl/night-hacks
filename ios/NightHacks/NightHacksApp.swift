import SwiftUI

@main
struct NightHacksApp: App {
    @StateObject private var engine = Engine()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(engine)
                .preferredColorScheme(.dark)
        }
        .onChange(of: scenePhase) { _, phase in
            engine.scenePhaseChanged(phase)
        }
    }
}
