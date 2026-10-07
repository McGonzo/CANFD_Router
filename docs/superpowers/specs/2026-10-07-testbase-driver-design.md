# TestBase VCI Driver Design

## Goal

Add TestBase VCI devices as a built-in hardware option so users can discover devices and CAN channels, import them into hardware configuration, and use them for CAN/CAN FD routing.

## Approach

Implement a dedicated `TestBaseDriver` and adapter using the SDK's official `python-can-hirain` backend (`hrcan`). Keep the vendor package and Windows driver runtime optional at import time so the application can still start without TestBase installed; report actionable diagnostics when a scan is requested without them. Register the driver under the stable key `testbase`.

Device scanning will use the SDK's hardware-enumeration API and return one `DeviceInfo` per usable CAN channel. Preserve device identity (including serial number when exposed), channel index, and reported CAN/CAN FD capability in device metadata so existing hardware import and routing workflows can distinguish devices and channels.

The adapter will open the selected channel through python-can, apply the configured classic CAN or CAN FD bitrates and the SDK-required channel parameters, and translate messages to and from the application's existing `Frame` type. The existing hardware UI remains the configuration surface; no separate TestBase UI is needed.

## Error handling

Missing python-can, missing `python-can-hirain`, missing TestBase runtime, empty enumeration, and adapter-open failures should produce actionable diagnostics or preserve the underlying open error. Closing an adapter must release the python-can bus. The adapter must retain device serial identity where the backend requires it to select among multiple connected devices.

## Verification

Use driver-level tests with controlled fake SDK enumeration and python-can bus objects to verify device/channel identity, CAN/CAN FD metadata, missing-dependency diagnostics, open configuration, frame send/receive translation, and close behavior. Run the existing test suite and ensure registry scanning continues to tolerate unavailable optional drivers.

## Constraints

- Follow the existing `DriverPlugin` / `BusAdapter` interfaces and hardware metadata conventions.
- Do not bundle or copy vendor binaries from the external SDK folder into this repository.
- Document the required TestBase driver installation and `python-can-hirain` dependency for deployment.
- Do not alter ZLG, NI, or other driver behavior.
