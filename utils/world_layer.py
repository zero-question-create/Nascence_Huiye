import os, json, random
import time as time_mod
from core.virtual_clock import clock

SCENE_FILE = "data/test/scene_state.json"

# 身体状态常量
MAX_HUNGER = 100
MAX_FATIGUE = 100
HUNGER_PER_HOUR = 2.5
FATIGUE_PER_HOUR_ACTIVE = 6.0
FATIGUE_RECOVERY_PER_HOUR_REST = 8.0

# 家居坐标
HOME_ZONES = {
    "玄关": {"desc": "门口的玄关，鞋柜上放着彩叶的帆布鞋和一把钥匙", "connected": ["客厅", "街道"]},
    "客厅": {"desc": "客厅铺着暖色地毯，茶几上摆着半杯水和一本翻到一半的杂志，窗外能看到街道和路灯", "connected": ["玄关", "厨房", "阳台", "卧室"]},
    "厨房": {"desc": "开放式小厨房，灶台上有水壶和碗筷，冰箱门上贴着便签", "connected": ["客厅", "阳台"]},
    "阳台": {"desc": "窄小的阳台，晾着几件衣服，能看见楼下的便利店和远处街道的灯光", "connected": ["客厅", "厨房"]},
    "卧室": {"desc": "合租的卧室，两张床各自靠墙，书桌上摊着课本和笔记本，墙上贴着照片", "connected": ["客厅"]},
    "便利店": {"desc": "楼下的24小时便利店，货架上摆着零食和泡面，收银台后面站着值夜班的店员", "connected": ["街道"]},
    "街道": {"desc": "夜晚的街道，路灯昏黄，偶尔有行人经过，远处能看见便利店的灯光", "connected": ["便利店"]},
}


class WorldState:
    def __init__(self):
        self.location = "客厅"
        self.hunger = 20.0
        self.fatigue = 10.0
        self.weather = "晴"
        self.time_of_day = "白天"
        self.last_tick_virtual = 0.0
        self.prev_utterance = ""
        self.prev_action = ""
        self.prev_caiye = ""
        self._init_weather_and_time()

    def _init_weather_and_time(self):
        hour = (clock.now() % 86400) / 3600
        if 6 <= hour < 18:
            self.time_of_day = "白天"
            self.weather = random.choice(["晴", "多云", "晴"])
        else:
            self.time_of_day = "夜晚"
            self.weather = random.choice(["晴", "多云", "晴"])

    def get_snapshot(self):
        zone = HOME_ZONES.get(self.location, HOME_ZONES["客厅"])
        return {
            "location": self.location,
            "location_desc": zone["desc"],
            "exits": zone["connected"],
            "weather": self.weather,
            "time_of_day": self.time_of_day,
            "hunger": round(self.hunger, 1),
            "fatigue": round(self.fatigue, 1),
            "max_hunger": MAX_HUNGER,
            "max_fatigue": MAX_FATIGUE,
            "prev_utterance": self.prev_utterance,
            "prev_action": self.prev_action,
            "prev_caiye": self.prev_caiye,
        }

    def tick(self):
        now = clock.now()
        if self.last_tick_virtual == 0:
            self.last_tick_virtual = now
            return
        delta = now - self.last_tick_virtual
        self.last_tick_virtual = now
        hours = delta / 3600
        if hours <= 0:
            return
        self.hunger = min(MAX_HUNGER, self.hunger + HUNGER_PER_HOUR * hours)
        if self.location == "卧室":
            self.fatigue = max(0, self.fatigue - FATIGUE_RECOVERY_PER_HOUR_REST * hours)
        else:
            self.fatigue = min(MAX_FATIGUE, self.fatigue + FATIGUE_PER_HOUR_ACTIVE * hours)
        hour_of_day = (now % 86400) / 3600
        if 6 <= hour_of_day < 18:
            self.time_of_day = "白天"
        else:
            self.time_of_day = "夜晚"

    def apply_action(self, action_cmd):
        action_cmd = action_cmd.strip().lower()
        if action_cmd.startswith("移动"):
            for zone_name in HOME_ZONES:
                if zone_name in action_cmd:
                    current = HOME_ZONES.get(self.location, HOME_ZONES["客厅"])
                    if zone_name in current["connected"]:
                        self.location = zone_name
                        return f"辉夜移动到了{zone_name}"
            return f"无法移动到目标位置"
        if "进食" in action_cmd or "吃" in action_cmd:
            if self.hunger >= 15:
                self.hunger = max(0, self.hunger - random.uniform(15, 30))
                return "辉夜吃了一些东西"
            return "辉夜不觉得饿"
        if "休息" in action_cmd or "睡" in action_cmd or "坐" in action_cmd:
            return "辉夜开始休息"
        if "查看" in action_cmd or "环顾" in action_cmd:
            snap = self.get_snapshot()
            return f"辉夜环顾四周：{snap['location_desc']}"
        return f"辉夜{action_cmd}"


    def save_scene(self):
        os.makedirs(os.path.dirname(SCENE_FILE), exist_ok=True)
        data = {
            "location": self.location,
            "hunger": self.hunger,
            "fatigue": self.fatigue,
            "weather": self.weather,
            "time_of_day": self.time_of_day,
            "last_tick_virtual": self.last_tick_virtual,
            "prev_utterance": self.prev_utterance,
            "prev_action": self.prev_action,
            "prev_caiye": self.prev_caiye,
        }
        with open(SCENE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def load_scene(self):
        if not os.path.exists(SCENE_FILE):
            self._init_weather_and_time()
            return
        try:
            with open(SCENE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.location = data.get("location", "客厅")
            self.hunger = data.get("hunger", 20.0)
            self.fatigue = data.get("fatigue", 10.0)
            self.weather = data.get("weather", "晴")
            self.time_of_day = data.get("time_of_day", "白天")
            self.last_tick_virtual = data.get("last_tick_virtual", 0.0)
            self.prev_utterance = data.get("prev_utterance", "")
            self.prev_action = data.get("prev_action", "")
            self.prev_caiye = data.get("prev_caiye", "")
        except Exception:
            self._init_weather_and_time()


WORLD = WorldState()
