# core/biorhythm.py
"""
辉夜的生物钟（自塑造节律）。

设计原则
--------
1. 不硬编码任何"睡觉时刻"：入睡/醒来都是动力学结果，而非某个钟点。
2. 不预设节律周期：周期由睡眠压力动力学与**实际经历**共同决定。
3. 只有动力学"速率"是人为设定的量纲（任何模型都无法消除）。
4. 作息惯性是**学出来的**，不是配置出来的：从她自己"什么时候真的睡着了"
   的覆盖时段累积成直方图，因此"到点就困"是她自己经历塑造的，不是外部钟表。

模型（睡眠稳态 + 学得的作息节律）
--------------------------------
- 睡眠压力 S ∈ [0,1]：清醒时按 T_WAKE 向 1 上升，睡眠时按 T_SLEEP 向 0 回落。
  精力 energy = 1 - S。
- 睡意 sleep_drive = S + RHYTHM_WEIGHT × circadian(当前时刻)。
  circadian ∈ [0,1] 由作息直方图得出：她历史上在此时段入睡的比例。
- 判定：
    纯疲劳：S >= ONSET_THRESHOLD            → 入睡（不需要安静，和以前一致）
    作息助力：sleep_drive >= ONSET_THRESHOLD → 且需安静 IDLE_TO_SLEEP 秒才入睡
  数据不足时 circadian = 0，公式退化为原式，行为与此前完全一致。
- 醒来：sleep_drive <= WAKE_THRESHOLD。由作息助攻而入睡的那一觉，在作息窗口内
  不易醒（睡满整段），但设了最长时长与深度下限作安全阀，避免长睡不醒。

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

# ========== 作息惯性（学得的昼夜节律）==========
# 作息窗口内的最大睡意抬升量：峰值时 S>=0.37 即可入睡（原为 0.62）
RHYTHM_WEIGHT = 0.25
# 作息助攻入睡所需的安静时长（秒）：群里没人说话这么久才容易睡着
IDLE_TO_SLEEP = 300.0
# 数据不足多少晚时不启用作息（避免刚上线就改变行为）
RHYTHM_MIN_NIGHTS = 5
# 达到多少晚时作息强度完全生效（此前按比例渐强）
RHYTHM_FULL_NIGHTS = 10
# 睡眠期间按此间隔采样"此刻处于睡眠"，用于累积作息直方图
RHYTHM_SAMPLE_INTERVAL = 900.0
# 每次入睡前直方图的衰减系数：让作息能跟随生活变化
RHYTHM_DECAY = 0.95
# 作息窗口内维持睡眠所需的最低节律强度（低于此值即视为窗口结束，可以醒）
RHYTHM_WAKE_HOLD = 0.5
# 作息助攻那一觉的最长时长（秒）：安全阀，防止长睡不醒。
# 说明：不需要额外的"深度下限"——醒来判定第一条就是 s <= WAKE_THRESHOLD，
# 压力低到一定程度必然醒，再设一个更低的深度阈值是永不触发的死代码。
MAX_RHYTHM_SLEEP = 14 * 3600


def _load_params():
    """从全局 config 读取可调参数（缺键时回落到本模块默认值）。"""
    global T_WAKE, T_SLEEP, ONSET_THRESHOLD, WAKE_THRESHOLD
    global RHYTHM_WEIGHT, IDLE_TO_SLEEP, RHYTHM_MIN_NIGHTS, RHYTHM_FULL_NIGHTS
    T_WAKE = float(config.get("biorhythm_wake_seconds", T_WAKE))
    T_SLEEP = float(config.get("biorhythm_sleep_seconds", T_SLEEP))
    ONSET_THRESHOLD = float(config.get("biorhythm_onset_threshold", ONSET_THRESHOLD))
    WAKE_THRESHOLD = float(config.get("biorhythm_wake_threshold", WAKE_THRESHOLD))
    RHYTHM_WEIGHT = float(config.get("biorhythm_rhythm_weight", RHYTHM_WEIGHT))
    IDLE_TO_SLEEP = float(config.get("biorhythm_idle_to_sleep", IDLE_TO_SLEEP))
    RHYTHM_MIN_NIGHTS = int(config.get("biorhythm_rhythm_min_nights", RHYTHM_MIN_NIGHTS))
    RHYTHM_FULL_NIGHTS = int(config.get("biorhythm_rhythm_full_nights", RHYTHM_FULL_NIGHTS))


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
        # ---- 作息惯性（学得的昼夜节律）----
        self.rhythm_hist = [0.0] * 24   # 24 小时直方图：她历史上在这些时段睡着的累积量
        self.rhythm_nights = 0          # 已积累的睡眠次数（用于置信度渐变）
        self.rhythm_total = 0.0         # 直方图累计总量（用于归一化，抗取整误差）
        self._last_rhythm_sample = None # 上次作息采样时刻（睡眠期间按间隔采样）
        self._by_rhythm = False         # 本次睡眠是否由作息助攻而入（决定醒来规则）
        self._late_sleep_guard = False  # 作息入睡但节律已弱时，交回自然醒判定
        self.last_guard_wake = False    # 上一次醒来是否由安全阀触发（供观测）
        self.last_activity = time.time() # 最近一次外部活动（群消息）时刻，用于安静检测
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
            self._maybe_sleep(now)
        else:
            self.s = self.s * math.exp(-dt / T_SLEEP)
            self._sample_rhythm(now)
            self._maybe_wake(now)

    # ---------- 作息惯性 ----------
    def circadian(self, ts: float = None) -> float:
        """当前时刻的作息强度 ∈ [0,1]：她历史上在这个时段睡着的比例。

        数据不足时返回 0，公式退化为纯稳态，行为与加入本机制前一致。
        """
        if self.rhythm_nights < RHYTHM_MIN_NIGHTS or self.rhythm_total <= 0:
            return 0.0
        if ts is None:
            ts = time.time()
        hour = int((ts % 86400) / 3600) % 24
        peak = max(self.rhythm_hist)
        if peak <= 0:
            return 0.0
        return max(0.0, min(1.0, self.rhythm_hist[hour] / peak))

    def rhythm_confidence(self) -> float:
        """作息可信度 ∈ [0,1]：数据越多越强，攒够 RHYTHM_FULL_NIGHTS 晚后完全生效。"""
        if self.rhythm_nights < RHYTHM_MIN_NIGHTS:
            return 0.0
        span = max(1, RHYTHM_FULL_NIGHTS - RHYTHM_MIN_NIGHTS)
        return max(0.0, min(1.0, (self.rhythm_nights - RHYTHM_MIN_NIGHTS) / span))

    def sleep_drive(self, ts: float = None) -> float:
        """睡前总驱力 = 睡眠压力 + 作息助力。纯诊断用，判定走 _maybe_sleep。"""
        return self.s + RHYTHM_WEIGHT * self.circadian(ts) * self.rhythm_confidence()

    def rhythm_hours(self, threshold: float = 0.5) -> list:
        """返回节律强度达到阈值的时段（小时列表），供观测"她学到了什么"。"""
        if self.rhythm_nights < RHYTHM_MIN_NIGHTS or self.rhythm_total <= 0:
            return []
        peak = max(self.rhythm_hist)
        if peak <= 0:
            return []
        return [h for h in range(24) if self.rhythm_hist[h] / peak >= threshold]

    def _rhythm_boost(self, ts: float) -> float:
        return RHYTHM_WEIGHT * self.circadian(ts) * self.rhythm_confidence()

    def _maybe_sleep(self, now: float):
        """入睡判定。

        两条路径都要求"安静"：群里正聊得热闹时她不会自顾自睡过去，
        既保证在线参与，也让学到的作息反映"环境安静 + 她困了"的时段
        （实测若允许疲劳直睡，会把下午被逼睡的时间学成作息）。
        """
        # 被点名唤醒后的锁定期内不重新入睡，以便完成回应
        if now < self.awake_until:
            return
        if now - self.last_activity < IDLE_TO_SLEEP:
            return
        # 1) 纯疲劳入睡：压力已越过阈值（不需要作息助攻）
        if self.s >= ONSET_THRESHOLD:
            self._sleep(now, by_rhythm=False)
            return
        # 2) 作息助攻入睡：睡意被节律抬到阈值
        if self._rhythm_boost(now) <= 0:
            return
        if self.sleep_drive(now) >= ONSET_THRESHOLD:
            self._sleep(now, by_rhythm=True)

    def _maybe_wake(self, now: float):
        """醒来判定。由作息助攻入睡的那一觉，在窗口内不易醒。"""
        if self.s <= WAKE_THRESHOLD:
            self._wake_internal(now)
            return
        if not self._by_rhythm:
            return
        # 安全阀：睡太久说明节律保护该结束了。
        # 这里不只是打标记——必须真的放行，否则会一直睡下去。
        dur = (now - self.sleep_started_at) if self.sleep_started_at else 0
        if dur >= MAX_RHYTHM_SLEEP:
            self._late_sleep_guard = True
            self._wake_internal(now)
            return
        # 仍处于作息窗口内（节律够强）就继续睡；窗口结束则醒
        if not self._replaying:
            boost = self._rhythm_boost(now)
            if boost >= RHYTHM_WAKE_HOLD * RHYTHM_WEIGHT * self.rhythm_confidence():
                return
        self._wake_internal(now)

    def _sample_rhythm(self, now: float):
        """睡眠期间按固定间隔采样"此刻处于睡眠"，累积作息直方图。

        因为入睡的两条路径都要求环境安静，这里统计到的每一觉都是
        "安静时她真的睡着了"，可以直接作为作息信号。作息因此是自塑造的：
        她的实际睡眠经历反过来塑造"到点就困"。

        按采样累积而非只记入睡时刻：这样得到的是"她通常睡着的时段"，
        而不是一个单点尖峰。
        """
        if self._replaying:
            return
        if self._last_rhythm_sample is not None and (now - self._last_rhythm_sample) < RHYTHM_SAMPLE_INTERVAL:
            return
        self._last_rhythm_sample = now
        hour = int((now % 86400) / 3600) % 24
        self.rhythm_hist[hour] += 1.0
        self.rhythm_total += 1.0

    def _decay_rhythm(self):
        """入睡前衰减直方图，使作息能跟随生活变化（老数据逐渐淡出）。"""
        self.rhythm_hist = [v * RHYTHM_DECAY for v in self.rhythm_hist]
        self.rhythm_total *= RHYTHM_DECAY

    def note_activity(self, now: float = None):
        """记录一次外部活动（群消息）。用于"安静多久了"的判定。

        只应由外部消息调用：认知循环每几秒就自转一次，把自己的念头算作活动
        会导致永远无法入睡。
        """
        with self._lock:
            self.last_activity = time.time() if now is None else now

    def idle_seconds(self, now: float = None) -> float:
        """距上次外部活动的秒数。"""
        ts = time.time() if now is None else now
        return max(0.0, ts - self.last_activity)

    # ---------- 状态转换 ----------
    def _sleep(self, now: float = None, by_rhythm: bool = False):
        if now is None:
            now = time.time()
        why = "（作息）" if by_rhythm else ""
        if self.last_onset is not None:
            interval = now - self.last_onset
            append_log(f"[生物钟] 入睡{why}（距上次入睡 {interval/3600:.2f}h，压力={self.s:.2f}，"
                       f"节律={self.circadian(now):.2f}，安静={self.idle_seconds(now)/60:.1f}min）")
        else:
            append_log(f"[生物钟] 入睡{why}（压力={self.s:.2f}，节律={self.circadian(now):.2f}）")
        self.last_onset = now
        self.onset_count += 1
        self.state = "asleep"
        self.sleep_started_at = now
        self.woke_by_user = False
        self._by_rhythm = bool(by_rhythm)
        self._late_sleep_guard = False
        self._last_rhythm_sample = None   # 新的一觉，采样从入睡后开始
        # 衰减旧作息后开始累积本次，使作息能跟随生活变化
        if not self._replaying:
            self._decay_rhythm()
            self.rhythm_nights += 1
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
        guard = "（安全阀）" if self._late_sleep_guard else ""
        append_log(f"[生物钟] 自然醒来{guard}（睡了 {dur/3600:.2f}h，压力={self.s:.2f}，"
                   f"作息入睡={self._by_rhythm}）")
        self.last_guard_wake = self._late_sleep_guard   # 留痕供观测/测试，_by_rhythm 清空
        self._by_rhythm = False
        self._late_sleep_guard = False
        self._last_rhythm_sample = None

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
            self._by_rhythm = False
            self._late_sleep_guard = False
            self._last_rhythm_sample = None
            append_log(f"[生物钟] 被唤醒（{reason}，仅睡 {dur/3600:.2f}h，压力仍 {self.s:.2f}）")
            try:
                self.save()
            except Exception as e:
                append_log(f"[生物钟] 保存唤醒状态失败: {e}")

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
            "circadian": round(self.circadian(), 3),
            "rhythm_nights": self.rhythm_nights,
            "rhythm_conf": round(self.rhythm_confidence(), 3),
            "sleep_drive": round(self.sleep_drive(), 3),
            "idle_min": round(self.idle_seconds() / 60, 1),
            "by_rhythm": self._by_rhythm,
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
                # 作息惯性：直方图与统计量需要持久化，否则重启就忘了作息
                "rhythm_hist": self.rhythm_hist,
                "rhythm_nights": self.rhythm_nights,
                "rhythm_total": self.rhythm_total,
                "by_rhythm": self._by_rhythm,
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
                with open(STATE_FILE, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                self.s = float(data.get("s", self.s))
                self.state = data.get("state", "awake")
                self.last_onset = data.get("last_onset", None)
                self.last_wake = data.get("last_wake", None)
                self.onset_count = int(data.get("onset_count", 0))
                self.wake_count = int(data.get("wake_count", 0))
                self.sleep_started_at = data.get("sleep_started_at", None)
                self.awake_until = float(data.get("awake_until", 0.0))
                # 作息惯性（旧存档没有这些字段，用默认值即可平滑过渡）
                hist = data.get("rhythm_hist")
                if isinstance(hist, list) and len(hist) == 24:
                    self.rhythm_hist = [float(v) for v in hist]
                self.rhythm_nights = int(data.get("rhythm_nights", 0))
                self.rhythm_total = float(data.get("rhythm_total", sum(self.rhythm_hist)))
                self._by_rhythm = bool(data.get("by_rhythm", False))
                saved_at = float(data.get("last_tick", time.time()))
                # 离线时长按真实流逝补算（重启后生物钟继续走）；
                # 补算期间跳过睡眠维护，避免启动时执行重量级全量清理。
                self.last_tick = saved_at
                # 重启后重新计安静时间：避免"离线几天→一启动就立刻入睡"
                self.last_activity = time.time()
                self._replaying = True
                try:
                    self.tick(time.time())
                finally:
                    self._replaying = False
                hours = self.rhythm_hours()
                append_log(f"[生物钟] 已加载：{self.state}，精力={self.energy:.2f}，"
                           f"作息数据={self.rhythm_nights}晚"
                           + (f"，常睡时段={hours}" if hours else ""))
            except Exception as e:
                append_log(f"[生物钟] 加载失败，使用默认值: {e}")
                self.last_tick = time.time()
                self._replaying = False


# 全局实例
BIORHYTHM = Biorhythm()
