# CAN/CANFD Signal Gateway Router

Windows-style desktop software for CAN/CAN FD routing, with per-port DBC binding, topology wiring, and signal-to-signal mapping.

## What is implemented

- Hardware configuration page:
  - Scan and recognize device ports from multiple vendors/drivers.
  - Import selected ports into software configuration.
  - Manual unique port naming (required for mapping).
  - Per-port DBC file assignment (DBC is configured in hardware page).
  - DBC Browser for parsed message/signal inspection.
  - DBC mode indicator (`strict` or `compatible`) per port.
  - DBC Browser supports both table view and message->signal tree view.
  - DBC Browser shows message DLC, including CANFD DLC > 8.
  - Port ordering, add/edit/remove.
  - One-click creation of 4 virtual CAN channels for offline testing.
- Route configuration page:
  - Circuit-like topology panel.
  - Manual wiring by click: source port -> target port.
  - Left/Right signal tables:
    - left table = source mapping rows (`No.`, `port`, `message`, `signal`)
    - right table = target mapping rows (`No.`, `port`, `message`, `signal`)
    - rows are one-to-one by sequence number
    - add/remove rows; double-click left or right row to open signal search dialog
    - source row supports simple math expression (`Calc(x)`) before forwarding
    - `Edit Row` dialog exposes advanced rule settings:
      - enable/disable
      - transform expression, scale, offset
      - source ID filter / target ID override
      - CAN/CANFD length strategy
      - bind RC/CRC presets to target signals in the same target message
      - support `Source Port = None` for software-generated direct TX rows
      - source-less rows can periodically send target DBC defaults plus configured RC/CRC values
  - dedicated `Test` sub-tab:
    - manual signal injection
    - random signal generator for virtual source ports
    - manual value override for instant validation
    - direct RC/CRC algorithm sender for target CAN network
    - random generator requires router running to avoid background popup loops
- Algorithm configuration page:
  - built-in rolling counter and CRC preset management
  - editable parameters for rolling counter (`start`, `step`, `min`, `max`)
  - editable parameters for CRC (`width`, `poly`, `init`, `xor_out`, reflect in/out)
  - configurable zeroed signals when CRC is calculated
- Routing engine:
  - Multi-thread RX + dispatch + monitor loop.
  - Immediate forwarding after source signal frame is received.
  - Signal-level transformation:
    - decode source frame by source port DBC
    - read source signal value
    - assign to target signal
    - optionally generate rolling counter / CRC target signals from preset algorithms
    - encode target frame by target port DBC
    - send from target port
  - CAN/CAN FD length handling (strict/truncate/pad).
  - runtime counters (RX/TX/drop/error) and rolling logs.
- Trace:
  - dedicated Trace tab with real-time RX/TX/INJECT monitor table
  - filter by port and direction
  - clear/pause/max-rows control
- Mixed vendor support:
  - different ports can use different driver brands in one config.
  - built-in registry includes `ni`, `zlg`, `python-can:*`, `sim-virtual`.
- DBC parse compatibility:
  - parser first tries strict mode
  - if strict fails with non-standard definitions (`overlapping`, `signal does not fit`), it auto-falls back to compatible mode

## Project layout

```text
can_gateway/
  core/
  drivers/
  services/
  ui/
  utils/
configs/
main.py
```

## Dependencies

- Python 3.11+
- `PySide6`
- `python-can`
- `cantools`
- optional for ZLG native support:
  - `zlgcan`
  - `zlgcan-driver`
- optional for NI-XNET support:
  - NI-XNET runtime/driver
  - Python package `nixnet`

Install:

```powershell
pip install -r requirements.txt
```

## Run

```powershell
python main.py
```

Or:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_dev.ps1
```

## Default behavior

- `configs/default_config.json` is pre-filled with 4 virtual channels:
  - `Port1` -> `SIM_CH1`
  - `Port2` -> `SIM_CH2`
  - `Port3` -> `SIM_CH3`
  - `Port4` -> `SIM_CH4`

You can start testing immediately without physical hardware.

## Typical workflow

1. In Hardware Config:
   - scan/import ports or use virtual ports
   - set unique port names
   - assign DBC per port
2. In Route Config:
   - click source and target ports in topology to create direction
   - configure source signal -> target signal mapping
   - optionally click `Edit Row` to configure advanced mapping parameters and RC/CRC preset bindings
3. In Algorithm Config:
   - create RC / CRC presets as needed
4. Start router.
5. Use Test Injection or Algorithm Sender to validate forwarding and RC/CRC behavior.

## Notes for NI / ZLG

- ZLG now uses native `zlgcan` backend integration instead of generic `python-can` arguments.
- If ZLG scan still returns empty, set environment variable `ZLGCAN_LIBPATH` to the folder containing ZLG native runtime libraries, then restart the app.
- NI uses the NI-XNET backend and requires both NI-XNET runtime and Python package `nixnet` in the same Python environment that runs `main.py`.
- Scan diagnostics will show missing runtime/module reasons when hardware cannot be discovered.
