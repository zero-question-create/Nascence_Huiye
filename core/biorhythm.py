# core/biorhythm.py
"""
辉夜的生物钟（自塑造节律）。

设计原则
--------
1. 不硬编码任何"睡觉时刻"：入睡/醒来都是睡眠压力越过阈值后**涌现**的结果。
2. 不预设节律周期：没有 24 小时振荡器，也没有固定周期常数。
   节律长度由睡眠压力动力学与**实际经历**共同决定——被提前叫醒时压力仍高，
   于是下次会更早犯困；睡到自然醒则作息规律。周期因此随经历变化。
3. 只有动力学"速率"是人为设定的量纲（任何模型都无法消除），
   但这些速率是"压力多久积满/排空"，不是某个钟点。

模型（睡眠稳态）
----------------
- 睡眠压力 S ∈ [0,1]：清醒时按 T_WAKE 向 1 上升，睡眠时按 T_SLEEP 向 0 回落。
  精力 energy = 1 - S。
- 判定：清醒且 S >= ONSET_THRESHOLD 入睡；睡眠且 S <= WAKE_THRESHOLD 醒来。
- 没有"睡眠债"等额外状态：被叫醒后 S 依然偏高，自然导致下次更早犯困，
  "经历塑造节律"由这一条动力学直接涌现，无需附加机制（避免正反馈螺旋）。

时间基准：真实时间（time.time()）。倍速已停用，离线时间在 load() 时按真实流逝补算。
"""

import json
import math
import os
import time
import threading

from utils.monitor import append_log
from config.api_config import config

STATE_FILE = "data/test/biorhythm.json"

# ========== 动力学速率（量纲，非钟点）==========
# 清醒时睡眠压力上升的指数时间常数（秒）→ 决定"醒着多久会困"
# 取 18.40h 与 T_SLEEP=9.1h 及当前阈值组合，使节律周期精确=24h（零漂移）
T_WAKE = 18.40 * 3600
# 睡眠时睡眠压力回落的指数时间常数（秒）→ 决定"睡多久会醒"
T_SLEEP = 9.1 * 3600
# 入睡阈值（0.62 → 精力降到 0.38 即入睡；配合 18.40h 的清醒时间常数，
# 精力从 0.40 掉到 0.38 约需 1 小时，困倦尾巴不再拖沓）
ONSET_THRESHOLD = 0.62
# 醒来阈值（0.20 → 精力回到 0.80 才自然醒，确保醒来时精神充沛）
WAKE_THRESHOLD = 0.20
# 被用户点名唤醒后的清醒锁定时间（秒）：期间不会立刻重新睡去，以便完成回应
AWAKE_LOCK = 20 * 60
# 单次 tick 最大推进步长（秒），用于离线补算时的数值稳定
MAX_STEP = 300.0


def _load_params():
    """从全局 config 读取可调参数（缺键时回落到本模块默认值）。"""
    global T_WAKE, T_SLEEP, ONSET_THRESHOLD, WAKE_THRESHOLD
    T_WAKE = float(config.get("biorhythm_wake_seconds", T_WAKE))
    T_SLEEP = float(config.get("biorhythm_sleep_seconds", T_SLEEP))
    ONSET_THRESHOLD = float(config.get("biorhythm_onset_threshold", ONSET_THRESHOLD))
    WAKE_THRESHOLD = float(config.get("biorhythm_wake_threshold", WAKE_THRESHOLD))


class Biorhythm:
    def __init__(self):
        self._lock = threading.RLock()
        self.s = 0.15               # 睡眠压力
        self.state = "awake"        # "awake" | "asleep"
        self.last_tick = time.time()
        # 记录
        self.onset_count = 0
        self.wake_count = 0
        self.sleep_started_at = None
        self.woke_by_user = False
        self.last_onset = None      # 上次入睡真实时刻（供观测实际节律）
        self.last_wake = None
        self.awake_until = 0.0      # 清醒锁定到此时刻（被点名唤醒后）
        self.last_feeling_time = 0.0 # 上次注入身体感受的时刻（防每轮轰炸）
        self._replaying = False     # 离线补算中（跳过重量级睡眠维护）
        _load_params()
        self.load()

    # ---------- 核心推进 ----------
    def tick(self, now: float = None):
        """按真实时间推进状态。可多次调用，等价于一次长推进。"""
        if now is None:
            now = time.time()
        with self._lock:
            dt = now - self.last_tick
            if dt <= 0:
                self.last_tick = now
                return
            # 拆分为小步，保证数值稳定与阈值判定顺序正确
            remaining = dt
            cursor = self.last_tick
            while remaining > 0:
                step = min(remaining, MAX_STEP)
                cursor += step
                self._advance(step, cursor)
                remaining -= step
            self.last_tick = now

    def _advance(self, dt: float, now: float):
        """推进 dt 秒（dt <= MAX_STEP），期间可能发生入睡/醒来转换。"""
        if self.state == "awake":
            self.s = 1.0 - (1.0 - self.s) * math.exp(-dt / T_WAKE)
            # 被点名唤醒后的清醒锁定期内不重新入睡，以便完成回应
            if now >= self.awake_until and self.s >= ONSET_THRESHOLD:
                self._sleep(now)
        else:
            self.s = self.s * math.exp(-dt / T_SLEEP)
            if self.s <= WAKE_THRESHOLD:
                self._wake_internal(now)

    # ---------- 状态转换 ----------
    def _sleep(self, now: float = None):
        if now is None:
            now = time.time()
        if self.last_onset is not None:
            interval = now - self.last_onset
            append_log(f"[生物钟] 入睡（距上次入睡 {interval/3600:.2f}h，压力={self.s:.2f}）")
        else:
            append_log(f"[生物钟] 入睡（压力={self.s:.2f}）")
        self.last_onset = now
        self.onset_count += 1
        self.state = "asleep"
        self.sleep_started_at = now
        self.woke_by_user = False
        # 沿用现有睡眠全量维护（离线补算时不执行，避免启动开销与副作用）
        if not self._replaying:
            try:
                from utils.persistence import sleep_cleanup
                sleep_cleanup()
            except Exception as e:
                append_log(f"[生物钟] 睡眠维护失败: {e}")

    def _wake_internal(self, now: float = None):
        if now is None:
            now = time.time()
        dur = (now - self.sleep_started_at) if self.sleep_started_at else 0
        self.state = "awake"
        self.wake_count += 1
        self.sleep_started_at = None
        self.last_wake = now
        append_log(f"[生物钟] 自然醒来（睡了 {dur/3600:.2f}h，压力={self.s:.2f}）")

    def wake(self, reason: str = "user"):
        """外部强制唤醒（如被 @）。被叫醒时压力仍高，之后会更快再次犯困。"""
        with self._lock:
            if self.state != "asleep":
                return
            now = time.time()
            dur = (now - self.sleep_started_at) if self.sleep_started_at else 0
            self.state = "awake"
            self.wake_count += 1
            self.sleep_started_at = None
            self.last_wake = now
            self.woke_by_user = True
            # 清醒锁定：给足时间完成这次回应，不会话说到一半又睡回去
            self.awake_until = now + AWAKE_LOCK
            append_log(f"[生物钟] 被唤醒（{reason}，仅睡 {dur/3600:.2f}h，压力仍 {self.s:.2f}）")

    def force_sleep(self, reason: str = "manual"):
        """外部强制入睡（调试/面板用）。"""
        with self._lock:
            if self.state == "awake":
                self._sleep()

    # ---------- 对外属性 ----------
    @property
    def energy(self) -> float:
        """精力 ∈ [0,1]，睡眠压力越低越有精神。"""
        return max(0.0, min(1.0, 1.0 - self.s))

    def is_asleep(self) -> bool:
        return self.state == "asleep"

    def depth_factor(self) -> float:
        """精力 → 思考深度系数 ∈ [0.5, 1.0]。低精力时想得浅。"""
        return 0.5 + 0.5 * self.energy

    def feeling_text(self, min_interval: float = 900.0) -> str | None:
        """按精力分档生成第一人称身体感受（纯字符串，不调 LLM）。

        - 正常起床且精力充沛（energy >= 0.4）：返回 None，不产生多余记忆。
        - 刚被叫醒（用户 @ 唤醒，处于清醒锁定窗口期）：体现被吵醒的迷糊状态。
        - 仅在精力 < 0.4 时附加感受，且加 15 分钟 (900s) 冷却，绝不每轮重复洗脑。
        """
        now = time.time()
        # 1. 处于被强行唤醒的锁定保护期
        if self.woke_by_user and now < self.awake_until:
            if now - self.last_feeling_time < min_interval:
                return None
            self.last_feeling_time = now
            return "[现在] 我刚被吵醒，脑子还发懵，有点迷糊"

        e = self.energy
        # 2. 精力充沛正常状态：不附加任何感受记忆
        if e >= 0.4:
            return None

        # 3. 冷却检查：疲倦感受 15 分钟内最多浮现一次，避免认知循环每 3 秒自我催眠
        if now - self.last_feeling_time < min_interval:
            return None

        self.last_feeling_time = now
        # 4. 疲乏与濒临入睡状态（仅 < 0.4 时附加）
        if e >= 0.25:
            return "[现在] 我挺累的，眼皮开始发沉，想找个地方歇一会儿"
        return "[现在] 我快撑不住了，脑子迷迷糊糊的，只想睡觉"

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "energy": round(self.energy, 3),
            "sleep_pressure": round(self.s, 3),
            "onset_count": self.onset_count,
            "wake_count": self.wake_count,
        }

    # ---------- 持久化 ----------
    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            data = {
                "s": self.s,
                "state": self.state,
                "last_tick": self.last_tick,
                "last_onset": self.last_onset,
                "last_wake": self.last_wake,
                "onset_count": self.onset_count,
                "wake_count": self.wake_count,
                "sleep_started_at": self.sleep_started_at,
                "awake_until": self.awake_until,
            }
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)

    def load(self):
        with self._lock:
            if not os.path.exists(STATE_FILE):
                self.last_tick = time.time()
                return
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.s = float(data.get("s", self.s))
                self.state = data.get("state", "awake")
                self.last_onset = data.get("last_onset", None)
                self.last_wake = data.get("last_wake", None)
                self.onset_count = int(data.get("onset_count", 0))
                self.wake_count = int(data.get("wake_count", 0))
                self.sleep_started_at = data.get("sleep_started_at", None)
                self.awake_until = float(data.get("awake_until", 0.0))
                saved_at = float(data.get("last_tick", time.time()))
                # 离线时长按真实流逝补算（重启后生物钟继续走）；
                # 补算期间跳过睡眠维护，避免启动时执行重量级全量清理。
                self.last_tick = saved_at
                self._replaying = True
                try:
                    self.tick(time.time())
                finally:
                    self._replaying = False
                append_log(f"[生物钟] 已加载：{self.state}，精力={self.energy:.2f}")
            except Exception as e:
                append_log(f"[生物钟] 加载失败，使用默认值: {e}")
                self.last_tick = time.time()
                self._replaying = False


# 全局实例
BIORHYTHM = Biorhythm()
