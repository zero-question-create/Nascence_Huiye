# core/model_backend.py
# ========================================================================
# 本地大模型后端管理器（llama.cpp）
#
# 本项目将三个模型能力全部接入 llama.cpp（GGUF）：
#   - text        : 文本理解/生成（Qwen3-4B GGUF）
#   - embed       : 语义向量（qwen3-embedding GGUF）
#   - multimodal  : 多模态图片理解（Qwen2.5-VL-3B GGUF）
#
# 每个"能力"对应一个 model_backend.ModelBackend 实例，职责：
#   1. 按配置决定使用"本地 llama.cpp"还是"外部 API"
#   2. 本地模式下负责拉起/托管 llama-server 子进程（OpenAI 兼容端口）
#   3. 提供 OpenAI 客户端（openai 库），对外暴露统一 chat / embed / vision 接口
#
# 关键切换语义（控制面板三个开关）：
#   - 默认模型只在进程启动时加载，加载后永不关闭
#   - 若中途从"默认"切到"API"：不关闭正在运行的模型，直接改用 API 配置（可实时更换）
#   - 若中途从"API"切回"默认"：因为默认模型从未被关闭，无需重启即可恢复
#   - 若默认模型从未被加载过（启动时即使用 API），中途切回默认需提示重启
#
# 本模块不应依赖 PyQt / web 框架，保证可被 qq_bot / main / webui 复用。
# ========================================================================

import os
import sys
import time
import socket
import shutil
import logging
import struct
import subprocess
import threading
from pathlib import Path

from openai import OpenAI

from core.win_job import assign_process as _assign_to_job

logger = logging.getLogger("ModelBackend")

# ------------------------------------------------------------------------
# 路径与常量
# ------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent

# llama.cpp 可执行文件目录（由 setup 脚本下载）
LLAMA_BIN_DIR = PROJECT_DIR / "llama" / "bin"
LLAMA_SERVER_EXE = LLAMA_BIN_DIR / ("llama-server.exe" if sys.platform == "win32" else "llama-server")

# 模型文件目录（由 setup 脚本下载三份 GGUF）
MODELS_DIR = PROJECT_DIR / "models"

# 三个后端的模型文件名（与 install_llama 脚本下载的文件名一一对应）
TEXT_MODEL_FILE = MODELS_DIR / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
EMBED_MODEL_FILE = MODELS_DIR / "qwen3-embed-0.6b-q8_0.gguf"
MULTIMODAL_MODEL_FILE = MODELS_DIR / "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"
MULTIMODAL_MMPROJ_FILE = MODELS_DIR / "Qwen2.5-VL-3B-Instruct-mmproj-f16.gguf"

# 三个后端本地服务的端口（OpenAI 兼容 API）
TEXT_PORT = 11436
EMBED_PORT = 11437
MULTIMODAL_PORT = 11438

# llama.cpp 服务启动超时（秒）
_SERVER_START_TIMEOUT = 180

# ------------------------------------------------------------------------
# GGUF 元数据修复
# ------------------------------------------------------------------------
# 部分旧版转换器/量化脚本会把 tokenizer 专用 token-id 写成 INT32(5)，
# 而新版 llama.cpp 强制要求 UINT32(4)，导致启动报错：
#   error loading model vocabulary: key tokenizer.ggml.suffix_token_id
#   has wrong type i32 but expected type u32
# 这里在启动前就地修复类型字节（值本身不变，仅改类型枚举）。
_GGUF_TYPE_UINT32 = 4
_GGUF_TYPE_INT32 = 5

def _repair_gguf_token_type(model_path: Path) -> None:
    """把 GGUF 元数据里 'tokenizer.ggml.*_token_id' 的 INT32 类型改为 UINT32。

    幂等：已经是 UINT32 或字段不存在时直接跳过，不做任何改动。
    """
    if not model_path.exists():
        return
    with open(model_path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:4] != b"GGUF":
        return
    version, n_tensors, n_kv = struct.unpack("<IQQ", head[4:])
    # 流式解析 kv 头部，只记录需要改写的偏移
    patch_offsets = []
    with open(model_path, "rb") as f:
        f.seek(24)

        def _skip(fh, t, size):
            """消费 type=t 的 value 全部字节，返回其总大小（含类型头）。"""
            if t == 8:
                n = struct.unpack("<Q", fh.read(8))[0]
                fh.seek(n, 1)  # 跳过字符串内容
                return size + 8 + n
            if t == 9:
                et = struct.unpack("<I", fh.read(4))[0]
                cnt = struct.unpack("<Q", fh.read(8))[0]
                inner = 12
                for _ in range(cnt):
                    inner = _skip(fh, et, inner)
                return size + inner
            if t in (0, 1, 7): return size + 1
            if t in (2, 3): return size + 2
            if t in (4, 5, 6): return size + 4
            if t in (10, 11, 12): return size + 8
            return size

        for _ in range(n_kv):
            key_len = struct.unpack("<Q", f.read(8))[0]
            key = f.read(key_len)
            type_off = f.tell()
            vtype = struct.unpack("<I", f.read(4))[0]
            if key.startswith(b"tokenizer.ggml.") and key.endswith(b"_token_id") and vtype == _GGUF_TYPE_INT32:
                patch_offsets.append((key, type_off))
            # 跳过 value：_skip 返回 vtype 字段起的完整 value 大小（含 4 字节类型头），
            # 直接用绝对偏移回到下一个 key 起点，避免游标漂移。
            total = _skip(f, vtype, 4)
            f.seek(type_off + total)
    if not patch_offsets:
        return
    with open(model_path, "r+b") as f:
        for key, off in patch_offsets:
            f.seek(off)
            f.write(struct.pack("<I", _GGUF_TYPE_UINT32))
    logger.info("[GGUF] 修复 %d 个 tokenizer token-id 类型（INT32→UINT32）: %s",
                len(patch_offsets),
                ", ".join(k.decode("utf-8", "replace") for k, _ in patch_offsets))


# ------------------------------------------------------------------------
# 模型下载源映射（与 run/install_llama.ps1 一致；启动时缺模型会自动下载）
# 每个模型给出：目标文件名 + 最小预期大小(字节) + 多个源 URL
# 源顺序：HuggingFace 直链 → hf-mirror 国内镜像
# ------------------------------------------------------------------------
MODEL_DOWNLOADS = {
    "text": {
        "file": TEXT_MODEL_FILE,
        "min_size": 2000000000,  # ~2.5GB Q4_K_M
        "urls": [
            "https://huggingface.co/DhruvalLabs/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
            "https://hf-mirror.com/DhruvalLabs/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        ],
    },
    "embed": {
        "file": EMBED_MODEL_FILE,
        "min_size": 500000000,  # ~640MB q8_0
        "urls": [
            "https://huggingface.co/cstr/qwen3-embed-0.6b-GGUF/resolve/main/qwen3-embed-0.6b-q8_0.gguf",
            "https://hf-mirror.com/cstr/qwen3-embed-0.6b-GGUF/resolve/main/qwen3-embed-0.6b-q8_0.gguf",
        ],
    },
    "multimodal": {
        "file": MULTIMODAL_MODEL_FILE,
        "min_size": 1500000000,  # ~1.9GB
        "urls": [
            "https://huggingface.co/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf",
            "https://hf-mirror.com/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf",
        ],
    },
}

# 多模态额外需要 mmproj 文件
MMPROJ_DOWNLOAD = {
    "file": MULTIMODAL_MMPROJ_FILE,
    "min_size": 1000000000,  # ~1.3GB f16
    "urls": [
        "https://huggingface.co/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-mmproj-f16.gguf",
        "https://hf-mirror.com/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-mmproj-f16.gguf",
    ],
}

# 互斥锁：保护子进程的启动/停止与状态变更
_lock = threading.RLock()


class ModelBackend:
    """单个模型能力（text / embed / multimodal）的后端管理对象。

    Attributes:
        name          : 能力名（'text' / 'embed' / 'multimodal'）
        local_model   : 本地 GGUF 文件路径
        local_port    : 本地 llama-server 监听端口
        extra_args    : 启动 llama-server 时的额外参数（如 embedding 模式、mmproj）
        _process      : 当前 llama-server 子进程（仅本地模式）
        _loaded_once  : 本次进程内是否曾经成功加载过本地模型
                        （决定"切回默认是否需要重启"）
    """

    def __init__(self, name, local_model: Path, local_port: int, extra_args=None, mmproj=None):
        self.name = name
        self.local_model = local_model
        self.local_port = local_port
        self.extra_args = list(extra_args or [])
        self.mmproj = mmproj
        self._process = None
        self._loaded_once = False

    # ------------------------------------------------------------------
    # 配置访问
    # ------------------------------------------------------------------
    def _cfg(self):
        """获取当前后端的配置子字典（text/embed/multimodal）。"""
        from config.api_config import config
        return config.get("backends", {}).get(self.name, {})

    def use_default(self) -> bool:
        """是否使用本地默认模型。"""
        return bool(self._cfg().get("use_default", True))

    # ------------------------------------------------------------------
    # 外部 API 三要素
    # ------------------------------------------------------------------
    def api_base_url(self) -> str:
        return str(self._cfg().get("api_base_url", "") or "").strip()

    def api_key(self) -> str:
        return str(self._cfg().get("api_key", "") or "").strip()

    def api_model(self) -> str:
        return str(self._cfg().get("api_model", "") or "").strip()

    # ------------------------------------------------------------------
    # 客户端获取：按当前配置返回可用的 OpenAI 客户端
    # ------------------------------------------------------------------
    def client(self):
        """返回当前模式下可用的 OpenAI 客户端。

        本地模式：指向 llama-server 的 OpenAI 兼容端口（本地不校验 key，用假 key）。
        外部模式：指向用户填写的 API。
        若本地模型尚未就绪且未配置 API，则抛 RuntimeError 提示。
        """
        # 优先本地（若已加载且未崩溃）
        if self.is_local_ready():
            return OpenAI(
                api_key="local-only",
                base_url=f"http://127.0.0.1:{self.local_port}/v1",
                timeout=300,
                max_retries=0,
            )
        # 否则使用外部 API
        url = self.api_base_url()
        key = self.api_key()
        model = self.api_model()
        if not (url and model):
            raise RuntimeError(
                f"[{self.name}] 本地默认模型未加载，且未配置外部 API。"
                "请在控制面板的「模型」页中：使用默认模型（需重启），或填写 API。"
            )
        return OpenAI(api_key=key or "not-needed", base_url=url, timeout=300, max_retries=0)

    # ------------------------------------------------------------------
    # 进程状态
    # ------------------------------------------------------------------
    def is_local_ready(self) -> bool:
        """本地 llama-server 是否已启动并可用（进程存活 + 端口响应）。

        子进程环境（NASCENCE_IS_WORKER=1）下不持有模型进程：所有认知循环统一
        共用主进程启动的共享 llama-server，这里直接探测共享端口判断是否就绪，
        避免子进程各自拉起模型实例争抢端口/重复占内存。
        """
        if os.environ.get("NASCENCE_IS_WORKER") == "1":
            # 共享模式：端口有响应即视为就绪（服务由主进程托管）
            try:
                with socket.create_connection(("127.0.0.1", self.local_port), timeout=0.5):
                    return True
            except OSError:
                return False
        with _lock:
            if self._process is None:
                return False
            if self._process.poll() is not None:
                return False
        # 快速探测端口，确保服务真正就绪
        try:
            with socket.create_connection(("127.0.0.1", self.local_port), timeout=0.5):
                return True
        except OSError:
            return False

    def _process_alive(self) -> bool:
        with _lock:
            return self._process is not None and self._process.poll() is None

    # ------------------------------------------------------------------
    # 启动 / 停止本地模型
    # ------------------------------------------------------------------
    def _ensure_llama_runtime(self) -> bool:
        """确保 llama-server 可执行文件存在。

        仅在真正要启动本地模型时调用：缺失时提示用户并尝试运行安装脚本
        （run/install_llama.sh / .ps1，幂等，已存在则跳过），而非强制预下载。
        """
        if LLAMA_SERVER_EXE.exists():
            return True
        logger.warning("[%s] 未找到 llama-server（%s），正在尝试运行安装脚本下载...",
                       self.name, LLAMA_SERVER_EXE)
        script = PROJECT_DIR / "run" / ("install_llama.ps1" if sys.platform == "win32" else "install_llama.sh")
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                    cwd=PROJECT_DIR, timeout=3600,
                )
            else:
                subprocess.run(["bash", str(script)], cwd=PROJECT_DIR, timeout=3600)
        except Exception as e:
            logger.error("[%s] 安装脚本执行失败: %s", self.name, e)
        if LLAMA_SERVER_EXE.exists():
            logger.info("[%s] llama-server 已就绪", self.name)
            return True
        logger.error("[%s] llama-server 仍不可用，请手动运行 setup 脚本下载", self.name)
        return False

    def start_local(self, wait=True) -> bool:
        """启动本地 llama-server 并加载模型。

        幂等：已启动则直接返回。启动成功后会置 _loaded_once = True。
        全局约束：同一时刻只允许一个 llama-server 进程在运行——
        启动新进程前会先停掉其他后端已启动的进程。
        """
        with _lock:
            if self._process_alive():
                return True
            # 子进程环境（用户 Worker / 其它沙箱）绝不启动本地 llama-server：
            # 所有认知循环统一通过主进程启动并持有的共享模型服务（排队共用），
            # 防止每个子进程各自拉起一个模型实例争抢端口、成倍占用内存。
            if os.environ.get("NASCENCE_IS_WORKER") == "1":
                logger.warning("[%s] 子进程环境不允许启动本地模型服务，统一使用主进程共享模型", self.name)
                return False
            # 只有真正要启动本地模型时，才提示下载并尝试（不强制、不在启动阶段预下载）
            if not self._ensure_llama_runtime():
                logger.error("[%s] llama-server 不可用，无法启动本地模型。请先运行 setup 脚本或 install_llama 脚本下载", self.name)
                return False
            if not self.local_model.exists():
                spec = MODEL_DOWNLOADS.get(self.name)
                logger.warning("[%s] 未找到模型文件 %s，尝试下载...", self.name, self.local_model.name)
                if spec is None or not _download_file(spec):
                    logger.error("[%s] 模型下载失败，无法启动本地模型", self.name)
                    return False
            if self.mmproj is not None and not self.mmproj.exists():
                logger.warning("[%s] 未找到 mmproj 文件 %s，尝试下载...", self.name, self.mmproj.name)
                _download_file(MMPROJ_DOWNLOAD)
            # 保证全局只有一个 llama-server：确认能启动后，先停掉其他后端进程
            for other in BACKENDS.values():
                if other is not self:
                    other.stop_local()

            # 修复旧版转换器写出的 tokenizer token-id 类型（INT32→UINT32），
            # 否则新版 llama.cpp 会拒绝加载模型。
            try:
                _repair_gguf_token_type(self.local_model)
            except Exception as e:
                logger.warning("[%s] GGUF 元数据检查跳过: %s", self.name, e)

            # 构建启动命令
            cmd = [str(LLAMA_SERVER_EXE), "-m", str(self.local_model), "--port", str(self.local_port)]
            cmd += self.extra_args
            if self.mmproj is not None and self.mmproj.exists():
                cmd += ["--mmproj", str(self.mmproj)]
            elif self.mmproj is not None:
                logger.warning("[%s] 未找到 mmproj 文件: %s，多模态模型可能无法识别图片", self.name, self.mmproj)

            logger.info("[%s] 启动 llama-server: %s", self.name, " ".join(cmd))
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                # 绑定到共享 Job Object：主进程消亡（含强杀）时 llama-server 自动终止
                _assign_to_job(self._process)
            except Exception as e:
                logger.error("[%s] 启动失败: %s", self.name, e)
                self._process = None
                return False

            # 后台读取子进程输出，写入日志
            threading.Thread(target=self._pipe_output, args=(self._process,), daemon=True).start()

        if not wait:
            return True

        # 等待端口和模型真正加载就绪（通过 /health 接口确认，避免 503 Loading model 竞态）
        deadline = time.time() + _SERVER_START_TIMEOUT
        while time.time() < deadline:
            if self._process.poll() is not None:
                logger.error("[%s] llama-server 提前退出，code=%s", self.name, self._process.returncode)
                return False
            try:
                # 优先通过 /health 接口检查模型加载状态
                import urllib.request
                req = urllib.request.Request(f"http://127.0.0.1:{self.local_port}/health")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        self._loaded_once = True
                        logger.info("[%s] 本地模型已完全就绪（/health 200），端口=%s", self.name, self.local_port)
                        return True
            except Exception:
                # 端口未通或模型仍在加载中（如 503），继续等待
                time.sleep(0.5)
        logger.error("[%s] 等待模型就绪超时（%ss）", self.name, _SERVER_START_TIMEOUT)
        return False

    def _pipe_output(self, process):
        """把 llama-server 的标准输出逐行写入日志。"""
        if not process.stdout:
            return
        for line in process.stdout:
            if line.strip():
                logger.info("[%s] %s", self.name, line.rstrip("\n"))

    def stop_local(self):
        """停止本地 llama-server（进程级杀除，含子进程）。"""
        with _lock:
            proc = self._process
            self._process = None
        if proc is None or proc.poll() is not None:
            return
        logger.info("[%s] 正在停止本地模型服务", self.name)
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=8)
            else:
                os.killpg(os.getpgid(proc.pid), 15)  # SIGTERM
            proc.wait(timeout=8)
        except Exception:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True, timeout=8)
                else:
                    os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 运行时切换
    # ------------------------------------------------------------------
    def switch_use_default(self, want_default: bool):
        """响应控制面板开关的切换。

        返回 (ok: bool, needs_restart: bool, message: str)。

        规则：
        1. 目标 = 本地：
           - 本地模型曾经加载过且进程存活 → 直接启用，无需重启
           - 本地模型从未加载过 → 返回 needs_restart=True（提示重启后加载）
        2. 目标 = API：
           - 不关闭正在运行的本地模型（保持常驻），仅切换路由
        """
        if want_default:
            if self._loaded_once and self._process_alive():
                return True, False, "已切换回本地默认模型（无需重启）。"
            if self.local_model.exists() and LLAMA_SERVER_EXE.exists():
                # 模型文件在，但本次进程从未加载 → 需要重启
                return True, True, "本地默认模型需重启进程后才能加载。"
            missing = []
            if not LLAMA_SERVER_EXE.exists():
                missing.append("llama-server")
            if not self.local_model.exists():
                missing.append(f"模型文件({self.local_model.name})")
            return False, True, f"无法使用本地默认模型：缺少 {', '.join(missing)}。请先运行 setup 脚本下载。"
        # 目标 = API：校验配置是否完整
        url, key, model = self.api_base_url(), self.api_key(), self.api_model()
        if not (url and model):
            return False, False, "未填写外部 API 的 Base URL 与模型名。"
        return True, False, "已切换到外部 API（本地模型保持运行，可随时切回）。"


# ------------------------------------------------------------------------
# 全局三个后端单例
# ------------------------------------------------------------------------
def _make_backends():
    return {
        "text": ModelBackend(
            name="text",
            local_model=TEXT_MODEL_FILE,
            local_port=TEXT_PORT,
            extra_args=["-c", "8192", "-ngl", "999"],
        ),
        "embed": ModelBackend(
            name="embed",
            local_model=EMBED_MODEL_FILE,
            local_port=EMBED_PORT,
            # llama.cpp embedding 模式：只输出向量
            extra_args=["--embedding", "-c", "8192"],
        ),
        "multimodal": ModelBackend(
            name="multimodal",
            local_model=MULTIMODAL_MODEL_FILE,
            local_port=MULTIMODAL_PORT,
            mmproj=MULTIMODAL_MMPROJ_FILE,
            extra_args=["-c", "8192", "-ngl", "999"],
        ),
    }


BACKENDS = _make_backends()


def _download_file(spec: dict) -> bool:
    """下载一个模型文件（多源逐个尝试），失败返回 False。

    spec: {"file": Path, "min_size": int, "urls": [str, ...]}
    - 文件已存在且大小达标 → 直接返回 True
    - 已存在但过小 → 视为损坏，删除重下
    - 下载完成后再次校验大小
    """
    target = spec["file"]
    min_size = spec["min_size"]
    if target.exists():
        size = target.stat().st_size
        if size >= min_size:
            logger.info("[下载] %s 已存在（%.0f MB），跳过", target.name, size / 1048576)
            return True
        logger.warning("[下载] %s 存在但大小异常（%.0f MB），判定损坏，重新下载", target.name, size / 1048576)
        try:
            target.unlink()
        except OSError:
            pass
    import requests
    target.parent.mkdir(parents=True, exist_ok=True)
    for url in spec["urls"]:
        logger.info("[下载] %s 开始下载: %s", target.name, url)
        try:
            # 流式下载，带进度提示
            with requests.get(url, stream=True, timeout=(10, 120)) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                done = 0
                with open(target, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1048576):
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            if total and done % (100 * 1048576) < 1048576:
                                logger.info("[下载] %s 进度 %.1f%%", target.name, done / total * 100)
            size = target.stat().st_size
            if size < min_size:
                raise RuntimeError(f"文件大小异常（{size / 1048576:.0f} MB），下载可能失败")
            logger.info("[下载] %s 完成（%.0f MB）", target.name, size / 1048576)
            # 修复旧版转换器写出的 tokenizer token-id 类型（INT32→UINT32）
            try:
                _repair_gguf_token_type(target)
            except Exception as e:
                logger.warning("[下载] %s GGUF 元数据修复跳过: %s", target.name, e)
            return True
        except Exception as e:
            logger.error("[下载] %s 源失败: %s", target.name, e)
            try:
                target.unlink()
            except OSError:
                pass
    return False


def ensure_model_files():
    """启动门槛前调用：对 use_default=True 的后端检查模型文件，缺失则自动下载。

    返回 (ok: bool, failed: list[str])——failed 列出下载失败的后端名。
    """
    failed = []
    for name, backend in BACKENDS.items():
        if not backend.use_default():
            logger.info("[%s] 未使用默认模型，跳过模型下载", name)
            continue
        spec = MODEL_DOWNLOADS.get(name)
        if spec is None:
            continue
        if not _download_file(spec):
            failed.append(name)
            continue
        # 多模态额外需要 mmproj
        if name == "multimodal" and not _download_file(MMPROJ_DOWNLOAD):
            failed.append(f"{name}(mmproj)")
    return (len(failed) == 0, failed)


def start_default_backends():
    """启动时调用：对配置为使用默认模型的三个后端执行加载。

    流程：
      0. 先全局清理系统中所有 llama-server 进程（残留/孤儿/其它实例），
         确保全局只有本项目拉起的一套 llama-server，避免端口冲突与内存翻倍
      1. 读取三个开关（config.backends.*.use_default）
      2. 对 use_default=True 的后端尝试启动本地模型；
         不强制预下载——缺失的 llama-server / 模型文件会在 start_local
         内部提示并尝试按需下载（只有真正启动本地模型时才下载）
    返回 dict：{name: bool}，表示每个后端是否就绪。
    """
    # 系统启动：先杀掉所有 llama-server，保证全局唯一
    kill_all_llama_servers()

    results = {}
    for name, backend in BACKENDS.items():
        if backend.use_default():
            results[name] = backend.start_local(wait=True)
        else:
            logger.info("[%s] 配置为使用外部 API，跳过本地加载", name)
            results[name] = False  # 外部 API 模式：本地不加载，视为"无需本地就绪"
    return results


def check_models_ready():
    """启动门槛检查：确保每个能力至少有一种可用来源（本地模型 或 外部 API）。

    仅记录提示，不再抛错阻止启动——缺模型时可先使用外部 API，
    或由用户切换到本地模型时触发按需下载。
    """
    problems = []
    for name, backend in BACKENDS.items():
        if not backend.use_default():
            continue
        # 本地已就绪 → 无需检查
        if backend.is_local_ready():
            continue
        # 本地未就绪：检查是否具备外部 API 兜底
        if backend.api_base_url() and backend.api_model():
            continue
        missing = []
        if not LLAMA_SERVER_EXE.exists():
            missing.append("llama-server")
        if not backend.local_model.exists():
            missing.append(f"模型文件({backend.local_model.name})")
        if missing:
            problems.append(f"「{name}」缺少 {', '.join(missing)}，且未配置外部 API")
        else:
            problems.append(f"「{name}」本地模型启动失败，且未配置外部 API")
    if problems:
        for p in problems:
            logger.warning("[模型] %s（切换到本地模型时将提示下载）", p)


def kill_all_llama_servers():
    """杀掉系统中所有 llama-server 进程（含残留孤儿 / 其它实例）。

    系统启动时调用，保证全局同一时刻只允许本项目拉起的一套 llama-server，
    避免端口冲突、内存/显存翻倍以及孤儿进程残留（例如强制杀服务后遗留的进程）。
    """
    exe = LLAMA_SERVER_EXE.name
    logger.info("[模型] 系统启动：清理所有 llama-server 进程...")
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["taskkill", "/F", "/IM", exe, "/T"],
                capture_output=True, timeout=15,
            )
        else:
            r = subprocess.run(
                ["pkill", "-9", "-f", exe],
                capture_output=True, timeout=15,
            )
        # taskkill 无匹配进程时返回 128；这里统一视为已完成清理
        logger.info("[模型] 已清理所有 llama-server 进程（exit=%s）", getattr(r, "returncode", None))
    except Exception as e:
        logger.warning("[模型] 清理 llama-server 进程异常: %s", e)


def stop_all_backends():
    """退出时调用：停止所有本地 llama-server 进程。"""
    for backend in BACKENDS.values():
        backend.stop_local()


# ------------------------------------------------------------------------
# 进程退出守护：保证无论以何种方式退出（webui 退出/CLI exit/异常/信号），
# 都能把 llama-server 进程一并结束，避免残留孤儿进程。
# ------------------------------------------------------------------------
def _atexit_stop_backends():
    try:
        stop_all_backends()
    except Exception:
        logger.exception("退出时停止 llama-server 失败")


import atexit
atexit.register(_atexit_stop_backends)
