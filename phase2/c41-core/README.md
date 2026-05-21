# c41-core

Versioned data contracts and synthetic C-41 orange-mask fixtures for the v1.2 narrowband
inversion pipeline. This package is the dependency root of milestone v1.2 (NFR-13): it
defines the stable import seams (`BaseRegionDescriptor`, `ChannelCalibration`,
`CalibrationResult`, `InversionParams`) that let Phases 09-13 (Python pipeline) and
Phase 14 (SwiftUI wizard) build and test in parallel against fixed shapes, and provides
the synthetic test data (`make_c41_negative`, `make_rebate_strip`) those phases consume
with no real-hardware dependency.
