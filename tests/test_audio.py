"""音频引擎 - 修复验证测试（13个）"""
import pytest, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audio.engine import AudioEngine, AudioInstance

class TestChannelLimit:
    def test_max_channels_respected(self):
        ae = AudioEngine()
        for i in range(100):
            ae.play_sound(f"s{i}")
        assert len(ae.instances) <= 32

    def test_audio_clip_cached_and_reused(self):
        ae = AudioEngine()
        for _ in range(10):
            ae.play_sound("gunshot")
        assert ae.load_count == 1

class Test3DRelativePosition:
    def test_volume_decreases_with_distance(self):
        ae = AudioEngine()
        v1 = ae.calculate_3d_volume((0, 0, 0), (10, 0, 0))
        v2 = ae.calculate_3d_volume((0, 0, 0), (100, 0, 0))
        assert v2 < v1

    def test_relative_coordinates_follow_listener(self):
        ae = AudioEngine()
        # 同一相对位置，听者整体平移后音量必须一致
        v1 = ae.calculate_3d_volume((0, 0, 0), (10, 0, 0))
        v2 = ae.calculate_3d_volume((90, 0, 0), (100, 0, 0))
        assert abs(v1 - v2) < 1e-9

    def test_pan_direction_not_reversed(self):
        ae = AudioEngine()
        # 声源在听者左侧 -> 声音从左边出来
        assert ae.calculate_3d_pan((0, 0, 0), (-10, 0, 0)) < 0
        assert ae.calculate_3d_pan((0, 0, 0), (10, 0, 0)) > 0

class TestAudioLimiter:
    def test_mixed_audio_not_clipped(self):
        ae = AudioEngine()
        result = ae.mix_audio([0.8, 0.8, 0.8])
        assert result[0] <= 1.0

    def test_master_volume_applied_to_mix(self):
        ae = AudioEngine()
        ae.set_master_volume(0.5)
        assert abs(ae.mix_audio([0.4])[0] - 0.2) < 1e-9

class TestAudioPool:
    def test_stopped_instances_recycled(self):
        ae = AudioEngine()
        inst = AudioInstance("s1", playing=True)
        ae.recycle_instance(inst)
        assert inst.playing == False

    def test_recycled_instance_reused_without_overlap(self):
        ae = AudioEngine()
        inst = ae.play_sound("step", duration=1.0)
        ae.recycle_instance(inst)
        again = ae.play_sound("step", duration=1.0)
        assert again is inst
        assert again.playing is True
        assert ae.pool_size("step") == 0

class TestBGMFade:
    def test_bgm_transition_is_smooth(self):
        ae = AudioEngine()
        result = ae.fade_bgm("battle", "explore", 2.0)
        assert result == True

    def test_bgm_crossfade_blends_both_layers(self):
        ae = AudioEngine()
        ae.fade_bgm("battle", "explore", 2.0)
        ae.update_bgm(1.0)
        assert 0 < ae.bgm_volume("battle") < 1
        assert 0 < ae.bgm_volume("explore") < 1

    def test_bgm_layered_transition(self):
        ae = AudioEngine()
        ae.fade_bgm("battle", "explore", 2.0)
        ae.update_bgm(2.0)
        ae.play_bgm_layer("battle", 0.8, duration=1.0)
        ae.update_bgm(1.0)
        assert ae.bgm_volume("battle") == 0.8
        assert ae.bgm_volume("explore") == 1.0

    def test_bgm_pauses_with_game(self):
        ae = AudioEngine()
        ae.play_bgm_layer("explore", 1.0, duration=1.0)
        ae.update_bgm(1.0)
        ae.pause()
        ae.stop_bgm("explore", duration=1.0)
        ae.update_bgm(1.0)
        assert ae.bgm_volume("explore") == 1.0
        ae.resume()
        ae.update_bgm(1.0)
        assert ae.bgm_volume("explore") == 0.0
