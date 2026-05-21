// ScanlightView — pure rendering layer for the Scanlight light source
// controller. Every interactive element is tagged with a stable
// accessibilityIdentifier drawn from the AccessibilityID enum. All state and
// all mutations route through ScanlightViewModel — this view contains
// zero calls to Scanlight, FakeTransport, SerialPortTransport, or any
// driver type.
//
// Visual language lives in DesignSystem.swift: PanelGroupBoxStyle for sections,
// the Theme tokens for spacing/color, and the RGBW channel tints that make the
// light panel's own subject — its color channels — the only saturated color.

import SwiftUI

struct ScanlightView: View {
    @ObservedObject var viewModel: ScanlightViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {

                // MARK: Connection

                GroupBox(label: Text("Connection")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        HStack(spacing: Theme.Space.sm) {
                            TextField("/dev/cu.usbmodemXXX", text: $viewModel.port)
                                .accessibilityIdentifier(AccessibilityID.portTextField)
                                .textFieldStyle(.roundedBorder)
                                .frame(minWidth: 200)

                            Button("Connect") { viewModel.connect() }
                                .accessibilityIdentifier(AccessibilityID.connectButton)
                                .buttonStyle(.borderedProminent)
                                .disabled(viewModel.isConnected || viewModel.portOwner != .idle)

                            Button("Disconnect") { viewModel.disconnect() }
                                .accessibilityIdentifier(AccessibilityID.disconnectButton)
                                .buttonStyle(.bordered)
                                .disabled(!viewModel.isConnected)
                        }
                        Text("Connect to the Scanlight panel over its USB serial (left) port.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if viewModel.portOwner != .idle {
                            Text("Serial port is held by an active \(viewModel.portOwner == .scanning ? "scan" : "calibration") — stop it before connecting here.")
                                .font(.caption)
                                .foregroundStyle(Theme.State.warning)
                        }
                    }
                }

                // MARK: Status

                GroupBox(label: Text("Status")) {
                    VStack(alignment: .leading, spacing: Theme.Space.sm) {
                        LabeledValue(label: "Connection") {
                            HStack(spacing: Theme.Space.sm) {
                                StatusDot(color: viewModel.isConnected ? Theme.State.success : Theme.State.idle)
                                Text(viewModel.connectionStatusString)
                                    .accessibilityIdentifier(AccessibilityID.connectionStatusLabel)
                                    .accessibilityValue(viewModel.connectionStatusString)
                                    .foregroundStyle(viewModel.isConnected ? .primary : .secondary)
                            }
                        }
                        Divider().opacity(0.5)
                        LabeledValue(label: "Firmware") {
                            Text(viewModel.firmwareString)
                                .accessibilityIdentifier(AccessibilityID.firmwareLabel)
                                .accessibilityValue(viewModel.firmwareString)
                                .foregroundStyle(viewModel.firmwareString == "—" ? .secondary : .primary)
                        }
                        LabeledValue(label: "Hardware") {
                            Text(viewModel.hardwareString)
                                .accessibilityIdentifier(AccessibilityID.hardwareLabel)
                                .accessibilityValue(viewModel.hardwareString)
                                .foregroundStyle(viewModel.hardwareString == "—" ? .secondary : .primary)
                        }
                        LabeledValue(label: "LED temp") {
                            Text(viewModel.ledTempString)
                                .accessibilityIdentifier(AccessibilityID.ledTempLabel)
                                .accessibilityValue(viewModel.ledTempString)
                                .foregroundStyle(viewModel.ledTempString == "—" ? .secondary : .primary)
                        }
                        LabeledValue(label: "VBUS") {
                            Text(viewModel.vbusString)
                                .accessibilityIdentifier(AccessibilityID.vbusLabel)
                                .accessibilityValue(viewModel.vbusString)
                                .foregroundStyle(viewModel.vbusString == "—" ? .secondary : .primary)
                        }
                    }
                }

                // MARK: Channels

                GroupBox(label: Text("Channels")) {
                    VStack(spacing: Theme.Space.md) {
                        channelRow(
                            label: "Red", tint: Theme.Channel.red,
                            level: $viewModel.redLevel,
                            sliderId: AccessibilityID.redSlider,
                            onButtonId: AccessibilityID.redOnButton,
                            onAction: { viewModel.turnOnRed() }
                        )
                        channelRow(
                            label: "Green", tint: Theme.Channel.green,
                            level: $viewModel.greenLevel,
                            sliderId: AccessibilityID.greenSlider,
                            onButtonId: AccessibilityID.greenOnButton,
                            onAction: { viewModel.turnOnGreen() }
                        )
                        channelRow(
                            label: "Blue", tint: Theme.Channel.blue,
                            level: $viewModel.blueLevel,
                            sliderId: AccessibilityID.blueSlider,
                            onButtonId: AccessibilityID.blueOnButton,
                            onAction: { viewModel.turnOnBlue() }
                        )
                        channelRow(
                            label: "White", tint: Theme.Channel.white,
                            level: $viewModel.whiteLevel,
                            sliderId: AccessibilityID.whiteSlider,
                            onButtonId: AccessibilityID.whiteOnButton,
                            onAction: { viewModel.turnOnWhite() }
                        )
                    }
                }

                // MARK: Actions

                GroupBox(label: Text("Actions")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        HStack(spacing: Theme.Space.md) {
                            Button("Set RGB") { viewModel.setAllRGB() }
                                .accessibilityIdentifier(AccessibilityID.setAllRGBButton)
                                .buttonStyle(.borderedProminent)

                            Button("All Off") { viewModel.allOff() }
                                .accessibilityIdentifier(AccessibilityID.allChannelsOffButton)
                                .buttonStyle(.bordered)
                        }
                        Text("White and RGB are mutually exclusive — the firmware blocks them together.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                // MARK: Shutter Pulse

                GroupBox(label: Text("Shutter Pulse")) {
                    VStack(alignment: .leading, spacing: Theme.Space.md) {
                        HStack(spacing: Theme.Space.sm) {
                            Text("Pulse")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                            TextField("100", text: $viewModel.pulseMs)
                                .accessibilityIdentifier(AccessibilityID.pulseMsTextField)
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 72)
                                .monospacedDigit()
                            Text("ms")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)

                            Button("Fire") { viewModel.firePulse() }
                                .accessibilityIdentifier(AccessibilityID.firePulseButton)
                                .buttonStyle(.borderedProminent)
                                .padding(.leading, Theme.Space.xs)
                        }
                        Text("10–2550 ms, in steps of 10.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                // MARK: Messages (last error)

                GroupBox(label: Text("Messages")) {
                    if viewModel.lastError.isEmpty {
                        HStack(spacing: Theme.Space.sm) {
                            Image(systemName: "checkmark.circle")
                                .foregroundStyle(Theme.State.success.opacity(0.85))
                                .accessibilityHidden(true)
                            Text("No errors")
                                .accessibilityIdentifier(AccessibilityID.lastErrorLabel)
                                .accessibilityValue("")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                            Spacer(minLength: 0)
                        }
                    } else {
                        HStack(alignment: .firstTextBaseline, spacing: Theme.Space.sm) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(Theme.State.danger)
                                .accessibilityHidden(true)
                            Text(viewModel.lastError)
                                .accessibilityIdentifier(AccessibilityID.lastErrorLabel)
                                .accessibilityValue(viewModel.lastError)
                                .font(.callout)
                                .foregroundStyle(Theme.State.danger)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                        }
                        .padding(.horizontal, Theme.Space.md)
                        .padding(.vertical, Theme.Space.sm)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                                .fill(Theme.State.danger.opacity(0.12))
                        )
                    }
                }

                // MARK: Log

                GroupBox(label: Text("Log")) {
                    VStack(alignment: .leading, spacing: Theme.Space.sm) {
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 3) {
                                if viewModel.logLines.isEmpty {
                                    Text("No log entries yet. Connect and drive a channel to see activity.")
                                        .font(.caption)
                                        .foregroundStyle(.tertiary)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                } else {
                                    ForEach(viewModel.logLines, id: \.self) { line in
                                        Text(line)
                                            .font(.system(.caption, design: .monospaced))
                                            .foregroundStyle(.secondary)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                            .textSelection(.enabled)
                                    }
                                }
                            }
                            .padding(Theme.Space.sm)
                        }
                        .accessibilityIdentifier(AccessibilityID.logScrollView)
                        .frame(minHeight: 88, maxHeight: 168)
                        .background(
                            RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                                .fill(Color.primary.opacity(0.03))
                        )

                        Button("Clear") { viewModel.clearLog() }
                            .accessibilityIdentifier(AccessibilityID.clearLogButton)
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                            .disabled(viewModel.logLines.isEmpty)
                    }
                }
            }
            .padding(Theme.Space.xl)
        }
        .groupBoxStyle(PanelGroupBoxStyle())
    }

    // MARK: - Private helpers

    @ViewBuilder
    private func channelRow(
        label: String,
        tint: Color,
        level: Binding<Double>,
        sliderId: String,
        onButtonId: String,
        onAction: @escaping () -> Void
    ) -> some View {
        HStack(spacing: Theme.Space.md) {
            HStack(spacing: Theme.Space.sm) {
                Circle()
                    .fill(tint)
                    .frame(width: 9, height: 9)
                    .accessibilityHidden(true)
                Text(label)
                    .font(.subheadline)
                    .frame(width: 46, alignment: .leading)
            }
            Slider(value: level, in: 0...255)
                .accessibilityIdentifier(sliderId)
                .tint(tint)
                .frame(minWidth: 140)
            Text("\(Int(level.wrappedValue))")
                .font(.body)
                .monospacedDigit()
                .foregroundStyle(tint)
                .frame(width: 36, alignment: .trailing)
            Button("On") { onAction() }
                .accessibilityIdentifier(onButtonId)
                .buttonStyle(.bordered)
                .controlSize(.small)
                .tint(tint)
        }
    }
}
