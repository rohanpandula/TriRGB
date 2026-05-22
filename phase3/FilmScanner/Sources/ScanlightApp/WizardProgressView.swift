// WizardProgressView — sticky 4-circle progress indicator for the calibration wizard.
//
// Four 24-pt numbered circles connected by 1-pt Theme.panelStroke lines.
// Circle fill: active→Theme.accent, completed→Theme.State.success (with checkmark),
//              pending→Theme.State.idle.
// Each circle carries its AX-ID and AX-value ("active"/"completed"/"pending").
//
// Theme.accent is used ONLY for the active circle (UI-SPEC color rule, SC-2).
// NO Theme.Channel.red/green/blue used anywhere here.

import SwiftUI

struct WizardProgressView: View {

    let currentStep: Int       // 1–4, the active step
    // completedSteps removed — circleState(for:) derives completion from currentStep alone

    var body: some View {
        HStack(spacing: 0) {
            ForEach(1...4, id: \.self) { stepNum in
                // Step circle
                stepCircle(for: stepNum)

                // Connector line (not after the last step)
                if stepNum < 4 {
                    Rectangle()
                        .fill(Theme.panelStroke)
                        .frame(height: 1)
                        .frame(maxWidth: .infinity)
                        .accessibilityHidden(true)
                }
            }
        }
        .padding(.horizontal, Theme.Space.xl)
    }

    // MARK: - Private helpers

    private func stepCircle(for stepNum: Int) -> some View {
        let state = circleState(for: stepNum)
        let axID  = axID(for: stepNum)
        let axVal = state.axValue

        return VStack(spacing: Theme.Space.xs) {
            ZStack {
                Circle()
                    .fill(state.fillColor)
                    .frame(width: 24, height: 24)

                if state == .completed {
                    Image(systemName: "checkmark")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Theme.State.success)
                } else {
                    Text("\(stepNum)")
                        .font(.caption.weight(.semibold))
                        .monospacedDigit()
                        .foregroundStyle(state.textColor)
                }
            }
            .accessibilityIdentifier(axID)
            .accessibilityValue(axVal)
            .accessibilityLabel("Step \(stepNum)")

            Text(stepLabel(for: stepNum))
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
        }
        .fixedSize()
    }

    private enum CircleState {
        case active, completed, pending

        var fillColor: Color {
            switch self {
            case .active:    return Theme.accent
            case .completed: return Theme.State.success.opacity(0.15)
            case .pending:   return Theme.State.idle.opacity(0.2)
            }
        }

        var textColor: Color {
            switch self {
            case .active:  return .white
            case .completed, .pending: return .secondary
            }
        }

        var axValue: String {
            switch self {
            case .active:    return "active"
            case .completed: return "completed"
            case .pending:   return "pending"
            }
        }
    }

    private func circleState(for stepNum: Int) -> CircleState {
        if stepNum < currentStep { return .completed }
        if stepNum == currentStep { return .active }
        return .pending
    }

    private func axID(for stepNum: Int) -> String {
        switch stepNum {
        case 1: return AccessibilityID.wizardStep1Indicator
        case 2: return AccessibilityID.wizardStep2Indicator
        case 3: return AccessibilityID.wizardStep3Indicator
        default: return AccessibilityID.wizardStep4Indicator
        }
    }

    private func stepLabel(for stepNum: Int) -> String {
        switch stepNum {
        case 1: return "1 \u{00B7} Rig"
        case 2: return "2 \u{00B7} Exposure"
        case 3: return "3 \u{00B7} Flat Field"
        default: return "4 \u{00B7} Results"
        }
    }
}
