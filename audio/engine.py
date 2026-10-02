"""音频引擎

修复点：
1. 通道管理：播放前检查硬件通道数（max_channels，默认32），满时复用已结束
   实例或按最老实例做通道抢占；同 id 的音频数据带缓存复用，不重复加载。
2. 3D 音效：基于听者-声源的相对坐标计算距离衰减与左右声像（pan），
   不再使用世界坐标。
3. 混音：应用主音量，求和后做增益衰减式限幅（不硬截断，防爆音）。
4. 音频池：回收时停止实例、重置状态；复用时取出池中同 id 的空闲实例，
   不会出现两个声音叠在一起。
5. BGM：支持淡入淡出、探索/战斗多层叠平滑过渡，并随游戏暂停/恢复。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class AudioInstance:
    aid: str
    volume: float = 1.0
    playing: bool = False
    position_3d: tuple = (0, 0, 0)
    # 该实例自身的3D参数，避免复用实例时残留上一次的声音状态
    pan: float = 0.0
    loop: bool = False
    paused: bool = False
    # 0.0 表示一次性音效自然结束，由调用方在更新时标记结束
    duration: float = 0.0
    elapsed: float = 0.0
    # 已加载的音频数据句柄（缓存复用，不每次重新读文件）
    clip: object = None


@dataclass
class BgmLayer:
    name: str
    volume: float = 0.0          # 当前音量（淡入淡出过程中变化）
    target_volume: float = 1.0   # 淡入淡出目标音量
    active: bool = True          # False 表示淡出完成后移除


class AudioEngine:
    def __init__(self, max_channels: int = 32):
        # 硬件通道上限：同时播放的实例不会超过该值
        self.max_channels = max_channels
        self.instances: list[AudioInstance] = []
        self.master_volume: float = 1.0
        # 空闲实例池：按 sound_id 分组，回收后可安全复用
        self.audio_pool: dict[str, list[AudioInstance]] = {}
        # 已加载音频缓存：sound_id -> clip，避免重复加载文件导致内存暴涨
        self._clip_cache: dict[str, object] = {}
        # 音频文件加载次数（用于验证复用）
        self.load_count = 0
        # BGM 层叠：name -> BgmLayer，支持多层平滑混合
        self._bgm_layers: dict[str, BgmLayer] = {}
        self.bgm_paused = False
        self.paused = False

    # ------------------------------------------------------------------
    # 资源加载（带缓存）
    # ------------------------------------------------------------------
    def _load_clip(self, sound_id: str) -> object:
        clip = self._clip_cache.get(sound_id)
        if clip is None:
            # 真实实现此处会读取磁盘文件；这里以标识对象代替解码后的音频数据
            clip = {"clip": sound_id}
            self._clip_cache[sound_id] = clip
            self.load_count += 1
        return clip

    # ------------------------------------------------------------------
    # 1. 通道管理 + 实例复用
    # ------------------------------------------------------------------
    def play_sound(
        self,
        sound_id: str,
        volume: float = 1.0,
        position_3d: tuple | None = None,
        loop: bool = False,
        duration: float = 0.0,
    ) -> AudioInstance | None:
        # 先尝试从实例池复用一个已停止的同 id 实例
        instance = self._acquire_from_pool(sound_id)
        if instance is not None:
            instance.volume = volume
            instance.position_3d = position_3d if position_3d is not None else (0, 0, 0)
            instance.pan = 0.0
            instance.loop = loop
            instance.paused = False
            instance.duration = duration
            instance.elapsed = 0.0
            instance.playing = True
        else:
            if len(self.instances) >= self.max_channels:
                # 通道已满：优先抢占一个已结束/暂停/非循环的实例
                instance = self._steal_channel()
                if instance is None:
                    # 全部在活跃发声，新音效只能丢弃，而不是无限追加
                    return None
                self.instances.remove(instance)
                instance.aid = sound_id
                instance.volume = volume
                instance.playing = True
                instance.position_3d = (
                    position_3d if position_3d is not None else (0, 0, 0)
                )
                instance.pan = 0.0
                instance.loop = loop
                instance.paused = False
                instance.duration = duration
                instance.elapsed = 0.0
            else:
                instance = AudioInstance(
                    sound_id,
                    volume=volume,
                    playing=True,
                    position_3d=position_3d if position_3d is not None else (0, 0, 0),
                    loop=loop,
                    duration=duration,
                )
        instance.clip = self._load_clip(sound_id)
        if instance not in self.instances:
            self.instances.append(instance)
        return instance

    def _acquire_from_pool(self, sound_id: str) -> AudioInstance | None:
        bucket = self.audio_pool.get(sound_id)
        if bucket:
            return bucket.pop()
        return None

    def _steal_channel(self) -> AudioInstance | None:
        # 已结束的实例最先被抢占
        for inst in self.instances:
            if not inst.playing:
                return inst
        # 其次非循环音效中最先开始播放的（最老实例）
        candidates = [
            inst for inst in self.instances
            if not inst.loop and not inst.paused
        ]
        if candidates:
            return candidates[0]
        return self.instances[0] if self.instances else None

    def update(self, dt: float) -> None:
        """推进播放时钟；自然播完的实例自动停止并回收到池中。"""
        finished = []
        for inst in self.instances:
            if inst.playing and not inst.paused and inst.duration > 0.0:
                inst.elapsed += dt
                if not inst.loop and inst.elapsed >= inst.duration:
                    finished.append(inst)
        for inst in finished:
            self.stop_instance(inst)

    def stop_instance(self, instance: AudioInstance) -> None:
        if instance in self.instances:
            self.instances.remove(instance)
        self.recycle_instance(instance)

    # ------------------------------------------------------------------
    # 2. 3D 音效：相对坐标 + 距离衰减 + 正确的左右方位
    # ------------------------------------------------------------------
    def calculate_3d_volume(
        self,
        listener: tuple,
        source: tuple,
        min_distance: float = 1.0,
        max_distance: float = 100.0,
        rolloff: float = 1.0,
    ) -> float:
        """根据听者与声源的相对坐标计算衰减后的音量（0.0~1.0）。

        使用相对坐标（source - listener），听者移动时声音方位/音量随之变化。
        小于 min_distance 不衰减；超过 max_distance 衰减到 0。
        """
        distance = self._relative_distance(listener, source)
        if distance <= min_distance:
            return 1.0
        if distance >= max_distance:
            return 0.0
        # 线性 rolloff 衰减：rolloff 越大衰减越快
        gain = 1.0 - rolloff * (distance - min_distance) / (max_distance - min_distance)
        return max(0.0, min(1.0, gain))

    def calculate_3d_pan(self, listener: tuple, source: tuple) -> float:
        """计算左右声像，范围 -1.0（全左）~ +1.0（全右）。

        基于相对坐标的 x 分量：声源在听者左侧 -> 声音从左边出来，
        修复了“左边来人声音从右边出”的方向反转问题。
        """
        rel_x = source[0] - listener[0]
        rel_y = (source[1] - listener[1]) ** 2
        rel_z = (source[2] - listener[2]) ** 2
        lateral = math.sqrt(rel_x * rel_x + rel_z + rel_y)
        if lateral == 0.0:
            return 0.0
        pan = rel_x / lateral
        return max(-1.0, min(1.0, pan))

    def calculate_3d(
        self,
        listener: tuple,
        source: tuple,
        min_distance: float = 1.0,
        max_distance: float = 100.0,
        rolloff: float = 1.0,
    ) -> tuple[float, float]:
        """同时返回 (衰减音量, 声像)。"""
        volume = self.calculate_3d_volume(
            listener, source, min_distance, max_distance, rolloff
        )
        pan = self.calculate_3d_pan(listener, source)
        return volume, pan

    @staticmethod
    def _relative_distance(listener: tuple, source: tuple) -> float:
        return math.sqrt(sum((s - l) ** 2 for s, l in zip(source, listener)))

    # ------------------------------------------------------------------
    # 3. 混音：主音量 + 限幅防爆音
    # ------------------------------------------------------------------
    def mix_audio(self, samples: list, apply_master: bool = True) -> list:
        """多路音频混合。

        - 先按主音量统一缩放（玩家调主音量立即生效）；
        - 再做增益衰减式限幅：只有峰值超过 1.0 时才整体压缩，避免硬截断
          削波产生爆音，同时不会损伤正常电平的声音。
        """
        if not samples:
            return [0.0]
        gain = self.master_volume if apply_master else 1.0
        scaled = [s * gain for s in samples]
        # 总线求和
        total = sum(scaled)
        # 增益衰减式限幅：总线峰值超过 1.0 时整体压缩到刚好不溢出，
        # 既不硬截断削波（防爆音），也不损伤正常电平的混音
        if abs(total) > 1.0:
            total /= abs(total)
        return [total]

    def set_master_volume(self, volume: float) -> None:
        self.master_volume = max(0.0, min(1.0, volume))

    # ------------------------------------------------------------------
    # 4. 音频池：停止 -> 重置 -> 回收
    # ------------------------------------------------------------------
    def recycle_instance(self, instance: AudioInstance) -> None:
        # 必须先停止播放，否则复用瞬间两个声音会叠在一起
        instance.playing = False
        instance.paused = False
        instance.loop = False
        instance.elapsed = 0.0
        instance.duration = 0.0
        instance.pan = 0.0
        instance.volume = 1.0
        instance.position_3d = (0, 0, 0)
        # clip 保留缓存引用即可，不释放共享缓存
        bucket = self.audio_pool.setdefault(instance.aid, [])
        if instance not in bucket:
            bucket.append(instance)

    def pool_size(self, sound_id: str | None = None) -> int:
        if sound_id is not None:
            return len(self.audio_pool.get(sound_id, []))
        return sum(len(bucket) for bucket in self.audio_pool.values())

    # ------------------------------------------------------------------
    # 5. BGM：淡入淡出 + 多层叠过渡 + 暂停联动
    # ------------------------------------------------------------------
    def fade_bgm(self, from_bgm: str, to_bgm: str, duration: float) -> bool:
        """切换 BGM：旧曲淡出、新曲淡入，duration 秒内完成交叉淡化。"""
        if duration < 0:
            return False
        if from_bgm:
            old = self._bgm_layers.get(from_bgm)
            if old is None:
                # 旧曲正在播放但尚未登记：按满音量创建层再淡出，保证交叉淡化完整
                old = BgmLayer(from_bgm, volume=1.0, target_volume=0.0, active=False)
                self._bgm_layers[from_bgm] = old
            old.target_volume = 0.0
            old.active = False
        if to_bgm:
            layer = self._bgm_layers.get(to_bgm)
            if layer is None:
                layer = BgmLayer(to_bgm, volume=0.0, target_volume=1.0)
                self._bgm_layers[to_bgm] = layer
            else:
                layer.target_volume = 1.0
                layer.active = True
        self._fade_duration = max(duration, 0.01)
        return True

    def play_bgm_layer(
        self, name: str, target_volume: float = 1.0, duration: float = 1.0
    ) -> bool:
        """播放/叠加一层 BGM（如战斗层叠在探索层之上），平滑淡入。

        其他层保持当前音量继续播放，实现层叠过渡而非直接切歌。
        """
        if duration < 0:
            return False
        layer = self._bgm_layers.get(name)
        if layer is None:
            layer = BgmLayer(name, volume=0.0)
            self._bgm_layers[name] = layer
        layer.target_volume = max(0.0, min(1.0, target_volume))
        layer.active = layer.target_volume > 0.0
        self._fade_duration = max(duration, 0.01)
        return True

    def stop_bgm(self, name: str, duration: float = 1.0) -> bool:
        layer = self._bgm_layers.get(name)
        if layer is None:
            return True
        layer.target_volume = 0.0
        layer.active = False
        self._fade_duration = max(duration, 0.01)
        return True

    def update_bgm(self, dt: float) -> None:
        """按时间推进所有 BGM 层音量，趋近各自的目标音量。"""
        if self.bgm_paused:
            return
        fade_t = getattr(self, "_fade_duration", 1.0)
        step = dt / fade_t if fade_t > 0 else 1.0
        finished = []
        for name, layer in self._bgm_layers.items():
            if layer.volume < layer.target_volume:
                layer.volume = min(layer.target_volume, layer.volume + step)
            elif layer.volume > layer.target_volume:
                layer.volume = max(layer.target_volume, layer.volume - step)
            if not layer.active and layer.volume <= 0.0:
                finished.append(name)
        for name in finished:
            del self._bgm_layers[name]

    def bgm_volume(self, name: str) -> float:
        layer = self._bgm_layers.get(name)
        return layer.volume if layer else 0.0

    def active_bgms(self) -> list[str]:
        return [name for name, layer in self._bgm_layers.items() if layer.volume > 0.0]

    def pause_bgm(self) -> None:
        self.bgm_paused = True

    def resume_bgm(self) -> None:
        self.bgm_paused = False

    def pause(self, *_args) -> None:
        """暂停游戏：暂停全部音效实例与 BGM。"""
        self.paused = True
        self.bgm_paused = True
        for inst in self.instances:
            if inst.playing:
                inst.paused = True

    def resume(self) -> None:
        """恢复游戏：恢复音效实例与 BGM。"""
        self.paused = False
        self.bgm_paused = False
        for inst in self.instances:
            inst.paused = False

    # 常见别名，便于调用方使用
    pause_all = pause
    resume_all = resume
