"""
Lightweight streaming buffer for cache-aware streaming ASR.

Pure numpy reimplementation of NeMo's CacheAwareStreamingAudioBuffer.
Handles mel spectrogram extraction via MelPreprocessor and chunks features
into model-sized pieces with pre-encode cache overlap.

Eliminates the need for torch/nemo at runtime in ONNX mode.
"""

import numpy as np

from stt_streaming.mel_preprocessor import MelPreprocessor


class StreamingBuffer:
    """
    Single-stream audio buffer for cache-aware streaming inference.

    Replaces NeMo's CacheAwareStreamingAudioBuffer for ONNX mode.
    Only supports batch_size=1 (single stream), which is all we need
    for per-connection streaming sessions.

    Usage:
        buf = StreamingBuffer(preprocessor, streaming_cfg, n_mels, sampling_frames)
        buf.append_audio(audio_float32, stream_id=-1)  # first append
        buf.append_audio(more_audio, stream_id=0)       # subsequent
        for chunk, chunk_lengths in buf:
            # chunk: [1, n_mels, T] numpy array
            # chunk_lengths: [1] numpy array
            pass
    """

    def __init__(
        self,
        preprocessor: MelPreprocessor,
        streaming_cfg: dict,
        n_mels: int = 80,
        sampling_frames: list[int] | None = None,
    ):
        """
        Args:
            preprocessor: MelPreprocessor instance for mel spectrogram extraction
            streaming_cfg: dict with keys:
                chunk_size: int or [first, subsequent]
                shift_size: int or [first, subsequent]
                pre_encode_cache_size: int or [first, subsequent]
            n_mels: number of mel bins (typically 80)
            sampling_frames: [first, subsequent] minimum frames for subsampling,
                or None to skip the check
        """
        self.preprocessor = preprocessor
        self.n_mels = n_mels
        self.sampling_frames = sampling_frames

        # Parse streaming config (values can be int or list[int])
        self.chunk_size = streaming_cfg["chunk_size"]
        self.shift_size = streaming_cfg["shift_size"]
        self.pre_encode_cache_size = streaming_cfg["pre_encode_cache_size"]

        # Buffer state
        self.buffer: np.ndarray | None = None  # [1, n_mels, T]
        self.buffer_idx: int = 0
        self.stream_length: int = 0  # actual feature length in buffer
        self.step: int = 0

    def append_audio(self, audio: np.ndarray, stream_id: int = -1):
        """
        Preprocess raw audio and append mel features to the buffer.

        Args:
            audio: float32 array of raw audio samples
            stream_id: -1 for first append (creates buffer), 0 for subsequent
        """
        features, feat_len = self.preprocessor(audio, len(audio))
        # features: [1, n_mels, T]

        if self.buffer is None:
            # First append — create buffer
            self.buffer = features
            self.stream_length = feat_len
        else:
            # Subsequent append — extend buffer
            needed_len = self.stream_length + feat_len
            if needed_len > self.buffer.shape[2]:
                # Grow buffer
                pad_amount = needed_len - self.buffer.shape[2]
                self.buffer = np.pad(
                    self.buffer, ((0, 0), (0, 0), (0, pad_amount)), constant_values=0.0
                )
            # Copy new features into buffer
            self.buffer[0, :, self.stream_length : self.stream_length + feat_len] = features[0, :, :feat_len]
            self.stream_length += feat_len

    def __iter__(self):
        return self

    def __next__(self) -> tuple[np.ndarray, np.ndarray]:
        """
        Yield next chunk with pre-encode cache prepended.

        Returns:
            (chunk, chunk_lengths):
                chunk: [1, n_mels, T] float32 numpy array
                chunk_lengths: [1] int64 numpy array
        """
        if self.buffer is None or self.buffer_idx >= self.buffer.shape[2]:
            raise StopIteration

        # Determine chunk_size and shift_size for current step
        if self.step == 0 and isinstance(self.chunk_size, list):
            chunk_size = self.chunk_size[0]
        else:
            chunk_size = self.chunk_size[1] if isinstance(self.chunk_size, list) else self.chunk_size

        if self.step == 0 and isinstance(self.shift_size, list):
            shift_size = self.shift_size[0]
        else:
            shift_size = self.shift_size[1] if isinstance(self.shift_size, list) else self.shift_size

        # Extract audio chunk
        end_idx = min(self.buffer_idx + chunk_size, self.buffer.shape[2])
        audio_chunk = self.buffer[:, :, self.buffer_idx : end_idx]  # [1, n_mels, chunk_len]
        actual_chunk_len = audio_chunk.shape[2]

        # Check sampling_frames minimum
        if self.sampling_frames is not None:
            if self.step == 0 and isinstance(self.sampling_frames, list):
                min_frames = self.sampling_frames[0]
            else:
                min_frames = (
                    self.sampling_frames[1]
                    if isinstance(self.sampling_frames, list)
                    else self.sampling_frames
                )
            if actual_chunk_len < min_frames:
                raise StopIteration

        # Compute pre-encode cache
        if self.step == 0 and isinstance(self.pre_encode_cache_size, list):
            # First step: use zeros (pre_encode_cache_size[0] is typically 0)
            cache_size = self.pre_encode_cache_size[0]
            cache_pre_encode = np.zeros(
                (1, self.n_mels, cache_size), dtype=np.float32
            )
            zeros_pads = None
        else:
            cache_size = (
                self.pre_encode_cache_size[1]
                if isinstance(self.pre_encode_cache_size, list)
                else self.pre_encode_cache_size
            )
            start = max(0, self.buffer_idx - cache_size)
            cache_pre_encode = self.buffer[:, :, start : self.buffer_idx]  # [1, n_mels, <=cache_size]

            # If not enough history frames, prepend zeros
            if cache_pre_encode.shape[2] < cache_size:
                zeros_pads = np.zeros(
                    (1, self.n_mels, cache_size - cache_pre_encode.shape[2]),
                    dtype=np.float32,
                )
            else:
                zeros_pads = None

        added_len = cache_pre_encode.shape[2]

        # Concatenate: [cache | chunk]
        audio_chunk = np.concatenate([cache_pre_encode, audio_chunk], axis=2)

        if zeros_pads is not None:
            audio_chunk = np.concatenate([zeros_pads, audio_chunk], axis=2)
            added_len += zeros_pads.shape[2]

        # Compute chunk_lengths (clamped to valid range)
        max_chunk_len = self.stream_length - self.buffer_idx + added_len
        chunk_lengths = np.array(
            [min(max(max_chunk_len, 0), audio_chunk.shape[2])], dtype=np.int64
        )

        self.buffer_idx += shift_size
        self.step += 1

        # Trim consumed frames to bound memory for long sessions.
        # Keep only frames from (buffer_idx - max_cache) onward.
        # For a 2-hour meeting this prevents ~220 MB of dead mel data.
        self._maybe_trim()

        return audio_chunk, chunk_lengths

    # Trim when this many dead frames accumulate (avoids trimming every step).
    _TRIM_THRESHOLD = 1000  # ~10 seconds of mel frames

    def _maybe_trim(self):
        """Discard consumed buffer frames that are no longer needed for cache."""
        if self.buffer is None:
            return

        max_cache = (
            self.pre_encode_cache_size[1]
            if isinstance(self.pre_encode_cache_size, list)
            else self.pre_encode_cache_size
        )
        # Earliest frame we might still read (for pre-encode cache lookback)
        keep_from = max(0, self.buffer_idx - max_cache)

        if keep_from < self._TRIM_THRESHOLD:
            return

        # Slice away dead prefix
        self.buffer = self.buffer[:, :, keep_from:].copy()
        self.buffer_idx -= keep_from
        self.stream_length -= keep_from

    def is_buffer_empty(self) -> bool:
        """Check if all buffered features have been consumed."""
        if self.buffer is None:
            return True
        return self.buffer_idx >= self.buffer.shape[2]

    def flush_preprocessor(self):
        """Flush any held-back mel frames from the preprocessor into the buffer."""
        result = self.preprocessor.flush()
        if result is None:
            return
        features, feat_len = result
        if self.buffer is not None and feat_len > 0:
            needed_len = self.stream_length + feat_len
            if needed_len > self.buffer.shape[2]:
                pad_amount = needed_len - self.buffer.shape[2]
                self.buffer = np.pad(
                    self.buffer, ((0, 0), (0, 0), (0, pad_amount)), constant_values=0.0
                )
            self.buffer[0, :, self.stream_length : self.stream_length + feat_len] = features[0, :, :feat_len]
            self.stream_length += feat_len

    def reset(self):
        """Reset buffer state for a new utterance."""
        self.buffer = None
        self.buffer_idx = 0
        self.stream_length = 0
        self.step = 0
