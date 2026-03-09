"""
Lightweight mel spectrogram preprocessor using only numpy.

Matches NeMo's AudioToMelSpectrogramPreprocessor output for streaming mode
(dither=0, pad_to=0). Eliminates the need for torch/nemo at runtime.

Parameters are loaded from metadata.json (saved during ONNX export).
The mel filterbank matrix is loaded from mel_filterbank.npy.
"""

import numpy as np


class MelPreprocessor:
    """
    Pure-numpy mel spectrogram matching NeMo's FilterbankFeatures.

    Streaming mode settings (hardcoded, matching CacheAwareStreamingAudioBuffer):
    - dither = 0 (disabled for streaming)
    - pad_to = 0 (no output padding)

    All other parameters come from the model config (saved in metadata.json).
    """

    def __init__(self, config: dict, filterbank: np.ndarray):
        """
        Args:
            config: preprocessor config dict from metadata.json["preprocessor"]
            filterbank: mel filterbank matrix, shape [n_mels, n_fft//2+1] float32
        """
        self.sample_rate = config["sample_rate"]
        self.n_fft = config["n_fft"]
        self.hop_length = config["hop_length"]
        self.win_length = config["win_length"]
        self.n_mels = config["n_mels"]
        self.preemph = config.get("preemph", 0.97)
        self.mag_power = config.get("mag_power", 2.0)
        self.do_log = config.get("log", True)
        self.log_zero_guard_value = config.get("log_zero_guard_value", 2**-24)
        self.log_zero_guard_type = config.get("log_zero_guard_type", "add")
        self.normalize = config.get("normalize", "NA")
        self.exact_pad = config.get("exact_pad", False)

        # Mel filterbank: ensure [n_mels, n_fft//2+1]
        fb = filterbank
        if fb.ndim == 3:
            fb = fb.squeeze(0)
        assert fb.shape == (self.n_mels, self.n_fft // 2 + 1), (
            f"filterbank shape mismatch: {fb.shape} vs ({self.n_mels}, {self.n_fft // 2 + 1})"
        )
        self.fb = fb.astype(np.float32)

        # Window: symmetric hann (torch.hann_window(N, periodic=False))
        # Formula: w[n] = 0.5 * (1 - cos(2*pi*n / (N-1))) for n = 0..N-1
        n = np.arange(self.win_length, dtype=np.float64)
        self.window = (0.5 * (1.0 - np.cos(2.0 * np.pi * n / (self.win_length - 1)))).astype(np.float32)

        # Zero-pad window to n_fft (centered), matching torch.stft behavior
        if self.win_length < self.n_fft:
            left_pad = (self.n_fft - self.win_length) // 2
            right_pad = self.n_fft - self.win_length - left_pad
            self.padded_window = np.pad(self.window, (left_pad, right_pad)).astype(np.float32)
        else:
            self.padded_window = self.window.copy()

        # STFT padding amount for exact_pad mode
        if self.exact_pad:
            self.stft_pad_amount = (self.n_fft - self.hop_length) // 2
        else:
            self.stft_pad_amount = None

        # Overlap buffer for streaming: keep last samples from the previous
        # call so the STFT has real audio context at BOTH edges instead of
        # reflected padding. Eliminates mel boundary artifacts that cause
        # ~1.5% WER degradation at real-time pacing.
        #
        # Left edge: prepend _overlap_size samples from previous call's tail.
        # Right edge: hold back _right_hold mel frames until the next call
        #   recomputes them with real right-side context from the overlap.
        self._overlap: np.ndarray | None = None
        self._overlap_size = self.n_fft  # 512 samples = 32ms at 16kHz
        # Use n_fft (not n_fft//2) so next call's overlap covers the
        # right-edge zone of the previous call, enabling recomputation.

        # Number of mel frames to hold back from right edge per call.
        # These frames have reflect-padded right context.  They get
        # recomputed on the next call via the overlap.
        self._right_hold = (self.n_fft // 2 + self.hop_length - 1) // self.hop_length  # = 2
        self._held_mel: np.ndarray | None = None  # [n_mels, _right_hold]

    def get_seq_len(self, audio_len: int) -> int:
        """Compute output mel feature length from input audio sample count."""
        if self.stft_pad_amount is not None:
            pad_amount = self.stft_pad_amount * 2
        else:
            pad_amount = self.n_fft // 2 * 2  # center=True padding
        return (audio_len + pad_amount - self.n_fft) // self.hop_length + 1

    def __call__(self, audio: np.ndarray, audio_len: int) -> tuple[np.ndarray, int]:
        """
        Compute log-mel spectrogram from raw audio.

        Args:
            audio: float32 array of shape [num_samples]
            audio_len: actual number of valid samples in audio

        Returns:
            (features, feature_len):
                features: [1, n_mels, T] float32 mel spectrogram
                feature_len: number of valid time frames
        """
        x = audio[:audio_len].astype(np.float32)

        # Save raw tail BEFORE preemphasis for next call's overlap.
        # We save raw audio (not preemph'd) so preemphasis is applied
        # consistently across the overlap boundary.
        if len(x) >= self._overlap_size:
            new_overlap = x[-self._overlap_size:].copy()
        else:
            new_overlap = x.copy()

        # Prepend overlap from previous call (raw audio from previous chunk's tail).
        # This gives the STFT real audio context at the left edge instead of
        # reflected padding, eliminating mel boundary artifacts.
        if self._overlap is not None:
            x = np.concatenate([self._overlap, x])
            # Adjust audio_len to include overlap
            overlap_len = len(self._overlap)
        else:
            overlap_len = 0

        self._overlap = new_overlap

        # Preemphasis (applied to the full concatenated signal)
        if self.preemph is not None and self.preemph > 0:
            x = np.concatenate([x[:1], x[1:] - self.preemph * x[:-1]])

        # Padding for exact_pad mode
        if self.exact_pad and self.stft_pad_amount is not None:
            x = np.pad(x, (self.stft_pad_amount, self.stft_pad_amount), mode="reflect")

        # STFT → magnitude → power
        spectrum = self._stft(x)  # complex, [n_fft//2+1, num_frames]
        mag = np.abs(spectrum).astype(np.float32)  # [n_fft//2+1, num_frames]

        if self.mag_power != 1.0:
            mag = np.power(mag, self.mag_power)

        # Mel filterbank: fb [n_mels, n_fft//2+1] @ mag [n_fft//2+1, T] → [n_mels, T]
        mel = self.fb @ mag

        # Log
        if self.do_log:
            if self.log_zero_guard_type == "add":
                mel = np.log(mel + self.log_zero_guard_value)
            else:  # "clamp"
                mel = np.log(np.maximum(mel, self.log_zero_guard_value))

        # Compute how many frames to skip (from left overlap region).
        # We want to return frames corresponding to the new audio,
        # plus recomputed frames from the right-hold zone of the previous call.
        total_mel_frames = mel.shape[1]
        new_audio_frames = self.get_seq_len(audio_len)

        if overlap_len > 0:
            total_expected = self.get_seq_len(audio_len + overlap_len)
            skip_left = total_expected - new_audio_frames
            # But we want to keep _right_hold extra frames from the overlap
            # region — these are the recomputed right-edge frames from the
            # previous call (now with real right-side context from new audio).
            skip_left = max(0, skip_left - self._right_hold)
        else:
            skip_left = 0

        mel = mel[:, skip_left:]

        # Prepend previously held frames (from before the overlap era).
        # Once overlap is active, the held frames are recomputed via overlap
        # and already included above, so _held_mel is only used for the
        # transition from first call to second call.
        if self._held_mel is not None and overlap_len == 0:
            mel = np.concatenate([self._held_mel, mel], axis=1)
            self._held_mel = None

        # Hold back rightmost _right_hold frames (affected by reflect padding
        # on the right edge). They'll be recomputed on the next call via overlap.
        if mel.shape[1] > self._right_hold:
            self._held_mel = mel[:, -self._right_hold:].copy()
            mel = mel[:, :-self._right_hold]
        else:
            # Too few frames to hold — keep them all (edge case: very short audio)
            self._held_mel = None

        seq_len = mel.shape[1]

        # Per-feature normalization (if enabled)
        if self.normalize == "per_feature" and seq_len > 1:
            valid = mel[:, :seq_len]
            mean = valid.mean(axis=1, keepdims=True)
            std = valid.std(axis=1, ddof=1, keepdims=True) + 1e-5
            mel = (mel - mean) / std

        # Add batch dimension: [1, n_mels, T]
        features = mel[np.newaxis, :, :seq_len].astype(np.float32)
        return features, seq_len

    def flush(self) -> tuple[np.ndarray, int] | None:
        """
        Return held-back right-edge frames. Call during finalize to
        ensure no mel frames are lost at the end of the stream.
        """
        if self._held_mel is not None and self._held_mel.shape[1] > 0:
            mel = self._held_mel
            self._held_mel = None
            features = mel[np.newaxis, :, :].astype(np.float32)
            return features, mel.shape[1]
        return None

    def _stft(self, x: np.ndarray) -> np.ndarray:
        """
        Short-time Fourier transform matching torch.stft behavior.

        With center=True (exact_pad=False):  reflects-pads n_fft//2 on each side.
        With center=False (exact_pad=True):   no additional padding (caller pads).

        Args:
            x: input signal, float32 (may already be padded for exact_pad mode)

        Returns:
            complex spectrum of shape [n_fft//2+1, num_frames]
        """
        center = not self.exact_pad

        if center:
            x = np.pad(x, (self.n_fft // 2, self.n_fft // 2), mode="reflect")

        # Frame the signal using stride tricks
        num_frames = 1 + (len(x) - self.n_fft) // self.hop_length
        if num_frames <= 0:
            return np.zeros((self.n_fft // 2 + 1, 0), dtype=np.complex64)

        shape = (num_frames, self.n_fft)
        strides = (self.hop_length * x.strides[0], x.strides[0])
        frames = np.lib.stride_tricks.as_strided(x, shape=shape, strides=strides).copy()

        # Apply window and compute real FFT
        windowed = frames * self.padded_window
        spectrum = np.fft.rfft(windowed, n=self.n_fft, axis=1)  # [num_frames, n_fft//2+1]

        return spectrum.T  # [n_fft//2+1, num_frames]
