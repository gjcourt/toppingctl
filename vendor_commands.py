# Vendor command map for the Topping DX5 II.
#
# PROVENANCE: extracted from the constant table in Topping's own web
# application bundle (home.toppingaudio.com, web v1.10.0). This is vendor data,
# NOT derived by observation -- it is why the README no longer claims a
# clean-room origin. Names are Topping's, with the dx5ii prefix stripped.
#
# A cmd is a 16-bit (register << 8) | sub-index. Every entry here is on the
# device-control register 0x71.

COMMANDS = {
    0x7101: 'PowerOn',
    0x7102: 'Volume',
    0x7103: 'Mute',
    0x7104: 'InputType',
    0x7105: 'OutputType',
    0x7106: 'AudioBluetooth',
    0x7107: 'BluetoothAptx',
    0x7108: 'Remote',
    0x7109: 'UacMode',
    0x710a: 'ScreenBrightness',
    0x710b: 'FactoryReset',
    0x710c: 'GetSettings',
    0x710d: 'PcmFilter',
    0x710e: 'LineMode',
    0x710f: 'GetSampling',
    0x7110: 'DisableController',
    0x7111: 'CallC1',
    0x7112: 'CallC2',
    0x7113: 'HomePage',
    0x7114: 'Theme',
    0x7115: 'PowerTrigger',
    0x7116: 'Balance',
    0x7117: 'HeadphoneGain',
    0x7118: 'MultifunctionKey',
    0x7119: 'Language',
    0x711a: 'InputMode',
    0x711b: 'InputOption',
    0x711c: 'OutputOption',
    0x711d: 'Polarity',
    0x711e: 'VolumeStep',
    0x711f: 'VuMeterLevel',
    0x7120: 'VuMode',
    0x7121: 'VolumeMemory',
    0x7122: 'PeqMemory',
    0x7123: 'RemoteAKeyFunction',
    0x7124: 'RemoteBKeyFunction',
    0x7125: 'ExecuteRemoteAKey',
    0x7126: 'ExecuteRemoteBKey',
    0x7127: 'DeviceName',
    0x7128: 'SpdifMode',
    0x7129: 'CrossfeedMemory',
    0x712a: 'DcDetectSensitivity',
    0x712b: 'CrossfeedType',
    0x712c: 'CrossfeedConvolutionOption',
    0x712d: 'CrossfeedSimpleOption',
    0x712e: 'FirmwareUpdate',
    0x7130: 'VuData',
    0x7131: 'FftData',
    0x7132: 'PeqState',
    0x7133: 'UsbSerial',
    0x7134: 'Heartbeat',
    0x7135: 'SaveC1',
    0x7136: 'SaveC2',
    0x7137: 'PeqPreviewState',
    0x7138: 'BluetoothState',
    0x7139: 'BluetoothClearPairing',
    0x713a: 'DimScreenTimeout',
    0x713b: 'DimScreenType',
    0x713c: 'TriggerOut',
    0x713d: 'MultifunctionDoubleKey',
    0x713e: 'RemoteMuteMode',
    0x713f: 'PeqOptionMask',
    0x7140: 'PcmOptionMask',
    0x7141: 'CrossfeedOptionMask',
}

# Byte 2 of every frame. We only ever sent writeNack; readAck is where the
# device's answer to a read arrives.
PROTOCOL = {0x10: 'readNack', 0x11: 'readAck', 0x20: 'writeNack', 0x21: 'writeAck'}

# GetSettings (0x710c) returns a numbered array of 32-bit records; byte 4 of
# each frame is the index. Field order below is Topping's own parser.
# Indices 1..8 are the device name as little-endian ASCII, 4 chars per record.
# Indices 48..51 exist only on firmware >= 2.40.
SETTINGS_FIELDS = {
    0: "powered", 9: "volume", 10: "muted", 11: "inputType", 12: "outputType",
    13: "highGain", 14: "homePage", 15: "theme", 16: "powerTrigger",
    17: "balance", 18: "pcmFilter", 19: "lineMode", 20: "bluetoothMode",
    21: "bluetoothAptx", 22: "remoteEnabled", 23: "multifunctionKey",
    24: "uacMode", 25: "brightness", 26: "language", 27: "sampleRate",
    28: "inputMode", 29: "spdifMode", 30: "inputOptionMask",
    31: "outputOptionMask", 32: "volumeStep", 33: "polarity",
    34: "volumeMemory", 35: "peqMemory", 36: "crossfeedMemory",
    37: "dcDetectSensitivity", 38: "classicVuLevel", 39: "vuBarMode",
    40: "remoteAKeyFunction", 41: "remoteBKeyFunction", 42: "crossfeedType",
    43: "crossfeedConvolutionOptionMask", 44: "crossfeedSimpleOptionMask",
    48: "autoScreenTimeout", 49: "dimScreenType", 50: "triggerOut",
    51: "multifunctionDoubleKey",
}

# Enum tables, from the vendor bundle's DX5II_*_ID_TO_DEVICE maps.
# device value -> vendor's own identifier.
ENUMS = {
    'auto_screen_timeout': {0: 'off', 10: '10s', 30: '30s', 60: '60s'},
    'bluetooth_mode': {0: 'off', 1: 'always', 2: 'input_only'},
    'brightness': {0: 'low', 1: 'medium', 2: 'high'},
    'classic_vu_level': {0: 'plus4dbu', 1: 'plus10dbu'},
    'crossfeed_convolution_option': {0: 'c1', 1: 'c2'},
    'crossfeed_simple_option': {0: 's1', 1: 's2', 2: 's3', 3: 's4'},
    'crossfeed_type': {0: 'convolution', 1: 'simple', 2: 'off'},
    'dim_screen_type': {0: 'input_only', 1: 'all_black'},
    'home_page': {0: 'normal', 1: 'vu', 2: 'fft'},
    'input': {0: 'usb', 1: 'fiber', 2: 'coax', 3: 'bt'},
    'input_mode': {0: 'auto', 1: 'manual'},
    'language': {0: 'en', 1: 'zh', 2: 'ja', 3: 'zh_tw'},
    'line_mode': {0: 'preamp', 1: 'dac'},
    'memory_mode': {0: 'input', 1: 'output', 2: 'disabled'},
    'multifunction_key': {
        0: 'input_select', 1: 'output_select', 2: 'home_select', 3: 'brightness', 4:
        'screen_off', 5: 'mute', 6: 'peq_select', 7: 'power_trigger', 8: 'pcm_filter', 9:
        'headphone_gain', 10: 'peq_toggle', 11: 'crossfeed_type', 12: 'crossfeed_config', 13:
        'play_pause', 14: 'previous_track', 15: 'next_track'
    },
    'output': {
        0: 'all', 1: 'hp_all', 2: 'line_all', 3: 'hp_single', 4: 'hp_balanced', 5:
        'line_single', 6: 'line_balanced'
    },
    'pcm_filter': {0: 'f1', 1: 'f2', 2: 'f3', 3: 'f4', 4: 'f5', 5: 'f6', 6: 'f7', 7: 'f8'},
    'polarity': {0: 'normal', 1: 'reverse'},
    'power_trigger': {0: 'signal', 1: 'trigger12v', 2: 'off'},
    'remote_mute_mode': {0: 'device_mute', 1: 'input_adaptive'},
    'spdif_mode': {0: 'mode1', 1: 'mode2'},
    'theme': {
        0: 'aurora', 1: 'orange', 2: 'peru', 3: 'pea_green', 4: 'dark_khaki', 5: 'rosy_brown',
        6: 'blue', 7: 'purple_magic', 8: 'white'
    },
    'trigger_out': {0: 'follow_power', 1: 'follow_lineout'},
    'uac_mode': {0: 'uac1', 1: 'uac2'},
    'volume_step': {0: 'half_db', 1: 'one_db'},
    'vu_bar_mode': {0: 'all_on', 1: 'normal', 2: 'fft', 3: 'all_off'},
}

# Which settings field reads through which enum table.
FIELD_ENUM = {
    "inputType": "input", "outputType": "output", "pcmFilter": "pcm_filter",
    "theme": "theme", "crossfeedType": "crossfeed_type", "homePage": "home_page",
    "language": "language", "lineMode": "line_mode", "inputMode": "input_mode",
    "uacMode": "uac_mode", "brightness": "brightness", "polarity": "polarity",
    "volumeStep": "volume_step", "spdifMode": "spdif_mode",
    "bluetoothMode": "bluetooth_mode", "powerTrigger": "power_trigger",
    "classicVuLevel": "classic_vu_level", "vuBarMode": "vu_bar_mode",
    "multifunctionKey": "multifunction_key",
    "remoteAKeyFunction": "multifunction_key",
    "remoteBKeyFunction": "multifunction_key",
    "crossfeedConvolutionOptionMask": "crossfeed_convolution_option",
    "crossfeedSimpleOptionMask": "crossfeed_simple_option",
    "dimScreenType": "dim_screen_type", "triggerOut": "trigger_out",
    "autoScreenTimeout": "auto_screen_timeout",
}
