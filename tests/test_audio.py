"""音频引擎 - 红态测试"""
import pytest, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audio.engine import AudioEngine, AudioInstance

class TestChannelLimit:
    def test_max_channels_respected(self):
        ae = AudioEngine()
        for i in range(100):
            ae.play_sound(f"s{i}")
        assert len(ae.instances) <= 32

class Test3DRelativePosition:
    def test_volume_decreases_with_distance(self):
        ae = AudioEngine()
        v1 = ae.calculate_3d_volume((0, 0, 0), (10, 0, 0))
        v2 = ae.calculate_3d_volume((0, 0, 0), (100, 0, 0))
        assert v2 < v1

class TestAudioLimiter:
    def test_mixed_audio_not_clipped(self):
        ae = AudioEngine()
        result = ae.mix_audio([0.8, 0.8, 0.8])
        assert result[0] <= 1.0

class TestAudioPool:
    def test_stopped_instances_recycled(self):
        ae = AudioEngine()
        inst = AudioInstance("s1", playing=True)
        ae.recycle_instance(inst)
        assert inst.playing == False

class TestBGMFade:
    def test_bgm_transition_is_smooth(self):
        ae = AudioEngine()
        result = ae.fade_bgm("battle", "explore", 2.0)
        assert result == True
