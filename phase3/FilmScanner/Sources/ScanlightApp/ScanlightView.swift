// ScanlightView — pure rendering layer for the Scanlight light source
// controller. Every interactive element is tagged with a stable
// accessibilityIdentifier drawn from the AccessibilityID enum. All state and
// all mutations route through ScanlightViewModel — this view contains
// zero calls to Scanlight, FakeTransport, SerialPortTransport, or any
// driver type.

import SwiftUI

struct ScanlightView: View {
    @ObservedObject var viewModel: ScanlightViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {

                // MARK: Connection row

                GroupBox(label: Text("Connection").font(.headline)) {
                    HStack(spacing: 8) {
                        TextField("/dev/cu.usbmodemXXX", text: $viewModel.port)
                            .accessibilityIdentifier(AccessibilityID.portTextField)
                            .textFieldStyle(.roundedBorder)
                            .frame(minWidth: 180)

                        Button("Connect") { viewModel.connect() }
                            .accessibilityIdentifier(AccessibilityID.connectButton)

                        Button("Disconnect") { viewModel.disconnect() }
                            .accessibilityIdentifier(AccessibilityID.disconnectButton)
                    }
                    .padding(.top, 4)
                }

                // MARK: Status grid

                GroupBox(label: Text("Status").font(.headline)) {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text("Connection:")
                                .frame(width: 120, alignment: .trailing)
                            Text(viewModel.connectionStatusString)
                                .accessibilityIdentifier(AccessibilityID.connectionStatusLabel)
                                .accessibilityValue(viewModel.connectionStatusString)
                        }
                        HStack {
                            Text("Firmware:")
                                .frame(width: 120, alignment: .trailing)
                            Text(viewModel.firmwareString)
                                .accessibilityIdentifier(AccessibilityID.firmwareLabel)
                                .accessibilityValue(viewModel.firmwareString)
                        }
                        HStack {
                            Text("Hardware:")
                                .frame(width: 120, alignment: .trailing)
                            Text(viewModel.hardwareString)
                                .accessibilityIdentifier(AccessibilityID.hardwareLabel)
                                .accessibilityValue(viewModel.hardwareString)
                        }
                        HStack {
                            Text("LED Temp:")
                                .frame(width: 120, alignment: .trailing)
                            Text(viewModel.ledTempString)
                                .accessibilityIdentifier(AccessibilityID.ledTempLabel)
                                .accessibilityValue(viewModel.ledTempString)
                        }
                        HStack {
                            Text("VBUS:")
                                .frame(width: 120, alignment: .trailing)
                            Text(viewModel.vbusString)
                                .accessibilityIdentifier(AccessibilityID.vbusLabel)
                                .accessibilityValue(viewModel.vbusString)
                        }
                    }
                    .padding(.top, 4)
                }

                // MARK: Channel controls

                GroupBox(label: Text("Channels").font(.headline)) {
                    VStack(spacing: 8) {
                        channelRow(
                            label: "Red",
                            level: $viewModel.redLevel,
                            sliderId: AccessibilityID.redSlider,
                            onButtonId: AccessibilityID.redOnButton,
                            onAction: { viewModel.turnOnRed() }
                        )
                        channelRow(
                            label: "Green",
                            level: $viewModel.greenLevel,
                            sliderId: AccessibilityID.greenSlider,
                            onButtonId: AccessibilityID.greenOnButton,
                            onAction: { viewModel.turnOnGreen() }
                        )
                        channelRow(
                            label: "Blue",
                            level: $viewModel.blueLevel,
                            sliderId: AccessibilityID.blueSlider,
                            onButtonId: AccessibilityID.blueOnButton,
                            onAction: { viewModel.turnOnBlue() }
                        )
                        channelRow(
                            label: "White",
                            level: $viewModel.whiteLevel,
                            sliderId: AccessibilityID.whiteSlider,
                            onButtonId: AccessibilityID.whiteOnButton,
                            onAction: { viewModel.turnOnWhite() }
                        )
                    }
                    .padding(.top, 4)
                }

                // MARK: Action row

                GroupBox(label: Text("Actions").font(.headline)) {
                    HStack(spacing: 12) {
                        Button("All Off") { viewModel.allOff() }
                            .accessibilityIdentifier(AccessibilityID.allChannelsOffButton)

                        Button("Set RGB") { viewModel.setAllRGB() }
                            .accessibilityIdentifier(AccessibilityID.setAllRGBButton)
                    }
                    .padding(.top, 4)
                }

                // MARK: Pulse row

                GroupBox(label: Text("Shutter Pulse").font(.headline)) {
                    HStack(spacing: 8) {
                        Text("Pulse (ms):")
                        TextField("100", text: $viewModel.pulseMs)
                            .accessibilityIdentifier(AccessibilityID.pulseMsTextField)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 80)

                        Button("Fire") { viewModel.firePulse() }
                            .accessibilityIdentifier(AccessibilityID.firePulseButton)
                    }
                    .padding(.top, 4)
                }

                // MARK: Error label

                GroupBox(label: Text("Last Error").font(.headline)) {
                    Text(viewModel.lastError.isEmpty ? "—" : viewModel.lastError)
                        .accessibilityIdentifier(AccessibilityID.lastErrorLabel)
                        .accessibilityValue(viewModel.lastError)
                        .foregroundColor(viewModel.lastError.isEmpty ? .secondary : .red)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, 4)
                }

                // MARK: Log area

                GroupBox(label: Text("Log").font(.headline)) {
                    VStack(alignment: .leading, spacing: 4) {
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 2) {
                                ForEach(viewModel.logLines, id: \.self) { line in
                                    Text(line)
                                        .font(.system(.caption, design: .monospaced))
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                }
                            }
                            .padding(4)
                        }
                        .accessibilityIdentifier(AccessibilityID.logScrollView)
                        .frame(minHeight: 80, maxHeight: 160)

                        Button("Clear") { viewModel.clearLog() }
                            .accessibilityIdentifier(AccessibilityID.clearLogButton)
                    }
                    .padding(.top, 4)
                }
            }
            .padding()
        }
    }

    // MARK: - Private helpers

    @ViewBuilder
    private func channelRow(
        label: String,
        level: Binding<Double>,
        sliderId: String,
        onButtonId: String,
        onAction: @escaping () -> Void
    ) -> some View {
        HStack(spacing: 8) {
            Text(label)
                .frame(width: 44, alignment: .trailing)
            Slider(value: level, in: 0...255)
                .accessibilityIdentifier(sliderId)
                .frame(minWidth: 120)
            Text("\(Int(level.wrappedValue))")
                .frame(width: 30, alignment: .trailing)
                .monospacedDigit()
            Button("On") { onAction() }
                .accessibilityIdentifier(onButtonId)
        }
    }
}
