from cyberdeck_pi.uart.base_reader import ToadletReader


class BtReader(ToadletReader):
    toadlet = "bt"
    valid_modes = frozenset(
        {
            "SLEEP",
            "BLE_SCAN",
            "BLE_PROXIMITY",
            "SKIMMER_DETECT",
            "BT_CLASSIC_SCAN",
            "DWELL_LOG",
        }
    )
    default_mode = "BLE_SCAN"
