import subprocess
import re
import time
from typing import List, Dict


class AudioManager:
    """基于 pactl（PulseAudio CLI）的音频设备管理。

    容器内 WirePlumber 在无 system dbus + 无 logind 时会 crash-loop，
    因此改用 pipewire-pulse 暴露的 PulseAudio 兼容接口 + pactl 管理。
    """

    # list sinks 短 TTL 缓存：前端 3 秒轮询一次，每次实际要起 2 个 pactl
    # （list sinks + get-default-sink），音量滑块的连续渲染不需要更高频。
    # 任何写操作（切默认/音量/静音）后立即失效。
    SINKS_TTL = 2.0

    def __init__(self):
        self._sinks_cache = {"at": 0.0, "data": []}

    def invalidate_sinks(self) -> None:
        """失效 sink 列表缓存（任何 pactl 写操作后调用）。"""
        self._sinks_cache["at"] = 0.0

    def list_sinks(self, force: bool = False) -> List[Dict]:
        """调用 `pactl list sinks` 解析音频输出设备列表。

        典型输出片段：
            Sink #0
                State: RUNNING
                Name: alsa_output.pci-0000_00_1b.0.analog-stereo
                Description: Internal Audio
                Mute: no
                Volume: front-left: 65536 / 100% / 0.00 dB,
                        front-right: 65536 / 100% / 0.00 dB
                ...

        每项附带：
            volume  0~100 整数（取前左声道百分比，立体声两侧通常一致）
            muted   是否静音（Mute: yes）
        """
        now = time.time()
        if not force and self._sinks_cache["at"] and \
                now - self._sinks_cache["at"] < self.SINKS_TTL:
            return [dict(s) for s in self._sinks_cache["data"]]
        try:
            res = subprocess.check_output(
                ["pactl", "list", "sinks"], encoding="utf-8", timeout=3,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return []

        sinks: List[Dict] = []
        current: Dict = {}
        default_name = self._get_default_sink_name()

        def _commit():
            if current.get("name"):
                sinks.append(current)

        for line in res.splitlines():
            stripped = line.strip()
            if stripped.startswith("Sink #"):
                _commit()
                current = {"id": "", "name": "", "desc": "",
                           "is_default": False, "volume": -1, "muted": False}
            elif not current:
                continue
            elif stripped.startswith("Name:"):
                current["name"] = stripped.split(":", 1)[1].strip()
                current["is_default"] = current["name"] == default_name
                if not current.get("id"):
                    current["id"] = current["name"]
            elif stripped.startswith("Description:"):
                current["desc"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Mute:"):
                current["muted"] = stripped.split(":", 1)[1].strip().lower() == "yes"
            elif stripped.startswith("Volume:"):
                # 形如 "front-left: 65536 / 100% / 0.00 dB, ..."，
                # 取行内第一个百分比即可（多声道行只有第一条与设备相关）
                m = re.search(r"/\s*(\d{1,3})\s*%", stripped)
                if m:
                    current["volume"] = max(0, min(100, int(m.group(1))))

        _commit()
        self._sinks_cache["at"] = now
        self._sinks_cache["data"] = [dict(s) for s in sinks]
        return sinks

    def _get_default_sink_name(self) -> str:
        """获取当前默认 sink 名称。"""
        try:
            res = subprocess.check_output(
                ["pactl", "get-default-sink"], encoding="utf-8", timeout=3,
                stderr=subprocess.DEVNULL,
            )
            return res.strip()
        except Exception:
            return ""

    def set_default_sink(self, sink_id: str) -> bool:
        """sink_id 是 pactl list sinks 输出的 Name 字段。"""
        try:
            subprocess.run(
                ["pactl", "set-default-sink", sink_id],
                timeout=3,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self.invalidate_sinks()
            return True
        except Exception:
            return False

    def set_sink_volume(self, sink_id: str, volume: int) -> bool:
        """设置 sink 音量（0~100）。

        pactl 接受原生百分比（set-sink-volume NAME 50%），
        无需自己做 65536 线性换算；越界值在这里钳一次，
        curl/脚本直接调 API 也不会把声卡设到异常状态。
        """
        try:
            volume = max(0, min(100, int(volume)))
        except (TypeError, ValueError):
            return False
        try:
            subprocess.run(
                ["pactl", "set-sink-volume", sink_id, f"{volume}%"],
                timeout=3,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self.invalidate_sinks()
            return True
        except Exception:
            return False

    def set_sink_mute(self, sink_id: str, muted: bool) -> bool:
        """静音 / 取消静音（pactl set-sink-mute NAME 1|0）。"""
        try:
            subprocess.run(
                ["pactl", "set-sink-mute", sink_id, "1" if muted else "0"],
                timeout=3,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self.invalidate_sinks()
            return True
        except Exception:
            return False

    def route_process_audio(self, pid: int, sink_id: str) -> bool:
        """将指定进程的音频路由到指定 sink。

        两步走：
        1. 切 default sink —— 影响该进程之后新建的所有 stream（新启动应用）
        2. 按 application.process.id 匹配已在播放的 sink-input 并迁移 ——
           应用先起、后选设备的场景（本函数主要服务这种时序），
           只切 default sink 是无效的，必须 move-sink-input 才能立刻换声卡
        """
        ok = self.set_default_sink(sink_id)
        moved = 0
        for si in self.list_sink_inputs():
            if si.get("pid") == pid and self.move_sink_input(si["index"], sink_id):
                moved += 1
        return ok or moved > 0

    def list_sink_inputs(self) -> List[Dict]:
        """列出当前所有音频播放流（sink-input）。

        `pactl list sink-inputs` 典型输出（每个流一个块）：
            Sink Input #12
                Sink: 0
                ...
                Properties:
                    application.name = "VLC media player"
                    application.process.id = "1234"

        返回：[{index, sink, pid, app_name}, ...]
        sink 为该流当前所在的 sink 编号（int，可能为 None），
        pid 取自 application.process.id，用于按进程精确路由。
        """
        try:
            res = subprocess.check_output(
                ["pactl", "list", "sink-inputs"], encoding="utf-8", timeout=5,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return []

        inputs: List[Dict] = []
        current: Dict = {}
        for line in res.splitlines():
            stripped = line.strip()
            if stripped.startswith("Sink Input #"):
                if current and "index" in current:
                    inputs.append(current)
                try:
                    current = {"index": int(stripped.split("#", 1)[1].strip()),
                               "sink": None, "pid": None, "app_name": ""}
                except ValueError:
                    current = {}
                continue
            if not current or "index" not in current:
                continue
            if stripped.startswith("Sink:"):
                val = stripped.split(":", 1)[1].strip()
                if val.isdigit():
                    current["sink"] = int(val)
            elif stripped.startswith("application.process.id"):
                m = re.search(r'"?(\d+)"?', stripped.split("=", 1)[-1])
                if m:
                    current["pid"] = int(m.group(1))
            elif stripped.startswith("application.name"):
                current["app_name"] = stripped.split("=", 1)[-1].strip().strip('"')
        if current and "index" in current:
            inputs.append(current)
        return inputs

    def move_sink_input(self, input_index: int, sink_id: str) -> bool:
        """把某个播放流迁移到指定 sink（sink_id 可用 Name 或编号）。"""
        try:
            subprocess.run(
                ["pactl", "move-sink-input", str(input_index), sink_id],
                timeout=3,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            return True
        except Exception:
            return False

    def move_all_sink_inputs(self, sink_id: str) -> int:
        """把所有正在播放的流迁移到指定 sink，返回成功迁移的流数量。

        用于用户在网页切换输出设备时立刻生效（而不是只影响后续新流）。
        """
        moved = 0
        for si in self.list_sink_inputs():
            if self.move_sink_input(si["index"], sink_id):
                moved += 1
        return moved


am = AudioManager()
