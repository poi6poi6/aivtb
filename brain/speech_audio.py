"""Conservative PCM edge-silence cleanup; no time stretch or internal pause edits."""
import io
import struct
import wave


def trim_edge_silence(data: bytes, *, start_padding_sec: float = 0.10) -> bytes:
    try:
        with wave.open(io.BytesIO(data), 'rb') as source:
            params = source.getparams()
            if params.sampwidth != 2 or params.comptype != 'NONE':
                return data
            frames = source.readframes(params.nframes)
        stride = params.nchannels * 2
        window = max(1, params.framerate // 100)
        active = []
        for start in range(0, params.nframes, window):
            block = frames[start * stride:min(start + window, params.nframes) * stride]
            if any(abs(sample[0]) > 48 for sample in struct.iter_unpack('<h', block)):
                active.append(start)
        if not active:
            return data
        start = max(0, active[0] - int(params.framerate * start_padding_sec))
        end = min(params.nframes, active[-1] + window + int(params.framerate * 0.22))
        # Leave short natural boundaries alone, and never edit silence within speech.
        if start < params.framerate * 0.20:
            start = 0
        if params.nframes - end < params.framerate * 0.20:
            end = params.nframes
        if start == 0 and end == params.nframes:
            return data
        output = io.BytesIO()
        with wave.open(output, 'wb') as target:
            target.setparams(params)
            target.writeframes(frames[start * stride:end * stride])
        return output.getvalue()
    except (wave.Error, EOFError, ValueError, struct.error):
        return data
