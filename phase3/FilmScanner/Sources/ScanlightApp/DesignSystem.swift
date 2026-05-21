// DesignSystem — the shared visual vocabulary for the Scanlight app.
//
// Product register (impeccable): design SERVES the task. Native macOS controls,
// a Restrained color strategy (tinted neutral chrome + one accent), and one
// consistent component vocabulary across every tab. The RGBW channels — the
// literal subject of an RGB light panel — carry the only saturated color.
// Everything resolves through semantic SwiftUI colors so the app adapts to the
// operator's light/dark environment instead of distorting color perception by
// forcing a theme.
//
// Nothing here touches behavior or accessibility identifiers; it is pure
// presentation. Views keep their AX-IDs, element types, and accessibilityValue
// wiring exactly as the automation contract (docs/automation.md) requires.

import AppKit
import SwiftUI

// MARK: - Tokens

enum Theme {

    /// Spacing scale. Section gaps are generous, intra-section gaps tight, so
    /// the eye groups by proximity rather than by box borders.
    enum Space {
        static let xs: CGFloat = 4
        static let sm: CGFloat = 8
        static let md: CGFloat = 12
        static let lg: CGFloat = 16
        static let xl: CGFloat = 22
        static let section: CGFloat = 20
    }

    enum Radius {
        static let chip: CGFloat = 5
        static let control: CGFloat = 7
        static let panel: CGFloat = 11
    }

    /// Accent — a calibrated teal. Deliberately not the "instrument-panel blue"
    /// category reflex, and clear of the R/G/B channel hues and the
    /// success/warning/danger/info semantics. Brighter in dark, deeper in light.
    static let accent = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(srgbRed: 0.36, green: 0.80, blue: 0.74, alpha: 1)
            : NSColor(srgbRed: 0.02, green: 0.52, blue: 0.49, alpha: 1)
    })

    // Panel surface: a faint neutral lift over the window, plus a hairline.
    // primary.opacity adapts automatically (white-ish on dark, ink on light).
    static let panelFill = Color.primary.opacity(0.045)
    static let panelStroke = Color.primary.opacity(0.09)

    // Semantic state colors. System hues already adapt to appearance and the
    // user's accessibility settings; we only standardize *meaning* and dosage.
    enum State {
        static let success = Color.green
        static let warning = Color.orange
        static let danger = Color.red
        static let info = Color.blue
        static let idle = Color.secondary
    }

    /// Channel tints for the RGBW sliders. The 5000K white channel reads as a
    /// warm parchment so its slider is visible in both appearances without
    /// pretending to be a pure-white fill.
    enum Channel {
        static let red = Color(.sRGB, red: 0.93, green: 0.26, blue: 0.26, opacity: 1)
        static let green = Color(.sRGB, red: 0.30, green: 0.78, blue: 0.40, opacity: 1)
        static let blue = Color(.sRGB, red: 0.27, green: 0.53, blue: 0.96, opacity: 1)
        static let white = Color(.sRGB, red: 0.82, green: 0.76, blue: 0.62, opacity: 1)
    }
}

// MARK: - Panel (GroupBoxStyle)

/// One consistent section container for every tab. Replaces the default
/// GroupBox chrome with a calm tinted surface, a hairline border, a section
/// header, and generous internal padding. Applied once via `.groupBoxStyle`
/// so every existing `GroupBox(label:)` call site inherits it unchanged.
struct PanelGroupBoxStyle: GroupBoxStyle {
    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: Theme.Space.md) {
            configuration.label
                .font(.headline)
                .foregroundStyle(.primary)
            configuration.content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Theme.Space.lg)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.panel, style: .continuous)
                .fill(Theme.panelFill)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.panel, style: .continuous)
                .strokeBorder(Theme.panelStroke, lineWidth: 1)
        )
    }
}

// MARK: - Chip

/// A small, tinted status pill — one shape for every state badge in the app
/// (scan phase, frame composite state, calibration verdict). Consistent
/// vocabulary: same radius, same padding, tint background + tinted label.
struct Chip: View {
    let text: String
    let tint: Color
    var filled: Bool = false

    var body: some View {
        Text(text)
            .font(.caption.weight(.medium))
            .monospacedDigit()
            .padding(.horizontal, Theme.Space.sm)
            .padding(.vertical, 3)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.chip, style: .continuous)
                    .fill(tint.opacity(filled ? 1.0 : 0.16))
            )
            .foregroundStyle(filled ? Color.white : tint)
    }
}

// MARK: - StatusDot

/// A small filled circle used as a non-text-only state indicator (e.g. live
/// connection). Pairs with a text label so meaning never rides on color alone.
struct StatusDot: View {
    let color: Color
    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 8, height: 8)
            .accessibilityHidden(true)
    }
}

// MARK: - Banner

/// An inline message strip for warnings, errors, and locked states. Full-width
/// tinted background + leading icon + tinted text — never a colored side-stripe
/// (an impeccable absolute ban).
struct Banner: View {
    enum Kind { case info, warning, danger
        var tint: Color {
            switch self {
            case .info: return Theme.State.info
            case .warning: return Theme.State.warning
            case .danger: return Theme.State.danger
            }
        }
        var icon: String {
            switch self {
            case .info: return "info.circle.fill"
            case .warning: return "lock.fill"
            case .danger: return "exclamationmark.triangle.fill"
            }
        }
    }

    let kind: Kind
    let text: String
    var icon: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Space.sm) {
            Image(systemName: icon ?? kind.icon)
                .foregroundStyle(kind.tint)
                .accessibilityHidden(true)
            Text(text)
                .font(.callout)
                .foregroundStyle(kind.tint)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Theme.Space.md)
        .padding(.vertical, Theme.Space.sm)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                .fill(kind.tint.opacity(0.12))
        )
    }
}

// MARK: - LabeledValue

/// A read-only label/value row with a calm leading label and a primary value.
/// Replaces the cramped fixed-width right-aligned columns. The caller supplies
/// the value as a fully-configured Text (so its accessibilityIdentifier and
/// accessibilityValue stay on the value element, per the automation contract).
struct LabeledValue<Value: View>: View {
    let label: String
    @ViewBuilder var value: () -> Value

    var body: some View {
        HStack(spacing: Theme.Space.md) {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer(minLength: Theme.Space.md)
            value()
                .font(.body)
                .monospacedDigit()
        }
    }
}
