# core/virtual_clock.py
import time         # 主要运用：获取当前时间戳
import os           # 主要运用：确保文件存在及操作安全
import threading    # 主要运用：发散线程
import random

STATE_FILE = "data/test/clock_state.txt"     # 持久化保存路径


class VirtualClock:
    def __init__(self, speed=1.0):
        self.session_start = time.time()    # 先给临时值，避免 self.now() 报错
        self.qq_mode = False                # qq模式开关，先给临时值，避免 self.now() 报错
        self._qq_real_offset = None              # qq模式时间基准
        self._last_save_real_time = None    # 上次保存时的真实时间戳
        saved = self._load_state()
        if saved is not None:
            self.speed = saved['speed']
            self.total_runtime = saved['total_runtime']
            total_virtual = saved['total_virtual']
            self._session_virtual_base = total_virtual - self.total_runtime * self.speed
            # 恢复状态机字段（如果存在，否则使用默认值）
            self._sleep_done_today = saved.get('sleep_done_today', False)
            self._last_sleep_day = saved.get('last_sleep_day', -1)
            self.state = saved.get('state', 'interactive')
            self.last_input_time = saved.get('last_input_time', self.now())
            self.qq_mode = saved.get('qq_mode', False)
            self._qq_real_offset = saved.get('_qq_real_offset', None)
        else:
            self.speed = speed
            self.total_runtime = 0.0
            self._session_virtual_base = 0.0
            self._sleep_done_today = False
            self._last_sleep_day = -1
            self.state = 'interactive'
            self.last_input_time = self.now()

        self.session_start = time.time()
        self.drift_threshold = 15 * 60
        self.sleep_start_hour = 3
        self.sleep_duration = 8 * 3600
        self._drift_thread = None
        self._stop_drift = threading.Event()

    def enable_qq_mode(self):
        self.qq_mode = True
        self.speed = 1.0
        if not self._qq_real_offset:
            self._qq_real_offset = time.time()
            self.save_state()
            self._compensate_downtime()
        else:
            self._session_virtual_base = time.time() - self._qq_real_offset - self.get_real_runtime()
            self.save_state()

    def to_real_time(self, virtual_timestamp: float) -> float:
        """将虚拟时间戳转换为真实Unix时间戳（仅QQ模式有效）"""
        if self._qq_real_offset is not None:
            return virtual_timestamp + self._qq_real_offset
        # 非QQ模式，如果虚拟时间已经是真实时间戳（例如基准为 time.time()），直接返回
        return virtual_timestamp

    def now(self) -> float:
        """返回当前绝对虚拟时间（从基准起算，总真实时间按当前速度缩放）"""
        # 绝对虚拟时间 = 全局基准 + 累计真实运行时间 × 当前速度
        return self._session_virtual_base + self.get_real_runtime() * self.speed

    def set_speed(self, speed: float):
        """更改时间倍速，返回更新后的速度值"""
        # 结算本段真实运行时间，计入总量（冻结此刻之前的真实耗时）
        self.total_runtime += time.time() - self.session_start
        self.session_start = time.time()        # 重置会话起点

        # 计算当前虚拟时间（用旧速度，保证变速瞬间虚拟时间连续）
        virt_now = self._session_virtual_base + self.total_runtime * self.speed

        # 更新速度
        self.speed = speed

        # 逆推新基准，使得“总真实时间 × 新速度 + 新基准 = virt_now”
        # 相当于把历史运行时间无缝拼接到新速度下
        self._session_virtual_base = virt_now - self.total_runtime * self.speed

        return self.speed

    def get_real_runtime(self) -> float:
        """返回从首次启动开始累计的真实运行时间（秒）"""
        # 累计时间 = 历史已结算时间 + 本段未结算时间
        return self.total_runtime + (time.time() - self.session_start)

    def save_state(self):
        self.total_runtime = self.get_real_runtime()
        self.session_start = time.time()
        total_virtual = self._session_virtual_base + self.total_runtime * self.speed
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            f.write(f"{self.speed}\n{total_virtual}\n{self.total_runtime}\n")
            f.write(f"{self._sleep_done_today}\n{self._last_sleep_day}\n")
            f.write(f"{self.state}\n{self.last_input_time}\n")
            f.write(f"{self.qq_mode}\n")
            offset_str = str(self._qq_real_offset) if self._qq_real_offset is not None else ""
            f.write(f"{offset_str}\n")
            f.write(f"{time.time()}\n")   # 第10行：保存时的真实时间

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                lines = f.read().strip().split('\n')
                if len(lines) >= 3:
                    try:
                        speed = float(lines[0])
                        total_virtual = float(lines[1])
                        total_runtime = float(lines[2])
                        sleep_done_today = False
                        last_sleep_day = -1
                        state = 'interactive'
                        last_input_time = 0.0
                        if len(lines) >= 7:
                            sleep_done_today = lines[3].strip() == 'True'
                            last_sleep_day = int(lines[4])
                            state = lines[5].strip()
                            last_input_time = float(lines[6])
                        # 读取QQ模式及偏移量（第8、9行）
                        qq_mode = False
                        qq_real_offset = None
                        if len(lines) >= 9:
                            qq_mode = lines[7].strip() == 'True'
                            offset_str = lines[8].strip()
                            if offset_str:
                                qq_real_offset = float(offset_str)
                        if len(lines) >= 10:
                            last_save_real = float(lines[9].strip())
                            self._last_save_real_time = last_save_real + 0.9

                        return {
                            'speed': speed,
                            'total_virtual': total_virtual,
                            'total_runtime': total_runtime,
                            'sleep_done_today': sleep_done_today,
                            'last_sleep_day': last_sleep_day,
                            'state': state,
                            'last_input_time': last_input_time,
                            'qq_mode': qq_mode,
                            '_qq_real_offset': qq_real_offset
                        }
                    except (ValueError, IndexError):
                        pass
        return None
    
    def _compensate_downtime(self):
        """QQ模式下，补偿停机期间的真实时间流逝"""
        if self.qq_mode and self._last_save_real_time is not None:
            elapsed_real = time.time() - self._last_save_real_time
            if elapsed_real > 0:
                self._session_virtual_base += elapsed_real
                # 同时也更新 last_input_time，避免因停机导致立即进入睡眠
                self.last_input_time = self.now()

    # === 状态机控制方法 ===
    def update_state(self):
        """每个认知周期调用，根据虚拟时间和空闲时长决定状态转换。每天只睡眠一次。"""
        virt_now = self.now()
        day_seconds = virt_now % 86400
        current_hour = day_seconds / 3600.0
        current_day = int(virt_now // 86400)

        # 进入新的一天时，重置睡眠标记
        if current_day != self._last_sleep_day:
            self._sleep_done_today = False

        # 判断当前是否处于睡眠时间窗口
        sleep_end_hour = (self.sleep_start_hour + 8) % 24
        if self.sleep_start_hour < sleep_end_hour:
            in_sleep_window = self.sleep_start_hour <= current_hour < sleep_end_hour
        else:
            # 跨午夜窗口
            in_sleep_window = current_hour >= self.sleep_start_hour or current_hour < sleep_end_hour

        # 如果处于睡眠窗口且今天还没睡过，立刻进入睡眠
        if in_sleep_window and not self._sleep_done_today:
            self._enter_sleep()
            self._sleep_done_today = True
            self._last_sleep_day = current_day
            return

        # 不在睡眠窗口：如果当前状态是 sleeping，则唤醒
        if self.state == "sleeping":
            self._wake_up()
            return

        # 正常交互/发散逻辑
        if self.state == "interactive":
            if virt_now - self.last_input_time > self.drift_threshold:
                self._enter_drifting()
        # drifting 状态会一直保持，直到用户输入时通过 on_user_input 切换回 interactive

    def on_user_input(self):
        """收到用户输入时调用，重置空闲计时并切回交互状态"""
        self.last_input_time = self.now()
        if self.state != "interactive":
            self._enter_interactive()

    def _enter_interactive(self):
        self.state = "interactive"
        self._stop_drift.set()   # 停止发散线程

    def _enter_drifting(self):
        self.state = "drifting"
        self._stop_drift.clear()
        # 启动发散线程（如果尚未启动）
        if self._drift_thread is None or not self._drift_thread.is_alive():
            self._drift_thread = threading.Thread(target=self._drifting_loop, daemon=True)
            self._drift_thread.start()

    def _enter_sleep(self):
        self.state = "sleeping"
        self._stop_drift.set()
        self._sleep_done_today = True
        self._last_sleep_day = int(self.now() // 86400)
        # 持久化当前状态（包括睡眠标记）
        self.save_state()
        # 执行全量睡眠巩固
        from utils.persistence import sleep_cleanup
        sleep_cleanup()

    def _wake_up(self):
        self.state = "interactive"
        self.last_input_time = self.now()

# 全局虚拟时钟实例，默认 1 倍速
clock = VirtualClock(speed=1)