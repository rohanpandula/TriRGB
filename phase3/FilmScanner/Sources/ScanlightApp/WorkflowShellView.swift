// WorkflowShellView — the redesigned app shell.
//
// Replaces the 5 peer tabs (Session / Calibrate / Scan / Develop / Set Up) with
// a workflow-ordered sidebar + a persistent readiness strip. The IA now matches
// the actual job: a linear per-roll pipeline (Set up → Calibrate → Scan →
// Develop) plus two utilities (Diagnostics, Film stocks) that are tools, not
// steps. The old "Session" dashboard is gone — it only mirrored state that the
// readiness strip now shows live and always-on.
//
// This file is pure navigation/presentation. Every content pane is an EXISTING
// view reused verbatim (ScanSettingsView, CalibrationWizardView, ScanView,
// PositiveInversionView, ScanlightView, FilmStockManagerView), so no pipeline,
// calibration, or capture logic moves here.

import SwiftUI

/// Sidebar destinations. The first four are the ordered workflow; the last two
/// are utilities surfaced below a divider.
enum NavItem: Hashable, CaseIterable, Identifiable {
    case setup, calibrate, scan, develop   // workflow (ordered)
    case diagnostics, filmStocks           // utilities

    var id: Self { self }

    var title: String {
        switch self {
        case .setup:       return "Set up"
        case .calibrate:   return "Calibrate"
        case .scan:        return "Scan"
        case .develop:     return "Develop"
        case .diagnostics: return "Diagnostics"
        case .filmStocks:  return "Film stocks"
        }
    }

    /// 1-based step number for workflow stages; nil for utilities (icon instead).
    var stepNumber: Int? {
        switch self {
        case .setup: return 1
        case .calibrate: return 2
        case .scan: return 3
        case .develop: return 4
        default: return nil
        }
    }

    var utilityIcon: String? {
        switch self {
        case .diagnostics: return "wrench.and.screwdriver"
        case .filmStocks:  return "square.stack.3d.up"
        default: return nil
        }
    }

    /// Stable AX identifier for the sidebar row — the automation entry point for
    /// switching panes (replaces the old `app.tabs[...]` navigation).
    var axID: String {
        switch self {
        case .setup:       return AccessibilityID.navSetup
        case .calibrate:   return AccessibilityID.navCalibrate
        case .scan:        return AccessibilityID.navScan
        case .develop:     return AccessibilityID.navDevelop
        case .diagnostics: return AccessibilityID.navDiagnostics
        case .filmStocks:  return AccessibilityID.navFilmStocks
        }
    }
}

/// A coarse readiness level for a status pill in the strip.
private enum ReadyLevel {
    case ok, warn, bad, idle
    var color: Color {
        switch self {
        case .ok:   return Theme.State.success
        case .warn: return Theme.State.warning
        case .bad:  return Theme.State.danger
        case .idle: return Theme.State.idle
        }
    }
}

/// Persistent, always-visible status header: the single home for roll/output
/// identity and live hardware readiness (light, camera, backend). Replaces the
/// per-tab "Readiness" cards and scattered connect prompts.
struct ReadinessStrip: View {
    @ObservedObject var store: SettingsStore
    @ObservedObject var light: ScanlightViewModel
    @ObservedObject var camera: SonyCameraConnection
    @ObservedObject var client: OrchestratorClient
    @ObservedObject var coordinator: ScanCoordinator
    var onConnectLight: () -> Void

    var body: some View {
        HStack(spacing: Theme.Space.lg) {
            Label(store.settings.rollName.isEmpty ? "Unnamed roll" : store.settings.rollName,
                  systemImage: "film")
                .foregroundStyle(.primary)

            Label(abbreviatedOutput, systemImage: "folder")
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)

            Spacer(minLength: Theme.Space.md)

            pill("Light", lightLevel)
            pill("Camera", cameraLevel)
            pill("Backend", client.isRunning ? .ok : .idle)

            if !light.isConnected {
                Button("Connect light", action: onConnectLight)
                    .controlSize(.small)
            }
        }
        .font(.callout)
        .padding(.horizontal, Theme.Space.xl)
        .padding(.vertical, Theme.Space.sm)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.panelFill)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(AccessibilityID.readinessStrip)
    }

    private func pill(_ label: String, _ level: ReadyLevel) -> some View {
        HStack(spacing: 5) {
            Circle().fill(level.color).frame(width: 7, height: 7)
            Text(label).foregroundStyle(.primary)
        }
        .accessibilityElement(children: .combine)
    }

    private var abbreviatedOutput: String {
        let p = store.settings.outputFolder
        guard !p.isEmpty else { return "No output set" }
        return (p as NSString).abbreviatingWithTildeInPath
    }

    private var lightLevel: ReadyLevel {
        if coordinator.phase == .scanning || coordinator.phase == .calibrating { return .ok }
        return light.isConnected ? .ok : .bad
    }

    private var cameraLevel: ReadyLevel {
        switch camera.phase {
        case .online:        return .ok
        case .checking:      return .warn
        case .configured:    return .warn   // saved, not yet verified
        case .offline, .stale, .notConfigured: return .bad
        case .notUsed:       return .idle
        }
    }
}
