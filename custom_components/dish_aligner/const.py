"""Constants for the Dish Aligner integration."""

DOMAIN = "dish_aligner"

# Upstream integrations we depend on
OPENWRT_DOMAIN = "openwrt"       # FaserF/ha-openwrt
ESPHOME_DOMAIN = "esphome"

# Services
SERVICE_AIM = "aim"
SERVICE_AUTO_ALIGN = "auto_align"
SERVICE_SURVEY = "survey"
SERVICE_JOIN = "join"
SERVICE_ABORT = "abort"
SERVICE_HOME = "home"

# Config entry data keys - devices chosen in the flow
CONF_DISH_DEVICE = "dish_device"       # ESPHome rotator device id
CONF_RADIO_DEVICE = "radio_device"     # OpenWrt/NanoBeam device id

# Config entry data keys - entities resolved from the dish device
CONF_ANGLE_ENTITY = "angle_entity"
CONF_TARGET_ENTITY = "target_entity"
CONF_MOVING_ENTITY = "moving_entity"
CONF_HOME_BUTTON = "home_button"

# Entities resolved from the radio device / enabled by setup
CONF_SIGNAL_ENTITY = "signal_entity"
CONF_QUALITY_ENTITY = "quality_entity"
CONF_BITRATE_ENTITY = "bitrate_entity"
CONF_NOISE_ENTITY = "noise_entity"

# Entities auto-enabled during setup that need a config-entry reload
# before they produce data
CONF_PENDING_RELOADS = "pending_reloads"

# Options
CONF_WIFI_INTERFACE = "wifi_interface"
CONF_AUTO_HOME = "auto_home"             # re-home before each survey
CONF_VERIFY_SPEEDTEST = "verify_speedtest"  # entity run after auto-align
CONF_SWEEP_DEGREES = "sweep_degrees"
CONF_STEP_DEGREES = "step_degrees"
CONF_DWELL_SECONDS = "dwell_seconds"
CONF_SURVEY_STEP_DEGREES = "survey_step_degrees"

DEFAULT_WIFI_INTERFACE = "phy0-sta0"
DEFAULT_SWEEP_DEGREES = 30
DEFAULT_STEP_DEGREES = 5
DEFAULT_DWELL_SECONDS = 3
DEFAULT_SURVEY_STEP_DEGREES = 15
DEFAULT_AUTO_HOME = True
SIGNAL_SAMPLES = 3

# Entity signatures we auto-resolve on each device:
# (domain, substring to match in entity_id/original_name, config key, required?)
DISH_ENTITY_SIGNATURES = [
    ("sensor", "current_angle", CONF_ANGLE_ENTITY, True),
    ("number", "azimuth_target", CONF_TARGET_ENTITY, True),
    ("binary_sensor", "azimuth_moving", CONF_MOVING_ENTITY, True),
    ("button", "home_azimuth", CONF_HOME_BUTTON, True),
]

RADIO_ENTITY_SIGNATURES = [
    ("sensor", "signal", CONF_SIGNAL_ENTITY, True),
    ("sensor", "signal_quality", CONF_QUALITY_ENTITY, False),
    ("sensor", "bitrate", CONF_BITRATE_ENTITY, False),
    ("sensor", "noise", CONF_NOISE_ENTITY, False),
]
