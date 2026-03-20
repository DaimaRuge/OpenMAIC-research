"""
openWakeWord + 豆包ASR + 豆包 Seed LLM + 豆包TTS 完整语音交互 Demo V4

功能流程:
    1. 唤醒词检测 (支持 hey_kee_dah 自定义模型 / hey_jarvis 内置模型)
    2. 唤醒后进入 ASR 录音模式
    3. 使用 Silero VAD 检测说话停止
    4. 流式传输音频到豆包 ASR
    5. ASR 识别结果 → doubao-seed-2-0-mini-260215 大模型
    6. 大模型输出 → 豆包 TTS 合成语音
    7. 本地播放 TTS 语音

V4 新特性:
    - 运行时选择唤醒词 (--wakeword hey_kee_dah / hey_jarvis)
    - 接入豆包 Seed-2.0-mini 大模型 (火山引擎 ARK API)
    - 支持多轮对话上下文 (最多 10 轮)
    - TTS 播放大模型回复而非原始 ASR 结果
    - 交互式 VAD 参数调节 (阈值/静音判停/降噪等)
    - noisereduce 实时降噪 (替代不可用的 SpeexDSP)
    - 播放时打断唤醒 (barge-in)
    - 连续对话模式 (对话后自动继续监听)

用法:
    python wake_word_asr_v4.py                         # 默认唤醒词: hey_kee_dah
    python wake_word_asr_v4.py --wakeword hey_jarvis   # 使用 hey_jarvis
    python wake_word_asr_v4.py --wakeword hey_kee_dah  # 明确指定 hey_kee_dah

依赖安装:
    pip install sounddevice openwakeword numpy pyyaml aiohttp openai

API Key 配置 (二选一):
    1. 环境变量: set ARK_API_KEY=your_api_key
    2. config.yaml:  ark:\n  api_key: "your_api_key"
"""

from __future__ import annotations

# Windows 控制台 UTF-8 编码设置
import sys
import io
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    except:
        pass

import asyncio
import json
import struct
import gzip
import uuid
import time
import threading
import queue
import yaml
import logging
import hmac
import base64
import hashlib
import os
from pathlib import Path
from typing import Optional, List, Dict, Tuple, AsyncIterator, Deque, Any
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import numpy as np


# ============ 日志配置 ============

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# ============ 唤醒词选项 ============

OWW_WORKSPACE = Path(r"D:\CodeStudy\openWakeWord-0.6.0\workspace")
OWW_LATEST_META = OWW_WORKSPACE / "latest_model.json"
CUSTOM_MODEL_PATH_FALLBACK = Path(r"D:\CodeStudy\openWakeWord-0.6.0\workspace\my_custom_model\hey_kee_dah.onnx")


def resolve_latest_custom_model() -> Tuple[str, str, str]:
    """
    返回 (model_path, prediction_key, display_name)
    优先读取 openWakeWord 训练产物 latest_model.json，失败则回退到固定路径。
    """
    if OWW_LATEST_META.exists():
        try:
            meta = json.loads(OWW_LATEST_META.read_text(encoding="utf-8"))
            latest_path = meta.get("latest_onnx_path") or meta.get("onnx_path")
            prediction_key = meta.get("prediction_key") or meta.get("model_name")
            model_name = meta.get("model_name", "latest_custom_model")
            if latest_path and prediction_key and Path(latest_path).exists():
                return str(Path(latest_path)), str(prediction_key), f"{model_name} (自动最新)"
        except Exception as e:
            logger.warning(f"读取 latest_model.json 失败，将使用回退模型: {e}")

    return str(CUSTOM_MODEL_PATH_FALLBACK), "hey_kee_dah", "Hey Kida (自定义模型-回退)"


CUSTOM_MODEL_PATH, CUSTOM_PREDICTION_KEY, CUSTOM_DISPLAY_NAME = resolve_latest_custom_model()

WAKEWORD_OPTIONS: Dict[str, Dict] = {
    "hey_kee_dah": {
        "model_path": CUSTOM_MODEL_PATH,
        "prediction_key": CUSTOM_PREDICTION_KEY,
        "display_name": CUSTOM_DISPLAY_NAME,
        "is_custom": True,
    },
    "hey_jarvis": {
        "model_path": None,           # 使用内置模型, download_models() 下载
        "prediction_key": "hey_jarvis",
        "display_name": "Hey Jarvis (内置模型)",
        "is_custom": False,
    },
}
DEFAULT_WAKEWORD = "hey_kee_dah"


# ============ VAD 预设方案 ============

VAD_PRESETS: Dict[str, Dict] = {
    "灵敏": {
        "display_name": "灵敏模式  (安静环境, 快速响应)",
        "threshold": 0.25,
        "silence_duration": 2.0,
        "no_speech_timeout": 4.0,
        "smooth_frames": 3,
        "noise_suppress": False,
    },
    "均衡": {
        "display_name": "均衡模式  (推荐, 兼顾灵敏与抗噪)",
        "threshold": 0.35,
        "silence_duration": 2.5,
        "no_speech_timeout": 5.0,
        "smooth_frames": 4,
        "noise_suppress": True,
    },
    "抗噪": {
        "display_name": "抗噪模式  (嘈杂环境, 减少误触发)",
        "threshold": 0.50,
        "silence_duration": 3.0,
        "no_speech_timeout": 6.0,
        "smooth_frames": 6,
        "noise_suppress": True,
    },
    "自定义": {
        "display_name": "自定义     (手动设置所有参数)",
        "threshold": None,
        "silence_duration": None,
        "no_speech_timeout": None,
        "smooth_frames": None,
        "noise_suppress": None,
    },
}
DEFAULT_VAD_PRESET = "均衡"


# ============ 大模型选项 ============

MODEL_OPTIONS: Dict[str, Dict] = {
    "doubao-seed-2-0-mini-260215": {
        "display_name": "Seed-2.0-mini  (面向低时延、高并发与成本敏感场景)",
        "api_model": "doubao-seed-2-0-mini-260215",
    },
    "doubao-seed-2-0-pro-260215": {
        "display_name": "Seed-2.0-pro   (侧重长链路推理能力与复杂任务稳定性，适配真实业务中的复杂场景)",
        "api_model": "doubao-seed-2-0-pro-260215",
    },
    "doubao-seed-2-0-lite-260215": {
        "display_name": "Seed-2.0-lite  (兼顾生成质量与响应速度，适合作为通用生产级模型)",
        "api_model": "doubao-seed-2-0-lite-260215",
    },
}
DEFAULT_MODEL = "doubao-seed-2-0-mini-260215"


# ============ 官方SDK协议常量 ============

class ProtocolVersion:
    V1 = 0b0001

class MessageType:
    CLIENT_FULL_REQUEST = 0b0001
    CLIENT_AUDIO_ONLY_REQUEST = 0b0010
    SERVER_FULL_RESPONSE = 0b1001
    SERVER_ERROR_RESPONSE = 0b1111

class MessageTypeSpecificFlags:
    NO_SEQUENCE = 0b0000
    POS_SEQUENCE = 0b0001
    NEG_SEQUENCE = 0b0010
    NEG_WITH_SEQUENCE = 0b0011

class SerializationType:
    NO_SERIALIZATION = 0b0000
    JSON = 0b0001

class CompressionType:
    GZIP = 0b0001


# ============ 配置加载 ============

class Config:
    """配置管理"""

    def __init__(self, config_path: str = "config.yaml"):
        path = Path(config_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / config_path
        with open(path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        self._set_defaults()

    def _set_defaults(self):
        """设置默认配置"""
        self.config.setdefault('tts', {})
        tts = self.config['tts']
        tts.setdefault('url', 'https://openspeech.bytedance.com/api/v3/tts/unidirectional')
        tts.setdefault('resource_id', 'seed-tts-1.0')
        tts.setdefault('speaker', 'zh_female_shuangkuaisisi_moon_bigtts')
        tts.setdefault('format', 'mp3')
        tts.setdefault('sample_rate', 24000)

        self.config.setdefault('vad', {})
        vad = self.config['vad']
        vad.setdefault('threshold', 0.35)
        vad.setdefault('silence_duration', 2.5)
        vad.setdefault('no_speech_timeout', 5.0)
        vad.setdefault('smooth_frames', 4)
        vad.setdefault('noise_suppress', True)

        self.config.setdefault('recording', {})
        rec = self.config['recording']
        rec.setdefault('max_duration', 50)

        self.config.setdefault('conversation', {})
        conv = self.config['conversation']
        conv.setdefault('continuous_mode', True)
        conv.setdefault('continuous_timeout', 8.0)
        conv.setdefault('barge_in', True)

        self.config.setdefault('wakeword', {})
        ww = self.config['wakeword']
        ww.setdefault('threshold', 0.5)
        ww.setdefault('vad_threshold', 0.5)
        ww.setdefault('enable_speex_noise_suppression', False)
        ww.setdefault('patience', 3)
        ww.setdefault('debounce_time', 2.0)
        ww.setdefault('smoothing_window', 5)

        # ARK LLM 配置 (火山引擎)
        self.config.setdefault('ark', {})
        ark = self.config['ark']
        ark.setdefault('api_key', '')
        ark.setdefault('model', 'doubao-seed-2-0-mini-260215')
        ark.setdefault('base_url', 'https://ark.cn-beijing.volces.com/api/v3')
        ark.setdefault('max_tokens', 500)
        ark.setdefault('max_history_turns', 10)
        ark.setdefault('system_prompt',
            '你是一个友好的AI语音助手。请给出简洁、口语化的回答，避免长段落和列表，方便朗读。')

    @property
    def doubao(self) -> Dict:
        return self.config['doubao']

    @property
    def tts(self) -> Dict:
        return self.config['tts']

    @property
    def vad(self) -> Dict:
        return self.config['vad']

    @property
    def wakeword(self) -> Dict:
        return self.config['wakeword']

    @property
    def audio(self) -> Dict:
        return self.config['audio']

    @property
    def recording(self) -> Dict:
        return self.config['recording']

    @property
    def performance(self) -> Dict:
        return self.config.get('performance', {})

    @property
    def ark(self) -> Dict:
        return self.config['ark']

    def get_ark_api_key(self) -> str:
        """获取 ARK API Key: 优先 config.yaml，其次环境变量"""
        key = self.config['ark'].get('api_key', '').strip()
        if key:
            return key
        env_key = os.environ.get('ARK_API_KEY', '').strip()
        if env_key:
            return env_key
        return ''


# ============ 官方SDK工具类 ============

class CommonUtils:
    @staticmethod
    def gzip_compress(data: bytes) -> bytes:
        return gzip.compress(data)

    @staticmethod
    def gzip_decompress(data: bytes) -> bytes:
        return gzip.decompress(data)


class AsrRequestHeader:
    def __init__(self):
        self.message_type = MessageType.CLIENT_FULL_REQUEST
        self.message_type_specific_flags = MessageTypeSpecificFlags.POS_SEQUENCE
        self.serialization_type = SerializationType.JSON
        self.compression_type = CompressionType.GZIP
        self.reserved_data = bytes([0x00])

    def with_message_type(self, message_type: int) -> 'AsrRequestHeader':
        self.message_type = message_type
        return self

    def with_message_type_specific_flags(self, flags: int) -> 'AsrRequestHeader':
        self.message_type_specific_flags = flags
        return self

    def with_serialization_type(self, serialization_type: int) -> 'AsrRequestHeader':
        self.serialization_type = serialization_type
        return self

    def with_compression_type(self, compression_type: int) -> 'AsrRequestHeader':
        self.compression_type = compression_type
        return self

    def with_reserved_data(self, reserved_data: bytes) -> 'AsrRequestHeader':
        self.reserved_data = reserved_data
        return self

    def to_bytes(self) -> bytes:
        header = bytearray()
        header.append((ProtocolVersion.V1 << 4) | 1)
        header.append((self.message_type << 4) | self.message_type_specific_flags)
        header.append((self.serialization_type << 4) | self.compression_type)
        header.extend(self.reserved_data)
        return bytes(header)

    @staticmethod
    def default_header() -> 'AsrRequestHeader':
        return AsrRequestHeader()


class RequestBuilder:
    @staticmethod
    def new_auth_headers(config: Config) -> Dict[str, str]:
        reqid = str(uuid.uuid4())
        return {
            "X-Api-Resource-Id": config.doubao['resource_id'],
            "X-Api-Request-Id": reqid,
            "X-Api-Connect-Id": reqid,
            "X-Api-Access-Key": config.doubao['access_key'],
            "X-Api-App-Key": config.doubao['app_key']
        }

    @staticmethod
    def new_full_client_request(seq: int, config: Config,
                                end_window_size: int = 800,
                                force_to_speech_time: int = 1000) -> bytes:
        header = AsrRequestHeader.default_header() \
            .with_message_type_specific_flags(MessageTypeSpecificFlags.POS_SEQUENCE)

        payload = {
            "user": {"uid": f"voice_assistant_{int(time.time())}"},
            "audio": {
                "format": "pcm", "codec": "raw",
                "rate": config.audio['rate'],
                "bits": 16, "channel": config.audio['channels']
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True, "enable_punc": True,
                "enable_ddc": True, "show_utterances": True,
                "result_type": "full",
                "enable_nonstream": False,
                "end_window_size": end_window_size,
                "force_to_speech_time": force_to_speech_time,
            }
        }

        payload_bytes = json.dumps(payload).encode('utf-8')
        compressed_payload = CommonUtils.gzip_compress(payload_bytes)

        request = bytearray()
        request.extend(header.to_bytes())
        request.extend(struct.pack('>i', seq))
        request.extend(struct.pack('>I', len(compressed_payload)))
        request.extend(compressed_payload)
        return bytes(request)

    @staticmethod
    def new_audio_only_request(seq: int, segment: bytes, is_last: bool = False) -> bytes:
        header = AsrRequestHeader.default_header()
        if is_last:
            header.with_message_type_specific_flags(MessageTypeSpecificFlags.NEG_WITH_SEQUENCE)
            seq = -seq
        else:
            header.with_message_type_specific_flags(MessageTypeSpecificFlags.POS_SEQUENCE)
        header.with_message_type(MessageType.CLIENT_AUDIO_ONLY_REQUEST)

        request = bytearray()
        request.extend(header.to_bytes())
        request.extend(struct.pack('>i', seq))
        compressed_segment = CommonUtils.gzip_compress(segment)
        request.extend(struct.pack('>I', len(compressed_segment)))
        request.extend(compressed_segment)
        return bytes(request)


class AsrResponse:
    def __init__(self):
        self.code = 0
        self.event = 0
        self.is_last_package = False
        self.payload_sequence = 0
        self.payload_size = 0
        self.payload_msg = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code, "event": self.event,
            "is_last_package": self.is_last_package,
            "payload_sequence": self.payload_sequence,
            "payload_size": self.payload_size,
            "payload_msg": self.payload_msg
        }

    def get_text(self) -> str:
        if self.payload_msg:
            if isinstance(self.payload_msg, dict):
                if "result" in self.payload_msg:
                    result = self.payload_msg["result"]
                    if isinstance(result, dict):
                        return result.get("text", "")
                    elif isinstance(result, str):
                        return result
                elif "text" in self.payload_msg:
                    return self.payload_msg["text"]
            elif isinstance(self.payload_msg, str):
                return self.payload_msg
        return ""

    def has_definite_utterance(self) -> bool:
        """检查是否包含 definite=true 的分句 (服务端 VAD 判停)"""
        if not self.payload_msg or not isinstance(self.payload_msg, dict):
            return False
        result = self.payload_msg.get("result")
        if not isinstance(result, dict):
            return False
        for utt in result.get("utterances", []):
            if utt.get("definite") is True:
                return True
        return False


class ResponseParser:
    @staticmethod
    def parse_response(msg: bytes) -> AsrResponse:
        response = AsrResponse()
        if len(msg) < 4:
            return response

        header_size = msg[0] & 0x0f
        message_type = msg[1] >> 4
        message_type_specific_flags = msg[1] & 0x0f
        serialization_method = msg[2] >> 4
        message_compression = msg[2] & 0x0f

        payload = msg[header_size * 4:]

        if message_type_specific_flags & 0x01:
            if len(payload) >= 4:
                response.payload_sequence = struct.unpack('>i', payload[:4])[0]
                payload = payload[4:]
        if message_type_specific_flags & 0x02:
            response.is_last_package = True
        if message_type_specific_flags & 0x04:
            if len(payload) >= 4:
                response.event = struct.unpack('>i', payload[:4])[0]
                payload = payload[4:]

        if message_type == MessageType.SERVER_FULL_RESPONSE:
            if len(payload) >= 4:
                response.payload_size = struct.unpack('>I', payload[:4])[0]
                payload = payload[4:]
        elif message_type == MessageType.SERVER_ERROR_RESPONSE:
            if len(payload) >= 8:
                response.code = struct.unpack('>i', payload[:4])[0]
                response.payload_size = struct.unpack('>I', payload[4:8])[0]
                payload = payload[8:]

        if not payload:
            return response

        if message_compression == CompressionType.GZIP:
            try:
                payload = CommonUtils.gzip_decompress(payload)
            except Exception as e:
                logger.debug(f"Failed to decompress payload: {e}")
                return response

        try:
            if serialization_method == SerializationType.JSON:
                response.payload_msg = json.loads(payload.decode('utf-8'))
        except Exception as e:
            logger.debug(f"Failed to parse payload: {e}")

        return response


# ============ ASR WebSocket客户端 (与v3相同) ============

class DoubaoASRClient:
    def __init__(self, config: Config):
        self.config = config
        self.url = config.doubao['url']
        self.seq = 1
        self.conn = None
        self.session = None
        self._is_connected = False
        self._result_queue: asyncio.Queue[str] = asyncio.Queue()
        self._final_result = ""

    async def __aenter__(self):
        import aiohttp
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.conn and not self.conn.closed:
            await self.conn.close()
        if self.session and not self.session.closed:
            await self.session.close()

    async def connect(self) -> bool:
        import aiohttp
        try:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession()
            headers = RequestBuilder.new_auth_headers(self.config)
            self.conn = await self.session.ws_connect(self.url, headers=headers)
            self._is_connected = True
            logger.info(f"ASR连接成功 (ID: {headers['X-Api-Request-Id'][:8]}...)")
            await self.send_full_client_request()
            msg = await self.conn.receive()
            if msg.type == aiohttp.WSMsgType.BINARY:
                response = ResponseParser.parse_response(msg.data)
                logger.debug(f"ASR配置响应: {response.to_dict()}")
            return True
        except Exception as e:
            logger.error(f"ASR连接失败: {e}")
            self._is_connected = False
            return False

    async def send_full_client_request(self):
        request = RequestBuilder.new_full_client_request(self.seq, self.config)
        self.seq += 1
        await self.conn.send_bytes(request)

    async def send_audio_chunk(self, audio_data: bytes, is_last: bool = False):
        if not self._is_connected:
            return
        try:
            request = RequestBuilder.new_audio_only_request(self.seq, audio_data, is_last=is_last)
            await self.conn.send_bytes(request)
            if not is_last:
                self.seq += 1
        except Exception as e:
            logger.error(f"发送音频失败: {e}")

    async def receive_loop(self):
        import aiohttp
        if not self._is_connected:
            return
        try:
            async for msg in self.conn:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    response = ResponseParser.parse_response(msg.data)
                    text = response.get_text()
                    if text:
                        await self._result_queue.put(text)
                    if response.is_last_package or response.code != 0:
                        break
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except Exception as e:
            logger.error(f"接收循环错误: {e}")

    async def close(self):
        if self.conn:
            try:
                await self.conn.close()
            except Exception:
                pass
        self._is_connected = False

    def clear_queue(self):
        while not self._result_queue.empty():
            try:
                self._result_queue.get_nowait()
            except:
                break


# ============ 流式 ASR 会话管理器 ============

class StreamingASRSession:
    """
    流式 ASR 会话管理器。

    在独立线程中运行 asyncio event-loop，持有一个 DoubaoASRClient 连接。
    音频回调线程通过 feed_audio() 将 PCM 帧投入队列，
    内部 _send_loop 取出并发送给 ASR 服务端。
    内部 _recv_loop 接收识别结果并通过回调通知外部。

    生命周期:
        session = StreamingASRSession(config, on_partial=..., on_final=...)
        session.start()          # 打开 WS 连接, 启动发送/接收循环
        session.feed_audio(...)  # 从音频回调线程调用 (线程安全)
        session.finish()         # 发送最后一包, 等待 definite / last_package
        session.abort()          # 强制关闭 (无语音超时等)
    """

    # 哨兵对象: 投入队列表示 "最后一包, 请关闭"
    _FINISH_SENTINEL = object()

    def __init__(self, config: Config, *,
                 on_partial: Optional[callable] = None,
                 on_final: Optional[callable] = None,
                 on_error: Optional[callable] = None,
                 end_window_size: int = 800,
                 force_to_speech_time: int = 1000):
        """
        Args:
            config: 全局配置
            on_partial: 回调 (text: str)  — 每次服务端返回中间结果时调用
            on_final:   回调 (text: str)  — 收到 definite / last_package 时调用
            on_error:   回调 (error: str) — 出错时调用
            end_window_size:    服务端静音判停 (ms), 默认 800
            force_to_speech_time: 最短语音时长才尝试判停 (ms), 默认 1000
        """
        self._config = config
        self._on_partial = on_partial
        self._on_final = on_final
        self._on_error = on_error
        self._end_window_size = end_window_size
        self._force_to_speech_time = force_to_speech_time

        # 线程间通信: 音频帧队列 (bytes | _FINISH_SENTINEL)
        self._audio_queue: queue.Queue = queue.Queue(maxsize=500)

        # 内部状态
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._started = threading.Event()
        self._finished = threading.Event()
        self._aborted = False
        self._latest_text = ""

    # ---------- public API (线程安全) ----------

    def start(self):
        """在后台线程中启动 ASR 连接和收发循环。"""
        self._finished.clear()
        self._started.clear()
        self._aborted = False
        self._latest_text = ""
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name="StreamingASR")
        self._thread.start()
        # 等待连接就绪 (最多 5s)
        if not self._started.wait(timeout=5.0):
            logger.error("StreamingASR: 启动超时")

    def feed_audio(self, audio_int16: np.ndarray):
        """
        从音频回调线程投递一帧 PCM int16 音频。
        如果队列满则丢弃 (不阻塞回调线程)。
        """
        if self._finished.is_set() or self._aborted:
            return
        try:
            self._audio_queue.put_nowait(audio_int16.tobytes())
        except queue.Full:
            pass  # 丢掉这帧, 不阻塞音频回调

    def finish(self, timeout: float = 5.0):
        """
        通知 ASR 发送最后一包, 等待最终结果返回。
        调用后不要再 feed_audio。
        """
        if self._aborted or self._finished.is_set():
            return
        try:
            self._audio_queue.put_nowait(self._FINISH_SENTINEL)
        except queue.Full:
            # 清空队列再放
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except queue.Empty:
                    break
            self._audio_queue.put_nowait(self._FINISH_SENTINEL)
        # 等待接收循环结束
        self._finished.wait(timeout=timeout)

    def abort(self):
        """强制中止 (无语音超时等)。"""
        self._aborted = True
        # 放入哨兵让 send loop 退出
        try:
            self._audio_queue.put_nowait(self._FINISH_SENTINEL)
        except queue.Full:
            pass
        self._finished.set()

    @property
    def is_active(self) -> bool:
        return self._started.is_set() and not self._finished.is_set()

    @property
    def latest_text(self) -> str:
        return self._latest_text

    # ---------- internal ----------

    def _run_thread(self):
        """后台线程入口: 创建 event-loop, 运行 _session_main。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session_main())
        except Exception as e:
            logger.error(f"StreamingASR 线程异常: {e}")
            if self._on_error:
                self._on_error(str(e))
        finally:
            try:
                # 清理残余 tasks
                pending = asyncio.all_tasks(self._loop)
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self._loop.close()
            self._finished.set()

    async def _session_main(self):
        """一次完整的流式 ASR 会话。"""
        import aiohttp
        session = aiohttp.ClientSession()
        try:
            headers = RequestBuilder.new_auth_headers(self._config)
            conn = await session.ws_connect(self._config.doubao['url'], headers=headers)
            logger.info(f"流式ASR连接成功 (ID: {headers['X-Api-Request-Id'][:8]}...)")

            # 发送 full_client_request (包含 end_window_size)
            fcr = RequestBuilder.new_full_client_request(
                1, self._config,
                end_window_size=self._end_window_size,
                force_to_speech_time=self._force_to_speech_time,
            )
            await conn.send_bytes(fcr)

            # 等待配置确认
            init_msg = await asyncio.wait_for(conn.receive(), timeout=5.0)
            if init_msg.type == aiohttp.WSMsgType.BINARY:
                resp = ResponseParser.parse_response(init_msg.data)
                logger.debug(f"流式ASR配置响应: {resp.to_dict()}")

            self._started.set()

            # 并行: 发送音频 + 接收结果
            send_task = asyncio.create_task(self._send_loop(conn))
            recv_task = asyncio.create_task(self._recv_loop(conn))

            # 等待两者都完成
            await asyncio.gather(send_task, recv_task, return_exceptions=True)

        except Exception as e:
            logger.error(f"流式ASR会话错误: {e}")
            if self._on_error:
                self._on_error(str(e))
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def _send_loop(self, conn):
        """从 _audio_queue 取音频帧并发送到 ASR 服务端。"""
        seq = 2  # seq=1 已被 full_client_request 使用
        try:
            while not self._aborted:
                # 非阻塞检查队列 (用短 sleep 避免忙等)
                try:
                    item = self._audio_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.02)
                    continue

                if item is self._FINISH_SENTINEL:
                    # 发送最后一包 (空音频 + 负序列号)
                    last_req = RequestBuilder.new_audio_only_request(seq, b'', is_last=True)
                    try:
                        await conn.send_bytes(last_req)
                    except Exception:
                        pass
                    logger.debug("流式ASR: 已发送最后一包")
                    break

                # 正常音频包
                req = RequestBuilder.new_audio_only_request(seq, item, is_last=False)
                try:
                    await conn.send_bytes(req)
                    seq += 1
                except Exception as e:
                    logger.error(f"流式ASR发送错误: {e}")
                    break
        except Exception as e:
            logger.error(f"流式ASR send_loop 异常: {e}")

    async def _recv_loop(self, conn):
        """接收 ASR 服务端返回的识别结果。"""
        import aiohttp
        final_sent = False  # 确保 on_final 只调用一次
        try:
            async for msg in conn:
                if self._aborted:
                    break
                if msg.type == aiohttp.WSMsgType.BINARY:
                    response = ResponseParser.parse_response(msg.data)
                    text = response.get_text()
                    if text:
                        self._latest_text = text
                        if self._on_partial and not final_sent:
                            self._on_partial(text)

                    # 检查 definite (服务端 VAD 判停)
                    if response.has_definite_utterance() and not final_sent:
                        logger.info(f"流式ASR: 服务端判停 (definite)")
                        final_sent = True
                        if text and self._on_final:
                            self._on_final(text)

                    # 最后一包 → 会话结束
                    if response.is_last_package:
                        logger.debug("流式ASR: 收到 last_package")
                        if not final_sent:
                            final_sent = True
                            final = self._latest_text
                            if final and self._on_final:
                                self._on_final(final)
                        break

                    if response.code != 0:
                        logger.error(f"流式ASR服务端错误: code={response.code}")
                        if self._on_error:
                            self._on_error(f"ASR error code {response.code}")
                        break

                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    logger.warning("流式ASR: 连接关闭/错误")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"流式ASR recv_loop 异常: {e}")
        finally:
            self._finished.set()


# ============ 实时降噪模块 ============

class NoiseSuppressor:
    """
    基于 noisereduce 的实时降噪。
    用法: 在每帧音频送入 VAD / ASR 之前调用 process()。
    首次调用自动采集 ~1s 环境噪声作为噪声 profile。
    """

    def __init__(self, sample_rate: int = 16000, enabled: bool = True):
        self.sr = sample_rate
        self.enabled = enabled
        self._nr = None
        self._noise_profile: Optional[np.ndarray] = None
        self._calibration_buf: List[np.ndarray] = []
        self._calibration_samples = sample_rate  # 1s
        self._calibration_collected = 0
        self._ready = False
        if enabled:
            self._try_import()

    def _try_import(self):
        try:
            import noisereduce as nr
            self._nr = nr
            logger.info("降噪模块 (noisereduce) 已加载")
        except ImportError:
            logger.warning("noisereduce 未安装, 降噪已禁用. pip install noisereduce")
            self.enabled = False

    def process(self, audio_int16: np.ndarray) -> np.ndarray:
        """处理一帧 int16 音频, 返回降噪后的 int16 音频"""
        if not self.enabled or self._nr is None:
            return audio_int16

        # 校准阶段: 采集噪声 profile
        if not self._ready:
            self._calibration_buf.append(audio_int16.copy())
            self._calibration_collected += len(audio_int16)
            if self._calibration_collected >= self._calibration_samples:
                self._noise_profile = np.concatenate(self._calibration_buf).astype(np.float32) / 32768.0
                self._calibration_buf.clear()
                self._ready = True
                logger.info(f"降噪校准完成 ({self._calibration_collected / self.sr:.1f}s 噪声样本)")
            return audio_int16

        # 降噪处理
        try:
            audio_float = audio_int16.astype(np.float32) / 32768.0
            cleaned = self._nr.reduce_noise(
                y=audio_float,
                sr=self.sr,
                y_noise=self._noise_profile,
                stationary=True,
                prop_decrease=0.75,
            )
            return (cleaned * 32768.0).clip(-32768, 32767).astype(np.int16)
        except Exception:
            return audio_int16

    def reset(self):
        """重置校准 (换环境后调用)"""
        self._noise_profile = None
        self._calibration_buf.clear()
        self._calibration_collected = 0
        self._ready = False


# ============ VAD 模块 ============

class VAD:
    def __init__(self, threshold: float = 0.35, silence_duration: float = 2.5,
                 smooth_frames: int = 4):
        self.threshold = threshold
        self.silence_duration = silence_duration
        self._vad_model = None
        self._silence_start = None
        self._is_speaking = False
        # 帧平滑：连续 N 帧非语音才开始计静音，防止瞬间抖动误判
        self._nonspeech_streak = 0
        self._SMOOTH_FRAMES = smooth_frames
        self._init_vad()

    def _init_vad(self):
        try:
            from openwakeword.vad import VAD as OWVAD
            self._vad_model = OWVAD()
            logger.info("VAD模块初始化完成")
        except Exception as e:
            logger.warning(f"VAD初始化失败: {e}")

    def reset(self):
        self._silence_start = None
        self._is_speaking = False
        self._nonspeech_streak = 0

    def process(self, audio_frame: np.ndarray) -> bool:
        if self._vad_model is None:
            return True
        try:
            speech_prob = self._vad_model(audio_frame)
            is_speech = speech_prob >= self.threshold
            if is_speech:
                self._is_speaking = True
                self._silence_start = None
                self._nonspeech_streak = 0
            else:
                self._nonspeech_streak += 1
                # 只有连续多帧非语音才开始计静音（帧平滑）
                if (self._is_speaking
                        and self._silence_start is None
                        and self._nonspeech_streak >= self._SMOOTH_FRAMES):
                    self._silence_start = time.time()
            return is_speech
        except Exception:
            return True

    def is_speaking(self) -> bool:
        if not self._is_speaking:
            return False
        if self._silence_start is not None:
            if time.time() - self._silence_start >= self.silence_duration:
                self._is_speaking = False
                self._silence_start = None
                return False
        return True

    def has_speech_stopped(self) -> bool:
        return (self._is_speaking and self._silence_start is not None and
                (time.time() - self._silence_start) >= self.silence_duration)

    def get_silence_duration(self) -> float:
        if self._silence_start is None:
            return 0.0
        return time.time() - self._silence_start


# ============ 音频环形缓冲区 ============

class AudioRingBuffer:
    def __init__(self, max_duration: float = 30.0, sample_rate: int = 16000):
        self.max_samples = int(max_duration * sample_rate)
        self.sample_rate = sample_rate
        self.buffer: Deque[np.ndarray] = deque(maxlen=2000)
        self._lock = threading.Lock()
        self._total_samples = 0

    def append(self, audio: np.ndarray):
        with self._lock:
            self.buffer.append(audio.copy())
            self._total_samples += len(audio)

    def get_all(self) -> np.ndarray:
        with self._lock:
            if not self.buffer:
                return np.array([], dtype=np.int16)
            return np.concatenate(list(self.buffer))

    def clear(self):
        with self._lock:
            self.buffer.clear()
            self._total_samples = 0

    @property
    def duration(self) -> float:
        return self._total_samples / self.sample_rate


# ============ TTS客户端 ============

class DoubaoTTSClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def synthesize(self, text: str) -> bytes:
        session = None
        try:
            import aiohttp
            session = aiohttp.ClientSession()
            request_id = str(uuid.uuid4())
            headers = {
                "Content-Type": "application/json",
                "X-Api-App-Id": self.config.doubao['app_key'],
                "X-Api-Access-Key": self.config.doubao['access_key'],
                "X-Api-Resource-Id": self.config.tts['resource_id'],
                "X-Api-Request-Id": request_id,
            }
            payload = {
                "user": {"uid": "demo_uid"},
                "req_params": {
                    "text": text,
                    "speaker": self.config.tts['speaker'],
                    "audio_params": {
                        "format": self.config.tts['format'],
                        "sample_rate": self.config.tts['sample_rate']
                    }
                }
            }

            logger.info(f"TTS请求: '{text[:50]}...' " if len(text) > 50 else f"TTS请求: '{text}'")

            response = await session.post(
                self.config.tts['url'], headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            )

            if response.status == 200:
                audio_chunks = []
                async for line in response.content:
                    if not line:
                        continue
                    line_str = line.decode('utf-8').strip()
                    if not line_str:
                        continue
                    try:
                        data = json.loads(line_str)
                        code = data.get('code', -1)
                        if code == 0:
                            audio_b64 = data.get('data')
                            if audio_b64:
                                audio_chunks.append(base64.b64decode(audio_b64))
                        elif code == 20000000:
                            logger.info("TTS合成完成")
                        elif code != 0:
                            logger.error(f"TTS错误响应: {data}")
                    except json.JSONDecodeError:
                        pass
                await session.close()
                if audio_chunks:
                    result = b''.join(audio_chunks)
                    logger.info(f"TTS合成成功: {len(result)} bytes")
                    return result
                else:
                    logger.error("TTS合成失败 - 未返回音频数据")
                    return b''
            else:
                error_text = await response.text()
                logger.error(f"TTS请求失败 ({response.status}): {error_text}")
                await session.close()
                return b''
        except asyncio.TimeoutError:
            logger.error("TTS请求超时")
            if session and not session.closed:
                await session.close()
            return b''
        except Exception as e:
            logger.error(f"TTS合成失败: {e}")
            if session and not session.closed:
                await session.close()
            return b''


# ============ 音频播放器 ============

class AudioPlayer:
    def __init__(self):
        self._is_playing = False
        self._stop_event = threading.Event()
        self._play_thread: Optional[threading.Thread] = None

    def play(self, audio_data: bytes, sample_rate: int = 24000, blocking: bool = True):
        """播放音频。blocking=False 时异步播放 (支持 barge-in 打断)"""
        self._stop_event.clear()
        if blocking:
            self._play_sync(audio_data, sample_rate)
        else:
            self._play_thread = threading.Thread(
                target=self._play_sync, args=(audio_data, sample_rate), daemon=True
            )
            self._play_thread.start()

    def _play_sync(self, audio_data: bytes, sample_rate: int):
        try:
            import sounddevice as sd
            audio_array = self._decode_audio(audio_data, sample_rate)
            self._is_playing = True
            sd.play(audio_array, samplerate=sample_rate)
            # 轮询等待播放完成或被打断
            while sd.get_stream().active:
                if self._stop_event.is_set():
                    sd.stop()
                    logger.info("播放被打断 (barge-in)")
                    break
                time.sleep(0.05)
            self._is_playing = False
        except Exception as e:
            logger.error(f"播放失败: {e}")
            self._is_playing = False

    def wait(self):
        """等待异步播放结束"""
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=60)

    def _decode_audio(self, audio_data: bytes, target_sample_rate: int) -> np.ndarray:
        if len(audio_data) % 2 != 0:
            audio_data = audio_data[:len(audio_data) // 2 * 2]
        samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        if not samples.flags['C_CONTIGUOUS']:
            samples = np.ascontiguousarray(samples)
        return samples

    async def play_async(self, audio_data: bytes, sample_rate: int = 24000):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.play, audio_data, sample_rate)

    def stop(self):
        self._stop_event.set()
        try:
            import sounddevice as sd
            sd.stop()
            self._is_playing = False
        except Exception:
            pass

    @property
    def is_playing(self) -> bool:
        return self._is_playing


# ============ LLM 客户端 (V4 新增) ============

class LLMClient:
    """
    豆包 Seed 大模型客户端 (火山引擎 ARK API)
    使用 OpenAI Python SDK 兼容接口
    """

    def __init__(self, config: Config, model_id: str = None):
        self.config = config
        # model_id 优先级: 运行时传入 > config.yaml > DEFAULT_MODEL
        self.model_id = model_id or config.ark.get('model', DEFAULT_MODEL)
        self._client = None
        self._init_client()

    def _init_client(self):
        """初始化 OpenAI SDK 客户端"""
        try:
            from openai import OpenAI
            api_key = self.config.get_ark_api_key()
            if not api_key:
                logger.error(
                    "未找到 ARK API Key!\n"
                    "  方式1: 设置环境变量 ARK_API_KEY\n"
                    "  方式2: 在 config.yaml 的 ark.api_key 中填写"
                )
                return

            self._client = OpenAI(
                base_url=self.config.ark['base_url'],
                api_key=api_key,
            )
            logger.info(f"LLM客户端初始化完成 (模型: {self.model_id})")

        except ImportError:
            logger.error("openai 未安装，请运行: pip install openai")
        except Exception as e:
            logger.error(f"LLM客户端初始化失败: {e}")

    def is_available(self) -> bool:
        return self._client is not None

    def chat(self, user_text: str, history: List[Dict] = None) -> Tuple[str, List[Dict]]:
        """
        发送消息给大模型，返回 (回复文本, 更新后的对话历史)

        Args:
            user_text: 用户语音识别的文本
            history: 对话历史，格式为 [{"role": "user"/"assistant", "content": "..."}]

        Returns:
            (reply_text, updated_history)
        """
        if self._client is None:
            return "抱歉，大模型服务未就绪。", history or []

        if history is None:
            history = []

        # 构建消息列表
        messages = [{"role": "system", "content": self.config.ark['system_prompt']}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        try:
            logger.info(f"LLM请求: '{user_text[:60]}...' " if len(user_text) > 60
                        else f"LLM请求: '{user_text}'")
            t0 = time.time()

            response = self._client.chat.completions.create(
                model=self.model_id,
                messages=messages,
                max_tokens=self.config.ark['max_tokens'],
            )

            reply = response.choices[0].message.content.strip()
            elapsed = time.time() - t0
            logger.info(f"LLM回复 ({elapsed:.1f}s): '{reply[:80]}...' " if len(reply) > 80
                        else f"LLM回复 ({elapsed:.1f}s): '{reply}'")

            # 更新对话历史
            max_turns = self.config.ark.get('max_history_turns', 10)
            new_history = history + [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": reply},
            ]
            # 保留最近 max_turns 轮 (每轮2条消息)
            if len(new_history) > max_turns * 2:
                new_history = new_history[-(max_turns * 2):]

            return reply, new_history

        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            error_msg = "抱歉，我暂时无法回应，请稍后再试。"
            return error_msg, history


# ============ 唤醒词检测器 (V4: 支持动态模型选择) ============

class WakeWordDetector:
    """唤醒词检测器 - 支持 hey_kee_dah / hey_jarvis"""

    def __init__(self, config: Config, wakeword: str = DEFAULT_WAKEWORD):
        self.config = config

        # 解析唤醒词配置
        if wakeword not in WAKEWORD_OPTIONS:
            logger.warning(f"未知唤醒词 '{wakeword}'，使用默认: {DEFAULT_WAKEWORD}")
            wakeword = DEFAULT_WAKEWORD
        self.wakeword_name = wakeword
        self.wakeword_info = WAKEWORD_OPTIONS[wakeword]
        self.prediction_key = self.wakeword_info["prediction_key"]

        self.model = None
        self._prediction_history: Deque[float] = deque(
            maxlen=config.wakeword['smoothing_window']
        )
        self._trigger_time = 0
        self._consecutive_frames = 0
        self._init_model()

    def _init_model(self):
        """根据选择的唤醒词初始化模型"""
        try:
            import openwakeword
            from openwakeword.model import Model

            if self.wakeword_info['is_custom']:
                # 自定义 ONNX 模型
                model_path = self.wakeword_info['model_path']
                if not Path(model_path).exists():
                    logger.error(f"自定义唤醒词模型不存在: {model_path}")
                    logger.error("请先完成训练或检查路径")
                    return

                logger.info(f"加载自定义唤醒词模型: {model_path}")
                self.model = Model(
                    wakeword_models=[model_path],
                    enable_speex_noise_suppression=self.config.wakeword['enable_speex_noise_suppression'],
                    vad_threshold=self.config.wakeword['vad_threshold'],
                    inference_framework="onnx"
                )
                logger.info(f"唤醒词模型就绪: {self.wakeword_info['display_name']}")

            else:
                # 内置模型 (hey_jarvis 等)
                logger.info("下载/加载内置唤醒词模型...")
                openwakeword.utils.download_models()
                self.model = Model(
                    wakeword_models=[self.wakeword_name],
                    enable_speex_noise_suppression=self.config.wakeword['enable_speex_noise_suppression'],
                    vad_threshold=self.config.wakeword['vad_threshold'],
                )
                logger.info(f"唤醒词模型就绪: {self.wakeword_info['display_name']}")

        except ImportError as e:
            logger.error(f"openWakeWord未安装: {e}")
        except Exception as e:
            logger.error(f"模型初始化失败: {e}")
            import traceback
            traceback.print_exc()

    def predict(self, audio_data: np.ndarray) -> float:
        """预测唤醒词分数 (带平滑)"""
        if self.model is None:
            return 0.0
        predictions_raw = self.model.predict(audio_data)
        if isinstance(predictions_raw, tuple):
            predictions = predictions_raw[0] if predictions_raw else {}
        else:
            predictions = predictions_raw

        if not isinstance(predictions, dict):
            score = 0.0
        else:
            score = predictions.get(self.prediction_key)
        if score is None:
            # 自定义模型在 output key 变化时，自动回退到当前模型唯一输出
            if isinstance(predictions, dict) and self.wakeword_info.get("is_custom") and len(predictions) == 1:
                score = float(next(iter(predictions.values())))
            else:
                score = 0.0
        self._prediction_history.append(score)
        if len(self._prediction_history) < 2:
            return score
        weights = np.linspace(0.5, 1.0, len(self._prediction_history))
        weights = weights / weights.sum()
        return float(np.average(list(self._prediction_history), weights=weights))

    def check_detection(self, audio_data: np.ndarray) -> bool:
        """检查是否检测到唤醒词"""
        if self.model is None:
            return False
        smooth_score = self.predict(audio_data)
        threshold = self.config.wakeword['threshold']
        current_time = time.time()
        if smooth_score >= threshold:
            self._consecutive_frames += 1
        else:
            self._consecutive_frames = 0
        if self._consecutive_frames < self.config.wakeword['patience']:
            return False
        if current_time - self._trigger_time < self.config.wakeword['debounce_time']:
            return False
        self._trigger_time = current_time
        self._consecutive_frames = 0
        return True

    def reset(self):
        self._consecutive_frames = 0

    def get_score(self) -> float:
        if self._prediction_history:
            return self._prediction_history[-1]
        return 0.0


# ============ 音频采集器 ============

class AudioCapture:
    def __init__(self, config: Config):
        self.config = config
        self.stream = None
        self.backend = None
        self._callback = None

    @staticmethod
    def get_available_backend() -> str:
        try:
            import sounddevice as sd
            return "sounddevice"
        except ImportError:
            pass
        try:
            import pyaudio
            return "pyaudio"
        except ImportError:
            pass
        return None

    def init(self, callback: callable) -> bool:
        self._callback = callback
        backend = self.get_available_backend()
        if backend == "sounddevice":
            return self._init_sounddevice()
        elif backend == "pyaudio":
            return self._init_pyaudio()
        else:
            logger.error("未找到可用的音频库! 请安装: pip install sounddevice")
            return False

    def _init_sounddevice(self) -> bool:
        try:
            import sounddevice as sd

            def sd_callback(indata, frames, time_info, status):
                if status:
                    logger.debug(f"音频状态: {status}")
                audio_data = (indata[:, 0] * 32767).astype(np.int16)
                if self._callback:
                    self._callback(audio_data)

            self.stream = sd.InputStream(
                channels=self.config.audio['channels'],
                samplerate=self.config.audio['rate'],
                dtype='float32',
                blocksize=self.config.audio['chunk'],
                callback=sd_callback
            )
            self.backend = "sounddevice"
            logger.info("使用sounddevice音频后端")
            return True
        except Exception as e:
            logger.error(f"sounddevice初始化失败: {e}")
            return False

    def _init_pyaudio(self) -> bool:
        try:
            import pyaudio

            def pa_callback(in_data, frame_count, time_info, status):
                audio_data = np.frombuffer(in_data, dtype=np.int16)
                if self._callback:
                    self._callback(audio_data)
                return (in_data, pyaudio.paContinue)

            pa = pyaudio.PyAudio()
            self.stream = pa.open(
                format=pyaudio.paInt16,
                channels=self.config.audio['channels'],
                rate=self.config.audio['rate'],
                input=True,
                frames_per_buffer=self.config.audio['chunk'],
                stream_callback=pa_callback,
            )
            self.backend = "pyaudio"
            logger.info("使用PyAudio音频后端")
            return True
        except Exception as e:
            logger.error(f"PyAudio初始化失败: {e}")
            return False

    def start(self):
        if self.stream:
            self.stream.start()

    def stop(self):
        if self.stream:
            if self.backend == "sounddevice":
                self.stream.stop()
                self.stream.close()
            elif self.backend == "pyaudio":
                self.stream.stop_stream()
                self.stream.close()


# ============ 应用状态 ============

class AppState(Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"

    def __str__(self):
        return self.value


# ============ 语音助手 V4 ============

class VoiceAssistant:
    """
    语音助手 V4
    唤醒词 → ASR → LLM (seed-2.0-mini) → TTS
    支持: 降噪 / barge-in 打断 / 连续对话
    """

    def __init__(self, config_path: str = "config.yaml", wakeword: str = DEFAULT_WAKEWORD,
                 model_id: str = None, vad_overrides: Optional[Dict] = None):
        self.config = Config(config_path)

        # 应用 VAD 覆盖 (来自交互式配置)
        if vad_overrides:
            for k, v in vad_overrides.items():
                if v is None:
                    continue
                if k == 'max_duration':
                    self.config.config['recording']['max_duration'] = v
                elif k in ('continuous_mode', 'barge_in', 'continuous_timeout'):
                    self.config.config['conversation'][k] = v
                else:
                    self.config.vad[k] = v

        self.state = AppState.IDLE
        self.wakeword_name = wakeword
        self.model_id = model_id or DEFAULT_MODEL

        # 核心组件
        self.wakeword_detector = WakeWordDetector(self.config, wakeword)
        self.vad = VAD(
            threshold=self.config.vad['threshold'],
            silence_duration=self.config.vad['silence_duration'],
            smooth_frames=self.config.vad.get('smooth_frames', 4),
        )
        self.noise_suppressor = NoiseSuppressor(
            sample_rate=self.config.audio['rate'],
            enabled=self.config.vad.get('noise_suppress', True),
        )
        self.asr_client = None
        self.tts_client = DoubaoTTSClient(self.config)
        self.audio_player = AudioPlayer()
        self.llm_client = LLMClient(self.config, model_id=self.model_id)

        # 对话历史
        self.conversation_history: List[Dict] = []

        # 缓冲区 (流式模式下仅用于降噪暂存, 不再用于批量 ASR)
        self.audio_buffer = AudioRingBuffer(
            max_duration=self.config.recording['max_duration']
        )
        self.audio_buffer.sample_rate = self.config.audio['rate']

        # 音频采集
        self.audio_capture = AudioCapture(self.config)

        # 控制
        self._running = False
        self._lock = threading.Lock()
        self._state_change_time = time.time()
        self._listening_start_time = None
        self._max_listening_duration = self.config.recording.get('max_duration', 30)
        self._no_speech_timeout = self.config.vad.get('no_speech_timeout', 5.0)

        # 流式 ASR 会话
        self._streaming_session: Optional[StreamingASRSession] = None
        self._asr_final_text = ""           # 最终识别结果
        self._asr_final_event = threading.Event()  # 收到最终结果时 set
        self._asr_partial_text = ""         # 实时中间结果 (用于状态栏显示)
        self._last_speech_sent = False      # 是否已发送 finish 到 ASR

        # 连续对话
        self._continuous_mode = self.config.config.get('conversation', {}).get('continuous_mode', True)
        self._continuous_timeout = self.config.config.get('conversation', {}).get('continuous_timeout', 8.0)
        self._barge_in = self.config.config.get('conversation', {}).get('barge_in', True)

        # 统计
        self.metrics = {
            "detections": 0,
            "asr_calls": 0,
            "llm_calls": 0,
            "tts_calls": 0,
        }

        # 回调
        self.on_asr_result = None
        self.on_llm_result = None
        self.on_tts_start = None
        self.on_tts_end = None

    def _audio_callback(self, audio_data: np.ndarray):
        """音频回调 — 降噪 → 唤醒词 / 流式ASR+VAD / barge-in 分发"""
        # 实时降噪
        clean_audio = self.noise_suppressor.process(audio_data)

        with self._lock:
            current_state = self.state

        if current_state == AppState.IDLE:
            if self.wakeword_detector.check_detection(clean_audio):
                ww_info = WAKEWORD_OPTIONS[self.wakeword_name]
                logger.info("=" * 50)
                logger.info(f"检测到唤醒词: {ww_info['display_name']}")
                logger.info("=" * 50)

                self.metrics["detections"] += 1
                self.audio_buffer.clear()
                self.vad.reset()
                self._start_streaming_asr()
                self._set_state(AppState.LISTENING)
                self._listening_start_time = time.time()
                self._last_speech_sent = False
                logger.info("进入流式录音+识别，请说话...")

        elif current_state == AppState.LISTENING:
            # 1. 同步喂音频给 VAD 和流式 ASR
            self.vad.process(clean_audio)
            if self._streaming_session and self._streaming_session.is_active:
                self._streaming_session.feed_audio(clean_audio)

            elapsed = time.time() - self._listening_start_time

            # 2. 判断是否应该结束录音
            if not self._last_speech_sent:
                # 条件 A: VAD 检测到说话后静音 → 发送最后一包给 ASR
                if self.vad.has_speech_stopped():
                    logger.info(f"端点检测: 说话结束 (静音 {self.vad.get_silence_duration():.1f}s)")
                    self._finish_streaming_asr()

                # 条件 B: 最长录音时间
                elif elapsed >= self._max_listening_duration:
                    logger.info(f"端点检测: 录音超时 ({elapsed:.1f}s)")
                    self._finish_streaming_asr()

                # 条件 C: 无语音超时 → 直接回 IDLE, 不走 pipeline
                elif elapsed >= self._no_speech_timeout and not self.vad.is_speaking():
                    logger.info(f"无语音超时 ({elapsed:.1f}s), 回到待唤醒状态")
                    self._abort_streaming_asr()
                    self.vad.reset()
                    self._set_state(AppState.IDLE)

        elif current_state == AppState.SPEAKING and self._barge_in:
            # Barge-in: 播放中检测唤醒词 → 打断并立即进入录音
            if self.wakeword_detector.check_detection(clean_audio):
                logger.info("=" * 50)
                logger.info("Barge-in: 播放中检测到唤醒词, 打断播放!")
                logger.info("=" * 50)
                self.audio_player.stop()
                self.metrics["detections"] += 1
                self.audio_buffer.clear()
                self.vad.reset()
                self._start_streaming_asr()
                self._set_state(AppState.LISTENING)
                self._listening_start_time = time.time()
                self._last_speech_sent = False
                logger.info("进入流式录音+识别，请说话...")

    # ---------- 流式 ASR 控制方法 ----------

    def _start_streaming_asr(self):
        """创建并启动一个新的流式 ASR 会话。"""
        # 清理旧会话
        if self._streaming_session and self._streaming_session.is_active:
            self._streaming_session.abort()
        self._asr_final_text = ""
        self._asr_partial_text = ""
        self._asr_final_event.clear()

        self._streaming_session = StreamingASRSession(
            self.config,
            on_partial=self._on_asr_partial,
            on_final=self._on_asr_final,
            on_error=self._on_asr_error,
            end_window_size=800,
            force_to_speech_time=1000,
        )
        self._streaming_session.start()
        logger.info("流式ASR会话已启动")

    def _finish_streaming_asr(self):
        """通知流式 ASR 发送最后一包并等待最终结果。"""
        # 使用 _lock 防止与 _on_asr_final (服务端判停) 竞争
        with self._lock:
            if self._last_speech_sent:
                return  # 服务端已经先行判停并启动了 pipeline
            self._last_speech_sent = True
        if self._streaming_session and self._streaming_session.is_active:
            # 在后台线程中执行 finish + 等待，避免阻塞音频回调
            threading.Thread(target=self._finish_and_process, daemon=True).start()

    def _abort_streaming_asr(self):
        """强制中止流式 ASR 会话。"""
        if self._streaming_session:
            self._streaming_session.abort()
        self._streaming_session = None

    def _finish_and_process(self):
        """后台线程: 等待 ASR 最终结果, 然后启动 pipeline。"""
        try:
            if self._streaming_session:
                self._streaming_session.finish(timeout=8.0)

            # 等待最终结果回调
            got_final = self._asr_final_event.wait(timeout=5.0)

            asr_text = self._asr_final_text.strip()
            if not asr_text:
                # 回退: 即使没有 definite, 也尝试用最新的中间结果
                asr_text = (self._streaming_session.latest_text if self._streaming_session else "").strip()

            self._streaming_session = None

            if not asr_text:
                logger.warning("流式ASR: 未识别到语音内容")
                self.vad.reset()
                self._set_state(AppState.IDLE)
                return

            self.metrics["asr_calls"] += 1
            self._set_state(AppState.PROCESSING)
            self._process_pipeline_with_text(asr_text)

        except Exception as e:
            logger.error(f"_finish_and_process 异常: {e}")
            import traceback
            traceback.print_exc()
            self.vad.reset()
            self._set_state(AppState.IDLE)

    def _on_asr_partial(self, text: str):
        """流式 ASR 中间结果回调 (在 StreamingASR 线程中调用)。"""
        self._asr_partial_text = text
        logger.debug(f"ASR中间结果: {text}")

    def _on_asr_final(self, text: str):
        """流式 ASR 最终结果回调 (definite / last_package)。
        
        可能由两个路径触发:
        1. 服务端 end_window_size 判停 → definite=true (更快, 800ms 静音)
        2. 本地 VAD 判停 → finish() → last_package
        
        如果是路径1且本地尚未调用 _finish_streaming_asr,
        则由服务端判停直接驱动 pipeline。
        """
        logger.info(f"ASR最终结果: {text}")
        self._asr_final_text = text
        self._asr_final_event.set()

        # 使用 _lock 防止与 _finish_streaming_asr 竞争
        with self._lock:
            if self._last_speech_sent:
                return  # 本地 VAD 已经触发了 _finish_and_process
            self._last_speech_sent = True

        # 服务端先判停 → 直接驱动 pipeline
        logger.info("服务端端点检测先于本地 VAD, 直接进入 pipeline")
        if self._streaming_session and self._streaming_session.is_active:
            self._streaming_session.abort()
        self._streaming_session = None
        self.metrics["asr_calls"] += 1
        self._set_state(AppState.PROCESSING)
        threading.Thread(
            target=self._process_pipeline_with_text,
            args=(text,), daemon=True
        ).start()

    def _on_asr_error(self, error: str):
        """流式 ASR 错误回调。"""
        logger.error(f"流式ASR错误: {error}")
        self._asr_final_event.set()  # 解除等待

    def _set_state(self, state: AppState):
        with self._lock:
            self.state = state
            self._state_change_time = time.time()
        logger.debug(f"状态变更: {state}")

    def _process_pipeline_with_text(self, asr_result: str):
        """V4 核心流水线 (流式版): 接收 ASR 文本 → LLM → TTS"""
        try:
            logger.info("-" * 50)
            logger.info(f"[用户] {asr_result}")

            if self.on_asr_result:
                self.on_asr_result(asr_result)

            # 步骤1: LLM
            if self.llm_client.is_available():
                llm_text, self.conversation_history = self.llm_client.chat(
                    asr_result, self.conversation_history
                )
                self.metrics["llm_calls"] += 1
                logger.info(f"[助手] {llm_text}")
                logger.info("-" * 50)

                if self.on_llm_result:
                    self.on_llm_result(llm_text)
            else:
                # LLM 不可用时，直接 echo ASR 结果
                logger.warning("LLM不可用，播放ASR识别结果")
                llm_text = asr_result

            # 步骤2: TTS
            self._process_tts(llm_text)

        except Exception as e:
            logger.error(f"流水线处理失败: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.vad.reset()
            self.audio_buffer.clear()
            with self._lock:
                if self.state == AppState.PROCESSING:
                    self._set_state(AppState.IDLE)

    def _process_tts(self, text: str):
        """TTS合成并播放"""
        try:
            self._set_state(AppState.SPEAKING)
            if self.on_tts_start:
                self.on_tts_start()

            result = [None]
            exception = [None]

            def run_tts():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        result[0] = loop.run_until_complete(self.tts_client.synthesize(text))
                    finally:
                        pending = asyncio.all_tasks(loop)
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                        loop.close()
                except Exception as e:
                    exception[0] = e

            thread = threading.Thread(target=run_tts)
            thread.start()
            thread.join(timeout=35)

            if exception[0]:
                raise exception[0]

            audio_data = result[0]
            if audio_data:
                self.metrics["tts_calls"] += 1
                # 非阻塞播放 — 允许 barge-in 打断
                self.audio_player.play(
                    audio_data, self.config.tts['sample_rate'],
                    blocking=not self._barge_in
                )
                if self._barge_in:
                    self.audio_player.wait()  # 等播完或被打断

                # 播放完成后判断: 如果已被 barge-in 打断, state 已变为 LISTENING
                with self._lock:
                    still_speaking = (self.state == AppState.SPEAKING)
                if still_speaking:
                    logger.info("播放完成")
            else:
                logger.error("TTS合成失败")

        except Exception as e:
            logger.error(f"TTS处理失败: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if self.on_tts_end:
                self.on_tts_end()
            with self._lock:
                current = self.state
            if current == AppState.SPEAKING:
                # 未被打断 → 进入连续对话或回到 IDLE
                if self._continuous_mode:
                    logger.info(f"连续对话: 等待语音输入 (超时 {self._continuous_timeout}s) ...")
                    self.vad.reset()
                    self.audio_buffer.clear()
                    self._start_streaming_asr()
                    self._set_state(AppState.LISTENING)
                    self._listening_start_time = time.time()
                    self._last_speech_sent = False
                else:
                    self._set_state(AppState.IDLE)

    def clear_history(self):
        """清空对话历史"""
        self.conversation_history = []
        logger.info("对话历史已清空")

    def run(self):
        """运行语音助手"""
        ww_info = WAKEWORD_OPTIONS[self.wakeword_name]

        print("=" * 65)
        print("  语音交互 Demo V4  (唤醒词 + ASR + LLM + TTS)")
        print("=" * 65)

        # 检查 LLM 配置
        api_key = self.config.get_ark_api_key()
        if api_key:
            print(f"\n[OK] ARK API Key 已配置 ({api_key[:8]}...)")
        else:
            print("\n[警告] ARK API Key 未配置！LLM 功能将不可用")
            print("  - 设置环境变量: set ARK_API_KEY=your_key")
            print("  - 或在 config.yaml 的 ark.api_key 中填写")

        # 检测音频库
        backend = AudioCapture.get_available_backend()
        if backend:
            print(f"[OK] 音频库: {backend}")
        else:
            print("[错误] 未找到可用的音频库!")
            return

        # 初始化音频采集
        if not self.audio_capture.init(self._audio_callback):
            return

        self.audio_capture.start()

        print("\n" + "=" * 65)
        model_display = MODEL_OPTIONS.get(self.model_id, {}).get('display_name', self.model_id)
        print(f"  唤醒词:     {ww_info['display_name']}")
        print(f"  大模型:     {model_display}")
        print(f"  TTS发音人:  {self.config.tts['speaker']}")
        print(f"  ASR模式:    流式识别 (边说边传边识别)")
        print(f"  ASR端点:    end_window_size=800ms, force_to_speech_time=1000ms")
        print(f"  VAD阈值:    {self.config.vad['threshold']}")
        print(f"  静音判停:   {self.config.vad['silence_duration']}秒")
        print(f"  帧平滑:     {self.config.vad.get('smooth_frames', 4)} 帧")
        print(f"  无语音超时: {self._no_speech_timeout}秒 (无输入直接回 IDLE)")
        print(f"  最长录音:   {self.config.recording['max_duration']}秒")
        print(f"  降噪:       {'开启' if self.noise_suppressor.enabled else '关闭'}")
        print(f"  连续对话:   {'开启' if self._continuous_mode else '关闭'} (超时 {self._continuous_timeout}s)")
        print(f"  播放打断:   {'开启' if self._barge_in else '关闭'}")
        print(f"  对话轮次:   最多{self.config.ark['max_history_turns']}轮")
        print("=" * 65)
        print("\n按 Ctrl+C 退出 | 按 R 键清空对话历史\n")

        self._running = True

        # 键盘监听线程
        def keyboard_listener():
            try:
                import msvcrt
                while self._running:
                    if msvcrt.kbhit():
                        key = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                        if key == 'r':
                            self.clear_history()
                            print("\n[对话历史已清空]\n")
                    time.sleep(0.1)
            except Exception:
                pass

        kb_thread = threading.Thread(target=keyboard_listener, daemon=True)
        kb_thread.start()

        # 状态显示线程
        def status_display():
            while self._running:
                with self._lock:
                    state = self.state
                    score = self.wakeword_detector.get_score()

                score_bar = "#" * int(score * 20) + "-" * (20 - int(score * 20))
                hist_count = len(self.conversation_history) // 2

                # 流式 ASR 实时文本 (LISTENING 时显示)
                asr_hint = ""
                if state == AppState.LISTENING and self._asr_partial_text:
                    partial = self._asr_partial_text[-30:]  # 最多显示后30字
                    asr_hint = f" | ASR: {partial}"

                print(f"\r[{state}] WakeWord:[{score_bar}]{score:.3f} | "
                      f"检测:{self.metrics['detections']} "
                      f"ASR:{self.metrics['asr_calls']} "
                      f"LLM:{self.metrics['llm_calls']} "
                      f"TTS:{self.metrics['tts_calls']} "
                      f"对话:{hist_count}轮{asr_hint}      ",
                      end="", flush=True)
                time.sleep(0.1)

        status_thread = threading.Thread(target=status_display, daemon=True)
        status_thread.start()

        try:
            while self._running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n\n用户中断")
        finally:
            self._cleanup()

    def _cleanup(self):
        self._running = False
        # 停止流式 ASR 会话
        if self._streaming_session and self._streaming_session.is_active:
            self._streaming_session.abort()
        self._streaming_session = None
        self.audio_capture.stop()
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.tts_client.close())
            loop.close()
        except:
            pass
        print("\n资源已清理")


# ============ 入口 ============

def parse_args():
    import argparse
    parser = argparse.ArgumentParser(
        description="语音助手 V4 - 唤醒词 + ASR + LLM + TTS",
        add_help=True,
    )
    parser.add_argument(
        '--wakeword',
        default=None,
        choices=list(WAKEWORD_OPTIONS.keys()),
        help='跳过交互菜单直接指定唤醒词'
    )
    parser.add_argument(
        '--model',
        default=None,
        choices=list(MODEL_OPTIONS.keys()),
        help='跳过交互菜单直接指定大模型 ID'
    )
    parser.add_argument(
        '--config',
        default='config.yaml',
        help='配置文件路径 (默认: config.yaml)'
    )
    return parser.parse_args()


def select_wakeword_interactive(preselected: str = None) -> str:
    """
    交互式唤醒词选择菜单。
    若命令行已通过 --wakeword 指定则跳过。
    默认选项为 hey_kee_dah，直接回车即确认。
    """
    options = list(WAKEWORD_OPTIONS.items())   # 保持顺序

    if preselected is not None:
        # 命令行已指定，直接返回，不弹菜单
        return preselected

    print()
    print("╔" + "═" * 45 + "╗")
    print("║       请选择唤醒词 (Wake Word)          ║")
    print("╠" + "═" * 45 + "╣")
    for idx, (key, info) in enumerate(options, 1):
        default_tag = "  [默认]" if key == DEFAULT_WAKEWORD else ""
        print(f"║  {idx}. {info['display_name']:<32}{default_tag} ║")
    print("╚" + "═" * 45 + "╝")
    print(f"直接回车 = 使用默认 ({WAKEWORD_OPTIONS[DEFAULT_WAKEWORD]['display_name']})")
    print()

    while True:
        try:
            raw = input("请输入选项编号 [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return DEFAULT_WAKEWORD

        # 空输入 = 回车 = 默认
        if raw == "":
            chosen = DEFAULT_WAKEWORD
            break

        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                chosen = options[idx - 1][0]
                break

        print(f"  无效输入，请输入 1~{len(options)} 之间的数字，或直接回车。")

    print(f"  已选择: {WAKEWORD_OPTIONS[chosen]['display_name']}")
    print()
    return chosen


def select_model_interactive(preselected: str = None) -> str:
    """
    交互式大模型选择菜单。
    若命令行已通过 --model 指定则跳过。
    默认选项为 DEFAULT_MODEL，直接回车即确认。
    """
    options = list(MODEL_OPTIONS.items())

    if preselected is not None:
        return preselected

    print()
    print("╔" + "═" * 55 + "╗")
    print("║         请选择大模型 (LLM Model)              ║")
    print("╠" + "═" * 55 + "╣")
    for idx, (key, info) in enumerate(options, 1):
        default_tag = "  [默认]" if key == DEFAULT_MODEL else ""
        print(f"║  {idx}. {info['display_name']:<42}{default_tag} ║")
    print("╚" + "═" * 55 + "╝")
    default_display = MODEL_OPTIONS[DEFAULT_MODEL]['display_name'].split()[0]
    print(f"直接回车 = 使用默认 ({default_display})")
    print()

    while True:
        try:
            raw = input("请输入选项编号 [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return DEFAULT_MODEL

        if raw == "":
            chosen = DEFAULT_MODEL
            break

        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                chosen = options[idx - 1][0]
                break

        print(f"  无效输入，请输入 1~{len(options)} 之间的数字，或直接回车。")

    print(f"  已选择: {MODEL_OPTIONS[chosen]['display_name']}")
    print()
    return chosen


def _input_float(prompt: str, default: float, low: float, high: float) -> float:
    """辅助: 读取浮点数, 范围校验"""
    while True:
        try:
            raw = input(prompt).strip()
            if raw == "":
                return default
            val = float(raw)
            if low <= val <= high:
                return val
            print(f"  请输入 {low}~{high} 之间的数值")
        except (ValueError, EOFError, KeyboardInterrupt):
            return default


def _input_int(prompt: str, default: int, low: int, high: int) -> int:
    """辅助: 读取整数, 范围校验"""
    while True:
        try:
            raw = input(prompt).strip()
            if raw == "":
                return default
            val = int(raw)
            if low <= val <= high:
                return val
            print(f"  请输入 {low}~{high} 之间的整数")
        except (ValueError, EOFError, KeyboardInterrupt):
            return default


def _input_bool(prompt: str, default: bool) -> bool:
    """辅助: 读取是/否"""
    tag = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"{prompt} [{tag}]: ").strip().lower()
            if raw == "":
                return default
            if raw in ("y", "yes", "是"):
                return True
            if raw in ("n", "no", "否"):
                return False
            print("  请输入 y 或 n")
        except (EOFError, KeyboardInterrupt):
            return default


def configure_vad_interactive() -> Dict:
    """
    交互式 VAD & 录音参数配置。
    返回 vad_overrides dict, 传入 VoiceAssistant 构造函数。
    """
    presets = list(VAD_PRESETS.items())

    print()
    print("╔" + "═" * 55 + "╗")
    print("║       语音检测参数配置 (VAD & 录音)            ║")
    print("╠" + "═" * 55 + "╣")
    for idx, (key, info) in enumerate(presets, 1):
        default_tag = "  [推荐]" if key == DEFAULT_VAD_PRESET else ""
        print(f"║  {idx}. {info['display_name']:<38}{default_tag} ║")
    print("╚" + "═" * 55 + "╝")
    default_idx = list(VAD_PRESETS.keys()).index(DEFAULT_VAD_PRESET) + 1
    print(f"直接回车 = 使用推荐 ({DEFAULT_VAD_PRESET})")
    print()

    chosen_key = DEFAULT_VAD_PRESET
    while True:
        try:
            raw = input(f"请输入选项编号 [{default_idx}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if raw == "":
            break
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(presets):
                chosen_key = presets[idx - 1][0]
                break
        print(f"  无效输入，请输入 1~{len(presets)} 之间的数字。")

    preset = VAD_PRESETS[chosen_key]
    print(f"  已选择: {preset['display_name']}")

    overrides: Dict = {}

    if chosen_key == "自定义":
        # 手动配置每个参数
        print()
        print("  ── 自定义 VAD 参数 ──")
        overrides['threshold'] = _input_float(
            "  VAD 语音阈值 (0.15~0.80) [0.35]: ", 0.35, 0.15, 0.80)
        overrides['silence_duration'] = _input_float(
            "  说话后静音判停 (1.0~6.0s) [2.5]: ", 2.5, 1.0, 6.0)
        overrides['no_speech_timeout'] = _input_float(
            "  无语音超时 (3.0~15.0s) [5.0]: ", 5.0, 3.0, 15.0)
        overrides['smooth_frames'] = _input_int(
            "  帧平滑窗口 (1~10 帧) [4]: ", 4, 1, 10)
        overrides['noise_suppress'] = _input_bool(
            "  启用降噪?", True)
    else:
        # 使用预设值
        overrides['threshold'] = preset['threshold']
        overrides['silence_duration'] = preset['silence_duration']
        overrides['no_speech_timeout'] = preset['no_speech_timeout']
        overrides['smooth_frames'] = preset['smooth_frames']
        overrides['noise_suppress'] = preset['noise_suppress']

    # 通用选项
    print()
    overrides['max_duration'] = _input_float(
        f"  最长录音时长 (10~120s) [50]: ", 50, 10, 120)
    continuous = _input_bool("  启用连续对话? (TTS播完自动继续监听)", True)
    barge_in = _input_bool("  启用播放打断? (说唤醒词可打断TTS播放)", True)
    overrides['continuous_mode'] = continuous
    overrides['barge_in'] = barge_in

    print()
    print("  ── 配置汇总 ──")
    print(f"    VAD阈值:     {overrides['threshold']}")
    print(f"    静音判停:    {overrides['silence_duration']}s")
    print(f"    无语音超时:  {overrides['no_speech_timeout']}s")
    print(f"    帧平滑:      {overrides['smooth_frames']} 帧")
    print(f"    降噪:        {'开启' if overrides['noise_suppress'] else '关闭'}")
    print(f"    最长录音:    {overrides['max_duration']}s")
    print(f"    连续对话:    {'开启' if continuous else '关闭'}")
    print(f"    播放打断:    {'开启' if barge_in else '关闭'}")
    print()

    return overrides


if __name__ == "__main__":
    args = parse_args()

    print("=" * 65)
    print("  语音助手 V4  启动向导")
    print("=" * 65)

    # 第一步: 唤醒词选择
    wakeword = select_wakeword_interactive(args.wakeword)

    # 第二步: 大模型选择
    model_id = select_model_interactive(args.model)

    # 第三步: VAD & 录音参数配置
    vad_overrides = configure_vad_interactive()

    print(f"  启动配置:")
    print(f"    唤醒词: {WAKEWORD_OPTIONS[wakeword]['display_name']}")
    print(f"    大模型: {MODEL_OPTIONS[model_id]['display_name']}")
    print(f"    配置:   {args.config}")
    print()

    assistant = VoiceAssistant(
        config_path=args.config,
        wakeword=wakeword,
        model_id=model_id,
        vad_overrides=vad_overrides,
    )
    assistant.run()
