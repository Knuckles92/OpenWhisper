"""In-process codec research only: libsndfile FLAC and libopus framed packets.

The custom 2-byte Opus packet framing isn't an OpenWhisper protocol proposal.
It permits measuring codec cost apart from launching ffmpeg per request.
"""
import ctypes
import os
import ctypes.util
import io
import struct
import numpy as np
import soundfile as sf

# ctypes discovery can fail under the isolated test environment; explicit
# installed-library fallback avoids accidentally loading the Python executable.
opus_library=os.environ.get('OW_BENCH_OPUS_LIBRARY') or ctypes.util.find_library('opus')
if not opus_library:
    raise RuntimeError('Installed libopus was not found; pass --opus-library /path/to/libopus.so.0')
lib=ctypes.CDLL(opus_library)
lib.opus_encoder_create.argtypes=[ctypes.c_int32,ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.c_int)]
lib.opus_encoder_create.restype=ctypes.c_void_p
lib.opus_encoder_destroy.argtypes=[ctypes.c_void_p]
lib.opus_encoder_ctl.argtypes=[ctypes.c_void_p,ctypes.c_int]
lib.opus_encoder_ctl.restype=ctypes.c_int
lib.opus_encode.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int16),ctypes.c_int,ctypes.POINTER(ctypes.c_ubyte),ctypes.c_int32]
lib.opus_encode.restype=ctypes.c_int32
lib.opus_decoder_create.argtypes=[ctypes.c_int32,ctypes.c_int,ctypes.POINTER(ctypes.c_int)]
lib.opus_decoder_create.restype=ctypes.c_void_p
lib.opus_decoder_destroy.argtypes=[ctypes.c_void_p]
lib.opus_decode.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ubyte),ctypes.c_int32,ctypes.POINTER(ctypes.c_int16),ctypes.c_int,ctypes.c_int]
lib.opus_decode.restype=ctypes.c_int


def opus_encode(pcm,bitrate=24000,complexity=5):
    samples=np.frombuffer(pcm,dtype='<i2')
    err=ctypes.c_int()
    enc=lib.opus_encoder_create(16000,1,2048,ctypes.byref(err))
    if err.value:raise RuntimeError(err.value)
    try:
        assert lib.opus_encoder_ctl(enc,4002,ctypes.c_int(bitrate))==0
        assert lib.opus_encoder_ctl(enc,4010,ctypes.c_int(complexity))==0
        lookahead=ctypes.c_int()
        assert lib.opus_encoder_ctl(enc,4027,ctypes.byref(lookahead))==0
        original_length=len(samples)
        samples=np.concatenate([samples,np.zeros(lookahead.value,dtype=np.int16)])
        packet=(ctypes.c_ubyte*4000)()
        output=bytearray(struct.pack('>II',original_length,lookahead.value))
        for start in range(0,len(samples),320):
            frame=np.zeros(320,dtype=np.int16)
            data=samples[start:start+320]
            frame[:len(data)]=data
            n=lib.opus_encode(enc,frame.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),320,packet,4000)
            if n<0:raise RuntimeError(n)
            output.extend(struct.pack('>H',n));output.extend(bytes(packet[:n]))
        return bytes(output)
    finally:lib.opus_encoder_destroy(enc)


def opus_decode(blob):
    err=ctypes.c_int()
    dec=lib.opus_decoder_create(16000,1,ctypes.byref(err))
    if err.value:raise RuntimeError(err.value)
    try:
        length,lookahead=struct.unpack('>II',blob[:8])
        pos=8;output=bytearray();pcm=(ctypes.c_int16*960)()
        while pos<len(blob):
            n=struct.unpack('>H',blob[pos:pos+2])[0];pos+=2
            packet=(ctypes.c_ubyte*n).from_buffer_copy(blob[pos:pos+n]);pos+=n
            samples=lib.opus_decode(dec,packet,n,pcm,960,0)
            if samples<0:raise RuntimeError(samples)
            output.extend(bytes(pcm)[:samples*2])
        return bytes(output[lookahead*2:(lookahead+length)*2])
    finally:lib.opus_decoder_destroy(dec)


def flac_encode(pcm):
    output=io.BytesIO()
    sf.write(output,np.frombuffer(pcm,'<i2'),16000,format='FLAC',subtype='PCM_16',compression_level=.5)
    return output.getvalue()


def flac_decode(blob):
    samples,rate=sf.read(io.BytesIO(blob),dtype='int16')
    assert rate==16000
    return samples.astype('<i2').tobytes()
