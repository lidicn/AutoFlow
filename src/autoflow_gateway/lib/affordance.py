# -*- coding: utf-8 -*-
"""
affordance.py — 常见 HA 域的「状态契约 + 服务词汇」静态表（token-free）。

为什么需要它：device_catalog 只存了实体某一刻的快照 state，没有「它可能处于哪些状态、
能调哪些服务、各服务的关键参数」。agent 写 flow 时只能猜 → 猜错触发 HA 422 / 分支遗漏。
本表把最常见的域契约硬编码，让 discover 直接附加，写 flow 立刻有依据。

注意：
- 全域隐含状态 `unavailable` / `unknown`：任何实体都可能离线（state=unavailable）或未知，
  写 flow 必须分支处理，不可当成 on/off 的等价物。
- 本表是「通用契约基线」。实体特有的枚举（如 select 的 options、climate 的 hvac_modes）
  仍在实体 attributes 里——B5a 已把 attributes 写进 catalog，get_detail 也可取实时值。
  两者互补：本表给「该域共有什么」，attributes 给「这个实体具体有哪些」。
"""

# 任何实体都可能出现的状态（与域无关）
GLOBAL_STATES = ["unavailable", "unknown"]

# 域 → 契约。services 的 value 是参数说明（写 flow 时参考），对外只暴露 service 名。
DOMAIN_AFFORDANCE = {
    "switch": {
        "states": ["on", "off", "unavailable", "unknown"],
        "services": {
            "turn_on": {}, "turn_off": {}, "toggle": {},
        },
        "note": "布尔开关。unavailable=离线，不可等同 off。",
    },
    "light": {
        "states": ["on", "off", "unavailable", "unknown"],
        "services": {
            "turn_on": {"brightness_pct": "0-100", "brightness": "0-255",
                        "rgb_color": "[r,g,b]", "color_temp": "开尔文(K)",
                        "color_name": "red/blue/...", "effect": "看实体 effect_list",
                        "transition": "秒"},
            "turn_off": {}, "toggle": {},
        },
        "note": "支持哪些调光/变色参数取决于实体 capabilities(supported_color_modes)；"
                "传了实体不支持的参数会 422。unavailable=离线。",
    },
    "climate": {
        "states": ["off", "heat", "cool", "auto", "dry", "fan_only", "unavailable", "unknown"],
        "services": {
            "turn_on": {}, "turn_off": {},
            "set_temperature": {"temperature": "°C", "target_temp_high": "°C", "target_temp_low": "°C"},
            "set_hvac_mode": {"hvac_mode": "取值见 states(去掉 unavailable/unknown)"},
            "set_fan_mode": {"fan_mode": "auto/low/high/..."},
            "set_preset_mode": {"preset_mode": "见实体 preset_modes"},
            "set_humidity": {"humidity": "0-100"},
        },
        "note": "支持的 hvac_modes/fan_modes/preset_modes 在 attributes；调用未列出的模式会 422。",
    },
    "fan": {
        "states": ["on", "off", "unavailable", "unknown"],
        "services": {
            "turn_on": {"percentage": "0-100", "preset_mode": ""},
            "turn_off": {}, "toggle": {},
            "set_percentage": {"percentage": "0-100"},
            "set_preset_mode": {"preset_mode": ""},
            "oscillate": {"oscillating": "bool"},
        },
        "note": "是否支持百分比/摆风看 capabilities。",
    },
    "cover": {
        "states": ["open", "closed", "opening", "closing", "stopped", "unavailable", "unknown"],
        "services": {
            "open_cover": {}, "close_cover": {}, "stop_cover": {},
            "set_cover_position": {"position": "0-100(开合度)"},
        },
        "note": "部分卷帘支持 set_cover_tilt。",
    },
    "lock": {
        "states": ["locked", "unlocked", "jammed", "unavailable", "unknown"],
        "services": {"lock": {}, "unlock": {}, "open": {}},
        "note": "开锁属高危域，需人工确认。jammed=卡死。",
    },
    "select": {
        "states": ["<option1>", "<option2>", "...", "unavailable", "unknown"],
        "services": {
            "select_option": {"option": "必须 ∈ attributes.options，否则 422"},
        },
        "note": "选项枚举在 attributes.options。调用不存在的选项会 422——写 flow 最常踩的坑。",
    },
    "media_player": {
        "states": ["playing", "paused", "idle", "off", "standby", "buffering", "unavailable", "unknown"],
        "services": {
            "play_media": {"media_content_id": "", "media_content_type": "music/video"},
            "media_play": {}, "media_pause": {}, "media_stop": {},
            "volume_set": {"volume_level": "0-1"}, "volume_mute": {"is_volume_muted": "bool"},
        },
        "note": "部分实体支持 select_source / shuffle_set 等。",
    },
    "vacuum": {
        "states": ["docked", "cleaning", "paused", "idle", "returning", "error", "unavailable", "unknown"],
        "services": {"start": {}, "pause": {}, "stop": {}, "return_to_base": {},
                     "locate": {}, "set_fan_speed": {"fan_speed": ""}},
    },
    "humidifier": {
        "states": ["on", "off", "unavailable", "unknown"],
        "services": {"turn_on": {}, "turn_off": {}, "toggle": {},
                     "set_humidity": {"humidity": "0-100"}, "set_mode": {"mode": ""}},
    },
    "water_heater": {
        "states": ["electric", "performance", "eco", "heat_pump", "off", "unavailable", "unknown"],
        "services": {"set_temperature": {"temperature": "°C"},
                     "set_operation_mode": {"operation_mode": ""}},
    },
    "number": {
        "states": ["<数值>", "unavailable", "unknown"],
        "services": {"set_value": {"value": "必须在 min/max 之间，否则 422"}},
        "note": "范围在 attributes.min/max。",
    },
    "input_boolean": {
        "states": ["on", "off"],
        "services": {"turn_on": {}, "turn_off": {}, "toggle": {}},
    },
    "scene": {
        "states": ["<激活快照>"],
        "services": {"turn_on": {"entities?": "可选覆盖"}},
        "note": "一次性激活快照，无持续状态；调用 turn_on 触发。",
    },
    "script": {
        "states": ["on", "off"],
        "services": {"turn_on": {"variables?": "可选"}, "turn_off": {}, "toggle": {}},
        "note": "运行中 state=on；可传 variables。",
    },
    "automation": {
        "states": ["on", "off"],
        "services": {"turn_on": {}, "turn_off": {}, "toggle": {}, "trigger": {}},
        "note": "仅启停/触发，不改逻辑。",
    },
    "sensor": {
        "states": ["<测量值>", "unavailable", "unknown"],
        "services": {},
        "note": "只读。单位在 attributes.unit_of_measurement。unavailable=离线/无读数。",
    },
    "binary_sensor": {
        "states": ["on", "off", "unavailable", "unknown"],
        "services": {},
        "note": "只读。on=触发(有人/开窗/移动)，off=未触发。unavailable=离线。",
    },
}


def affordance_for(domain: str) -> dict:
    """返回某域的契约（含全域隐含状态），无则空 dict。"""
    aff = DOMAIN_AFFORDANCE.get(domain)
    if not aff:
        return {}
    # 把全域隐含状态并入 states（去重）
    states = list(aff.get("states", []))
    for g in GLOBAL_STATES:
        if g not in states:
            states.append(g)
    return {
        "states": states,
        "services": list(aff.get("services", {}).keys()),
        "note": aff.get("note", ""),
    }
