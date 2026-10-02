"""音频引擎

职责：
- 通道管理：受硬件通道数上限约束，超出时回收已播完实例 / 窃取最旧通道
- 资源复用：音频文件只加载一次并缓存；实例播放完进入对象池复用
- 3D 音效：基于听者相对坐标计算距离衰减与左右声像
- 混音：主音量统一控制，峰值限幅防止削波爆音
- BGM：淡入淡出 / 多层叠混 / 随游戏暂停
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

MAX_CHANNELS = 32


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class AudioInstance:
    aid: str
    volume: float = 1.0
    playing: bool = False
    position_3d: tuple = (0.0, 0.0, 0.0)
    duration: float = 0.0
    elapsed: float = 0.0

    def stop(self) -> None:
        self.playing = False

    def reset(self, aid: str, volume: float = 1.0,
              position_3d: tuple = (0.0, 0.0, 0.0), duration: float = 0.0) -> None:
        self.aid = aid
        self.volume = volume
        self.playing = True
        self.position_3d = tuple(position_3d)
        self.duration = duration
        self.elapsed = 0.0


@dataclass
class BGMChannel:
    name: str
    volume: float = 0.0
    target_volume: float = 1.0
    playing: bool = False
    paused: bool = False


@dataclass
class _Fade:
    channel: BGMChannel
    start_volume: float
    target_volume: float
    elapsed: float
    duration: float
    stop_after: bool = False


class AudioEngine:
    def __init__(self, max_channels: int = MAX_CHANNELS):
        self.max_channels = max_channels
        self.instances: list[AudioInstance] = []
        self.audio_pool: list[AudioInstance] = []
        self.master_volume: float = 1.0
        self.sfx_volume: float = 1.0
        self.bgm_volume: float = 1.0
        self.loaded_sounds: set[str] = set()
        self.load_count: int = 0
        self.bgm_channels: dict[str, BGMChannel] = {}
        self._fades: list[_Fade] = []
        self.paused: bool = False

    # ------------------------------------------------------------------ 资源

    def _load_sound(self, sound_id: str) -> None:
        """音频文件只加载一次，后续播放复用缓存。"""
        if sound_id not in self.loaded_sounds:
            self.loaded_sounds.add(sound_id)
            self.load_count += 1

    def _acquire_instance(self, sound_id: str, volume: float,
                          position_3d: tuple, duration: float) -> AudioInstance:
        self._reclaim_finished()
        if not self.instances or len(self.instances) < self.max_channels:
            instance = self._take_from_pool()
            if instance is None:
                instance = AudioInstance(sound_id)
            instance.reset(sound_id, volume, position_3d, duration)
            self.instances.append(instance)
            return instance
        stolen = self.instances.pop(0)
        stolen.stop()
        stolen.reset(sound_id, volume, position_3d, duration)
        self.instances.append(stolen)
        return stolen

    def _take_from_pool(self, sound_id: Optional[str] = None) -> Optional[AudioInstance]:
        if sound_id is not None:
            for i, instance in enumerate(self.audio_pool):
                if instance.aid == sound_id and not instance.playing:
                    return self.audio_pool.pop(i)
        if self.audio_pool:
            instance = self.audio_pool.pop(0)
            if instance.playing:
                instance.stop()
            return instance
        return None

    def _reclaim_finished(self) -> None:
        finished = [i for i in self.instances if not i.playing]
        for instance in finished:
            self.instances.remove(instance)
            if instance not in self.audio_pool:
                self.audio_pool.append(instance)

    # ------------------------------------------------------------------ 播放

    def play_sound(self, sound_id: str, volume: float = 1.0,
                   position_3d: tuple = (0.0, 0.0, 0.0),
                   duration: float = 0.0) -> AudioInstance:
        """播放一个音效。受通道上限约束，自动复用缓存数据与对象池实例。"""
        self._load_sound(sound_id)
        effective = volume * self.master_volume * self.sfx_volume
        return self._acquire_instance(sound_id, effective, position_3d, duration)

    def stop_sound(self, instance: AudioInstance) -> None:
        instance.stop()

    def active_channel_count(self) -> int:
        return len(self.instances)

    def recycle_instance(self, instance: AudioInstance) -> None:
        """回收实例：必须先停止播放，再放回池中，避免复用时两声叠放。"""
        if instance.playing:
            instance.stop()
        if instance in self.instances:
            self.instances.remove(instance)
        if instance not in self.audio_pool:
            self.audio_pool.append(instance)

    # ------------------------------------------------------------------ 3D

    def _relative_position(self, listener: tuple, source: tuple) -> tuple:
        return tuple(s - l for s, l in zip(source, listener))

    def calculate_3d_volume(self, listener: tuple, source: tuple,
                            min_distance: float = 1.0,
                            max_distance: float = 1000.0,
                            volume: float = 1.0) -> float:
        """基于听者的相对坐标做线性距离衰减。"""
        rel = self._relative_position(listener, source)
        distance = math.sqrt(sum(c * c for c in rel))
        if distance <= min_distance:
            attenuation = 1.0
        else:
            attenuation = clamp(
                1.0 - (distance - min_distance) / (max_distance - min_distance),
                0.0, 1.0,
            )
        return clamp(volume * attenuation, 0.0, 1.0)

    def calculate_3d_pan(self, listener: tuple, source: tuple,
                         min_distance: float = 1.0,
                         max_distance: float = 1000.0,
                         volume: float = 1.0) -> tuple:
        """返回 (左声道音量, 右声道音量)。

        相对坐标 x < 0 表示声源在听者左侧，左声道更响。
        """
        rel = self._relative_position(listener, source)
        distance = math.sqrt(sum(c * c for c in rel))
        total = self.calculate_3d_volume(
            listener, source, min_distance, max_distance, volume
        )
        if distance <= 0.0:
            return total, total
        pan = clamp(rel[0] / distance, -1.0, 1.0)
        left = total * (1.0 - pan) / 2.0
        right = total * (1.0 + pan) / 2.0
        return left, right

    # ------------------------------------------------------------------ 混音

    def mix_audio(self, samples: list, master_volume: Optional[float] = None) -> list:
        """多路音频混合：峰值限幅防爆音，并统一施加主音量。"""
        if not samples:
            return []
        master = self.master_volume if master_volume is None else master_volume
        mixed = 0.0
        for sample in samples:
            mixed += sample
        peak = abs(mixed)
        if peak > 1.0:
            mixed /= peak
        mixed *= master
        mixed = clamp(mixed, -1.0, 1.0)
        return [mixed]

    def set_master_volume(self, volume: float) -> None:
        self.master_volume = clamp(volume, 0.0, 1.0)

    # ------------------------------------------------------------------ BGM

    def _get_bgm(self, name: str) -> BGMChannel:
        channel = self.bgm_channels.get(name)
        if channel is None:
            channel = BGMChannel(name=name)
            self.bgm_channels[name] = channel
        return channel

    def play_bgm(self, name: str, volume: float = 1.0, fade: float = 0.0) -> BGMChannel:
        channel = self._get_bgm(name)
        channel.playing = True
        channel.paused = self.paused
        if fade <= 0.0:
            channel.volume = volume
            channel.target_volume = volume
        else:
            channel.target_volume = volume
            self._fades.append(
                _Fade(channel, channel.volume, volume, 0.0, fade)
            )
        return channel

    def stop_bgm(self, name: str, fade: float = 0.0) -> None:
        channel = self.bgm_channels.get(name)
        if channel is None:
            return
        if fade <= 0.0:
            channel.volume = 0.0
            channel.target_volume = 0.0
            channel.playing = False
            channel.paused = False
            return
        self._fades.append(
            _Fade(channel, channel.volume, 0.0, 0.0, fade, stop_after=True)
        )

    def fade_bgm(self, from_bgm: str, to_bgm: str, duration: float) -> bool:
        """两首 BGM 交叉淡入淡出，支持层叠（旧层淡出、新层淡入并存）。"""
        if duration < 0.0:
            return False
        if duration == 0.0:
            if from_bgm:
                old = self.bgm_channels.get(from_bgm)
                if old is not None:
                    old.volume = 0.0
                    old.playing = False
                    old.paused = False
            if to_bgm:
                channel = self.play_bgm(to_bgm, volume=1.0)
            return True
        if from_bgm:
            old = self._get_bgm(from_bgm)
            old.playing = True
            old.paused = self.paused
            self._fades.append(
                _Fade(old, old.volume, 0.0, 0.0, duration, stop_after=True)
            )
        if to_bgm:
            new = self._get_bgm(to_bgm)
            new.volume = 0.0 if not new.playing else new.volume
            new.playing = True
            new.paused = self.paused
            self._fades.append(
                _Fade(new, new.volume, 1.0, 0.0, duration)
            )
        return True

    def is_bgm_playing(self, name: str) -> bool:
        channel = self.bgm_channels.get(name)
        return bool(channel and channel.playing and not channel.paused)

    def set_paused(self, paused: bool) -> None:
        """游戏暂停/恢复：暂停时 BGM 停止发声，恢复时继续。"""
        if paused == self.paused:
            return
        self.paused = paused
        for channel in self.bgm_channels.values():
            if channel.playing:
                channel.paused = paused

    def pause(self) -> None:
        self.set_paused(True)

    def resume(self) -> None:
        self.set_paused(False)

    # ------------------------------------------------------------------ 推进

    def update(self, delta_time: float) -> None:
        for instance in list(self.instances):
            if instance.duration > 0.0 and instance.playing:
                instance.elapsed += delta_time
                if instance.elapsed >= instance.duration:
                    self.recycle_instance(instance)
        self._reclaim_finished()
        if self.paused:
            return
        done = []
        for fade in self._fades:
            fade.elapsed += delta_time
            ratio = clamp(fade.elapsed / fade.duration, 0.0, 1.0) if fade.duration else 1.0
            value = fade.start_volume + (fade.target_volume - fade.start_volume) * ratio
            fade.channel.volume = value
            if ratio >= 1.0:
                fade.channel.target_volume = fade.target_volume
                if fade.stop_after:
                    fade.channel.volume = 0.0
                    fade.channel.playing = False
                    fade.channel.paused = False
                done.append(fade)
        for fade in done:
            self._fades.remove(fade)
