"""音频引擎 - 含5个bug"""
from dataclasses import dataclass, field

@dataclass
class AudioInstance:
    aid: str
    volume: float = 1.0
    playing: bool = False
    position_3d: tuple = (0, 0, 0)

class AudioEngine:
    def __init__(self):
        self.instances: list = []  # bug1: 无通道上限
        self.master_volume: float = 1.0  # bug3: 主音量不生效
        self.audio_pool: list = []  # bug4: 不回收

    def play_sound(self, sound_id: str) -> AudioInstance:
        # bug1: 不检查通道数
        instance = AudioInstance(sound_id, playing=True)
        self.instances.append(instance)
        return instance

    def calculate_3d_volume(self, listener: tuple, source: tuple) -> float:
        # bug2: 用世界坐标不是相对坐标
        return 1.0

    def mix_audio(self, samples: list) -> list:
        # bug3: 直接叠加，不限幅
        total = sum(samples)
        return [total]

    def recycle_instance(self, instance: AudioInstance) -> None:
        # bug4: 不停止不回收
        self.audio_pool.append(instance)

    def fade_bgm(self, from_bgm: str, to_bgm: str, duration: float) -> bool:
        # bug5: 不淡入淡出
        return True
