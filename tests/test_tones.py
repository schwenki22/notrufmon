"""Tone signal + detector: must survive level changes, noise and loss, and must not
mistake our own echo, silence or comfort noise for the far end."""
import math
import random

from notrufmon import tones as t


def _packets(sig, n_frames, codec="PCMA", drop=(), mangle=None, pt=8):
    out = []
    for i in range(n_frames):
        if i in drop:
            continue
        payload = sig.frame(i)
        if mangle:
            payload = t.encode(mangle(i, t.decode(payload, codec)), codec)
        out.append((100.0 + i * 0.02, i & 0xFFFF, (1000 + i * 160) & 0xFFFFFFFF, 1, pt, payload, ("10.0.0.1", 4000)))
    return out


def test_clean_signal_detected_both_codecs():
    for codec, pt in (("PCMA", 8), ("PCMU", 0)):
        sig = t.signal_for("up", codec)
        r = t.analyze(_packets(sig, 750, codec, pt=pt), t.UP_FREQS, codec, pt)     # 15 s
        assert r["audio_ok"], r
        assert r["tone_ratio"] > 0.9 and r["bursts"] >= 35 and r["freqs_seen"] == list(t.UP_FREQS)
        assert r["longest_gap_s"] <= 0.15


def test_wrong_direction_is_not_detected():
    """The far end echoing our own signal must not count as audio from the far end."""
    sig = t.signal_for("down")
    r = t.analyze(_packets(sig, 750), t.UP_FREQS, "PCMA", 8)
    assert not r["audio_ok"] and r["tone_windows"] == 0


def test_attenuated_and_noisy_still_detected():
    sig = t.signal_for("up")
    rnd = random.Random(3)
    def mangle(i, x):                       # -12 dB and white noise at about -35 dBFS
        return [v * 0.25 + rnd.gauss(0, 600) for v in x]
    r = t.analyze(_packets(sig, 750, mangle=mangle), t.UP_FREQS, "PCMA", 8)
    assert r["audio_ok"], r


def test_ten_percent_loss_still_ok_but_long_dropout_is_reported():
    sig = t.signal_for("up")
    rnd = random.Random(1)
    lossy = {i for i in range(750) if rnd.random() < 0.1}
    assert t.analyze(_packets(sig, 750, drop=lossy), t.UP_FREQS, "PCMA", 8)["audio_ok"]
    gap = set(range(200, 400))              # 4 s without audio in the middle
    r = t.analyze(_packets(sig, 750, drop=gap), t.UP_FREQS, "PCMA", 8)
    assert r["longest_gap_s"] >= 3.9


def test_silence_and_comfort_noise_are_not_audio():
    sig = t.signal_for("up")
    rnd = random.Random(7)
    noise = t.analyze(_packets(sig, 750, mangle=lambda i, x: [rnd.gauss(0, 300) for _ in x]),
                      t.UP_FREQS, "PCMA", 8)
    silence = t.analyze(_packets(sig, 750, mangle=lambda i, x: [0] * len(x)), t.UP_FREQS, "PCMA", 8)
    assert not noise["audio_ok"] and not silence["audio_ok"]
    assert t.analyze([], t.UP_FREQS, "PCMA", 8)["audio_ok"] is False


def test_only_first_seconds_then_silence_is_not_ok():
    """Audio that stops after 3 s of a 15 s call (one-way after a handover) must not pass."""
    sig = t.signal_for("up")
    r = t.analyze(_packets(sig, 750, mangle=lambda i, x: x if i < 150 else [0] * len(x)), t.UP_FREQS, "PCMA", 8)
    assert not r["audio_ok"] and r["tone_ratio"] < 0.5


def test_level_is_moderate():
    sig = t.signal_for("up")
    x = [v for i in range(80) for v in t.decode(sig.frame(i), "PCMA")]
    rms = math.sqrt(sum(v * v for v in x) / len(x)) / 32768
    assert -21 < 20 * math.log10(rms) < -14
